"""Sequential, bounded-memory sequence export with guarded atomic publication."""

import json
import math
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from pathlib import Path

from stitch.audio import (
    active_overlay,
    normalize_overlay_command,
    overlay_duration,
    overlay_mix_graph,
)
from stitch.joins import plan_crossfades, render_join_chunk
from stitch.media import MediaError, file_identity, probe_media
from stitch.models import GENERATED_MEDIA_COMMENT, MediaMetadata, Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled, check_cancelled, progress_command, run_ffmpeg
from stitch.render import render_segment
from stitch.render_plan import RenderError, RenderOptions, build_segment_plan, decimal

DISK_RESERVE = 512 * 1024 * 1024


@dataclass(frozen=True)
class DiskEstimate:
    segments: int
    output: int
    reserve: int = DISK_RESERVE


@dataclass(frozen=True)
class ExportProgress:
    stage: str
    clip: int
    clip_count: int
    clip_fraction: float
    fraction: float
    elapsed: float
    message: str


@dataclass(frozen=True)
class ExportResult:
    path: Path
    job: Path
    metadata: MediaMetadata
    warnings: tuple[str, ...]


def estimate_disk(project: Project, duration: float) -> DiskEstimate:
    """Planning estimates, not CRF size guarantees; reserve and runtime checks also apply."""
    output = project.output
    pixels_per_second = output.width * output.height * float(Fraction(output.frame_rate))
    if output.profile == "prores_hq":
        bitrate = 220_000_000 * pixels_per_second / (1920 * 1080 * (30000 / 1001))
    else:
        bitrate = pixels_per_second * (0.30 if output.profile == "h264" else 0.20)
    video_bytes = max(2_000_000, bitrate) / 8 * duration
    pcm_bytes = 48000 * 2 * 3 * duration
    delivery_audio = pcm_bytes if output.profile == "prores_hq" else 320_000 / 8 * duration
    return DiskEstimate(
        math.ceil(1.5 * (video_bytes + pcm_bytes)), math.ceil(1.5 * (video_bytes + delivery_audio))
    )


def require_space(cache: Path, destination_dir: Path, estimate: DiskEstimate) -> None:
    shared = cache.stat().st_dev == destination_dir.stat().st_dev
    cache_needed = estimate.segments + estimate.reserve
    if shared:
        cache_needed += estimate.output
    if shutil.disk_usage(cache).free < cache_needed:
        raise RenderError(
            f"Not enough temporary disk space: need about {cache_needed / 2**30:.2f} GiB."
        )
    if not shared and shutil.disk_usage(destination_dir).free < estimate.output + estimate.reserve:
        raise RenderError("Not enough free space on the output volume for the estimated export.")


def check_destination(project: Project, destination: Path, *, overwrite: bool) -> tuple | None:
    expected_suffix = ".mov" if project.output.profile == "prores_hq" else ".mp4"
    if destination.suffix.lower() != expected_suffix:
        raise RenderError(f"This profile requires a {expected_suffix} output filename.")
    if not destination.parent.is_dir():
        raise RenderError("The output folder does not exist.")
    if destination.is_symlink():
        raise RenderError("Choose a regular output file, not a symbolic link.")
    for source in project.sources:
        if destination.resolve() == source.path.resolve() or (
            destination.exists() and source.path.exists() and destination.samefile(source.path)
        ):
            raise RenderError("An export cannot overwrite source media.")
    if destination.exists():
        if not destination.is_file() or not overwrite:
            raise RenderError(
                "Output already exists. Choose another filename or confirm replacement."
            )
        return destination_identity(destination)
    return None


def destination_identity(path: Path) -> tuple:
    stat = path.stat(follow_symlinks=False)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def publish_output(staged: Path, destination: Path, expected: tuple | None) -> None:
    """Publish a completed same-volume file; do not clobber a newly appeared file."""
    if expected is None:
        # Atomic no-clobber publication, unlike os.replace when the target was absent.
        # Unsupported hard-link filesystems fail safely rather than risking an overwrite.
        os.link(staged, destination)
    else:
        if destination.is_symlink() or destination_identity(destination) != expected:
            raise RenderError("Output changed during export; it was not replaced.")
        os.replace(staged, destination)


def concat_command(
    ffmpeg: str,
    playlist: Path,
    output: Path,
    project: Project,
    duration: Fraction,
    overlay_path: Path | None = None,
) -> tuple[str, ...]:
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-xerror",
        "-n",
        "-protocol_whitelist",
        "file",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        str(playlist),
    ]
    if overlay_path is not None:
        if project.overlay.end_behavior == "loop":
            args += ["-stream_loop", "-1"]
        args += [
            "-protocol_whitelist",
            "file",
            "-i",
            str(overlay_path),
            "-filter_complex_threads",
            "2",
            "-filter_complex",
            overlay_mix_graph(project, duration),
        ]
    args += [
        "-map",
        "0:v:0",
        "-map",
        "[mix]" if overlay_path is not None else "0:a:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-metadata",
        f"comment={GENERATED_MEDIA_COMMENT}",
        "-c:v",
        "copy",
        "-video_track_timescale",
        str(Fraction(project.output.frame_rate).numerator),
    ]
    if project.output.profile == "prores_hq":
        args += ["-c:a", "pcm_s24le"]
    else:
        args += ["-c:a", "aac", "-b:a", "320k"]
        if project.output.profile == "hevc_main10":
            args += ["-tag:v", "hvc1"]
    # Repair sub-sample boundary rounding on a shared clock and encode delivery audio once.
    if overlay_path is None:
        args += ["-af", "aresample=48000:async=1:first_pts=0"]
    args += [
        "-ar",
        "48000",
        "-ac",
        "2",
        "-t",
        decimal(duration),
        "-movflags",
        "+faststart",
        str(output),
    ]
    return tuple(args)


def validate_export(metadata: MediaMetadata, project: Project, duration: Fraction) -> None:
    video = metadata.video
    codec, pixel_format = {
        "h264": ("h264", "yuv420p"),
        "hevc_main10": ("hevc", "yuv420p10le"),
        "prores_hq": ("prores", "yuv422p10le"),
    }[project.output.profile]
    if video is None or (video.codec, video.pixel_format, video.width, video.height) != (
        codec,
        pixel_format,
        project.output.width,
        project.output.height,
    ):
        raise RenderError("Final video does not match the selected export profile.")
    if video.average_frame_rate != project.output.frame_rate:
        raise RenderError("Final frame rate does not match the project.")
    if video.duration is None or abs(video.duration - float(duration)) > 0.001:
        raise RenderError("Final video duration does not match the sequence.")
    if video.rotation % 360 or video.sample_aspect_ratio != "1/1":
        raise RenderError("Final display geometry is incorrect.")
    if len(metadata.audio) != 1:
        raise RenderError("Final output must have one audio stream.")
    audio = metadata.audio[0]
    expected_audio = "pcm_s24le" if project.output.profile == "prores_hq" else "aac"
    if (audio.codec, audio.sample_rate, audio.channels) != (expected_audio, 48000, 2):
        raise RenderError("Final audio format is incorrect.")
    # AAC packet duration is 1024 samples; PCM boundaries allow only sample-level rounding.
    tolerance = 1024 / 48000 + 0.001 if expected_audio == "aac" else 0.001
    if audio.duration is None or abs(audio.duration - float(duration)) > tolerance:
        raise RenderError("Final audio duration does not match the sequence.")
    if abs(video.start_time or 0) > 0.001 or abs(audio.start_time or 0) > tolerance:
        raise RenderError("Final audio/video start timestamps are not synchronized.")


def export_project(
    project: Project,
    destination: Path,
    paths: AppPaths,
    *,
    overwrite: bool = False,
    cancel: threading.Event | None = None,
    progress: Callable[[ExportProgress], None] | None = None,
    options: RenderOptions = RenderOptions(),
) -> ExportResult:
    project = Project.model_validate(project.model_dump())
    check_cancelled(cancel)
    destination = destination.expanduser().absolute()
    if not project.clips:
        raise RenderError("Add at least one clip before exporting.")
    expected = check_destination(project, destination, overwrite=overwrite)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RenderError("ffmpeg is missing. Install FFmpeg and add it to PATH.")
    job = paths.new_job()
    started = time.monotonic()
    state = {"state": "preparing", "destination": str(destination), "clips": len(project.clips)}
    staged_dir = None
    successful = False
    warnings = []
    segments = []
    overlay_path = None
    render_count = len(project.clips)

    def write_status() -> None:
        (job / "status.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def report(stage, clip=0, clip_fraction=0.0, fraction=0.0, message=""):
        if progress:
            progress(
                ExportProgress(
                    stage,
                    clip,
                    render_count,
                    clip_fraction,
                    fraction,
                    time.monotonic() - started,
                    message,
                )
            )

    try:
        write_status()
        sources = {source.id: source for source in project.sources}
        overlay_source = active_overlay(project)
        used_sources = {clip.source_id for clip in project.clips}
        if overlay_source:
            used_sources.add(overlay_source.id)
            if file_identity(overlay_source.path) != overlay_source.cache.identity:
                raise RenderError("Overlay source changed; reimport it before exporting.")
        plans = []
        colors = set()
        for index, clip in enumerate(project.clips):
            check_cancelled(cancel)
            source = sources[clip.source_id]
            if source.cache is None or file_identity(source.path) != source.cache.identity:
                raise RenderError(f"Source is missing or changed; reimport {source.path.name}.")
            plan = build_segment_plan(
                source, clip, project.output, job / f"{index}.mov", options=options, ffmpeg=ffmpeg
            )
            plans.append(plan)
            video = source.cache.metadata.video
            colors.add((video.color_primaries, video.color_transfer, video.color_matrix))
            warnings.extend(plan.warnings)
        if len(colors) > 1:
            raise RenderError(
                "Mixed color metadata is not supported yet. "
                "Normalize sources to one SDR color space."
            )
        chunks = plan_crossfades(project, plans, job)
        if chunks:
            plans = [chunk.plan for chunk in chunks]
        render_count = len(plans)
        duration = sum((plan.duration for plan in plans), Fraction(0))
        estimate = estimate_disk(project, float(duration))
        overlay_bytes = (
            math.ceil(overlay_duration(overlay_source, duration) * 48000 * 6 * 1.5)
            if overlay_source
            else 0
        )
        render_estimate = estimate.segments
        estimate = replace(estimate, segments=estimate.segments + overlay_bytes)
        render_weight = 0.85 if overlay_source else 0.9
        state.update(estimate=asdict(estimate), duration=float(duration), state="rendering")
        write_status()
        require_space(job, destination.parent, estimate)
        report(
            "preflight",
            message=f"Estimated temporary and output space: "
            f"{(estimate.segments + estimate.output + estimate.reserve) / 2**30:.2f} GiB",
        )
        segment_paths = replace(paths, jobs=job / "segments")
        completed = Fraction(0)
        for index, plan in enumerate(plans):
            check_cancelled(cancel)
            state["clip"] = index + 1
            write_status()
            remaining = float((duration - completed) / duration)
            require_space(
                job,
                destination.parent,
                replace(estimate, segments=math.ceil(render_estimate * remaining) + overlay_bytes),
            )

            def on_clip(seconds: float) -> None:
                part = min(1.0, max(0.0, seconds / float(plan.duration)))
                report(
                    "rendering",
                    index + 1,
                    part,
                    render_weight
                    * (float(completed) + part * float(plan.duration))
                    / float(duration),
                    f"Rendering {chunks[index].kind if chunks else 'clip'} "
                    f"{index + 1}/{len(plans)}",
                )

            result = (
                render_join_chunk(chunks[index], segment_paths, cancel=cancel, progress=on_clip)
                if chunks
                else render_segment(
                    project,
                    project.clips[index].id,
                    segment_paths,
                    options=options,
                    cancel=cancel,
                    progress=on_clip,
                )
            )
            segments.append(result.path)
            on_clip(float(plan.duration))
            completed += plan.duration
        if overlay_source:
            check_cancelled(cancel)
            if file_identity(overlay_source.path) != overlay_source.cache.identity:
                raise RenderError("Overlay source changed during export.")
            overlay_path = job / "overlay.wav"
            command = normalize_overlay_command(ffmpeg, overlay_source, overlay_path, duration)
            state.update(state="preparing_audio", audio_command=list(progress_command(command)))
            write_status()

            def on_audio(seconds: float) -> None:
                part = min(1.0, seconds / overlay_duration(overlay_source, duration))
                report(
                    "preparing_audio",
                    fraction=0.85 + 0.05 * part,
                    message="Preparing overlay audio",
                )

            code = run_ffmpeg(command, job / "overlay.log", cancel=cancel, progress=on_audio)
            if code:
                raise RenderError(
                    f"Overlay preparation failed (FFmpeg {code}). See {job / 'overlay.log'}"
                )
            normalized = probe_media(overlay_path)
            if not normalized.audio or (
                normalized.audio[0].codec,
                normalized.audio[0].sample_rate,
                normalized.audio[0].channels,
            ) != ("pcm_s24le", 48000, 2):
                raise RenderError("Prepared overlay audio has an unexpected format.")
            if file_identity(overlay_source.path) != overlay_source.cache.identity:
                raise RenderError("Overlay source changed during preparation.")
            check_cancelled(cancel)
        # Relative generated paths keep concat scripts safe even with quotes/unicode in user paths.
        playlist = job / "sequence.ffconcat"
        playlist.write_text(
            concat_script(job, segments, [p.duration for p in plans]), encoding="ascii"
        )
        require_space(
            job,
            destination.parent,
            DiskEstimate(0, math.ceil(sum(p.stat().st_size for p in segments) * 1.2)),
        )
        check_cancelled(cancel)
        staged_dir = Path(tempfile.mkdtemp(prefix=".stitch-export-", dir=destination.parent))
        staged = staged_dir / ("output" + destination.suffix.lower())
        command = concat_command(ffmpeg, playlist, staged, project, duration, overlay_path)
        state.update(state="assembling", command=list(progress_command(command)))
        write_status()

        def on_concat(seconds: float) -> None:
            report(
                "assembling",
                fraction=0.9 + 0.09 * min(1, seconds / float(duration)),
                message="Joining video and mixing delivery audio"
                if overlay_source
                else "Joining video and encoding delivery audio",
            )

        code = run_ffmpeg(command, job / "assembly.log", cancel=cancel, progress=on_concat)
        if code:
            raise RenderError(f"Assembly failed (FFmpeg {code}). See {job / 'assembly.log'}")
        check_cancelled(cancel)
        report("validating", fraction=0.99, message="Validating completed output")
        metadata = probe_media(staged)
        validate_export(metadata, project, duration)
        # Recheck every used source before publishing, not only the last rendered clip.
        for source_id in used_sources:
            source = sources[source_id]
            if file_identity(source.path) != source.cache.identity:
                raise RenderError("A source changed during export. Output was not published.")
        check_cancelled(cancel)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        state.update(state="publishing", metadata=metadata.model_dump(mode="json"))
        write_status()
        check_cancelled(cancel)
        publish_output(staged, destination, expected)
        successful = True  # Publication is the commit point; later cleanup must not report failure.
        state["state"] = "succeeded"
        try:
            write_status()
        except OSError as exc:
            warnings.append(f"Output saved, but the final job status could not be written: {exc}")
        try:
            report("complete", fraction=1, message=f"Exported {destination.name}")
        except Exception:
            warnings.append("Output saved, but the completion notification failed.")
        return ExportResult(destination, job, metadata, tuple(dict.fromkeys(warnings)))
    except (OSError, MediaError, RenderError) as exc:
        state.update(
            state="cancelled" if isinstance(exc, RenderCancelled) else "failed", error=str(exc)
        )
        try:
            write_status()
        except OSError:
            pass
        error_type = RenderCancelled if isinstance(exc, RenderCancelled) else RenderError
        raise error_type(f"{exc}\nExport diagnostics: {job}") from exc
    finally:
        # Only explicitly owned media files are deleted. Logs and failure segments remain.
        if successful:
            for segment in [*segments, *([overlay_path] if overlay_path else [])]:
                try:
                    segment.unlink(missing_ok=True)
                except OSError:
                    pass
        if staged_dir is not None:
            try:
                (staged_dir / ("output" + destination.suffix.lower())).unlink(missing_ok=True)
                staged_dir.rmdir()
            except OSError:
                pass


def concat_script(job: Path, segments: list[Path], durations: list[Fraction]) -> str:
    lines = ["ffconcat version 1.0"]
    total = Fraction(0)
    previous_us = 0
    for segment, duration in zip(segments, durations, strict=True):
        total += duration
        boundary_us = round(total * 1_000_000)
        # FFconcat uses microseconds. Round cumulative boundaries, not each duration,
        # so fractional-frame clips cannot accumulate timestamp drift over a long job.
        lines += [
            f"file {segment.relative_to(job).as_posix()}",
            f"duration {(boundary_us - previous_us) / 1_000_000:.6f}",
        ]
        previous_us = boundary_us
    return "\n".join(lines) + "\n"
