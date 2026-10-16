"""Single-segment execution in an isolated job directory, with durable diagnostics."""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from stitch.media import MediaError, file_identity, probe_media
from stitch.models import MediaMetadata, Project
from stitch.paths import AppPaths
from stitch.render_plan import RenderError, RenderOptions, SegmentPlan, build_segment_plan


@dataclass(frozen=True)
class RenderResult:
    path: Path
    job: Path
    metadata: MediaMetadata
    warnings: tuple[str, ...]


def validate_segment(plan: SegmentPlan, metadata: MediaMetadata) -> None:
    video = metadata.video
    if video is None:
        raise RenderError("Rendered segment has no video stream.")
    if (video.width, video.height) != (plan.output.width, plan.output.height):
        raise RenderError("Rendered dimensions do not match the project canvas.")
    if video.codec != plan.video_codec or video.pixel_format != plan.pixel_format:
        raise RenderError("Rendered codec or pixel format does not match the selected profile.")
    if video.average_frame_rate != plan.output.frame_rate:
        raise RenderError("Rendered frame rate does not match the project rate.")
    if video.rotation % 360 or video.sample_aspect_ratio != "1/1":
        raise RenderError("Rendered orientation or pixel aspect ratio was not normalized.")
    # MOV stream durations let us detect truncation without a full decode/frame scan.
    if video.duration is None or abs(video.duration - float(plan.duration)) > 0.0001:
        raise RenderError("Rendered video duration does not match the planned frame count.")
    if len(metadata.audio) != 1:
        raise RenderError("Rendered segment must have exactly one normalized audio stream.")
    audio = metadata.audio[0]
    if (audio.codec, audio.sample_rate, audio.channels, audio.bit_depth) != (
        "pcm_s24le",
        48000,
        2,
        24,
    ):
        raise RenderError("Rendered audio must be 24-bit PCM, 48 kHz, stereo.")
    if audio.duration is None or abs(audio.duration - plan.audio_samples / 48000) > 0.0001:
        raise RenderError("Rendered audio duration does not match the planned segment.")
    if abs(video.start_time or 0) > 0.0001 or abs(audio.start_time or 0) > 0.0001:
        raise RenderError("Rendered audio/video timestamps must start at zero.")


def render_segment(
    project: Project,
    clip_id: UUID,
    paths: AppPaths,
    *,
    options: RenderOptions = RenderOptions(),
    timeout: float | None = None,
) -> RenderResult:
    """Render one clip, never overwrite a source/final export. Not a GUI-thread API.

    The caller owns successful job cleanup. Failed jobs retain logs and any partial
    segment for diagnosis; only a returned RenderResult denotes validated success.
    """
    project = Project.model_validate(project.model_dump())
    clip = next((clip for clip in project.clips if clip.id == clip_id), None)
    if clip is None:
        raise RenderError("The selected clip does not exist in this project.")
    source = next(source for source in project.sources if source.id == clip.source_id)
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RenderError("ffmpeg is missing. Install FFmpeg and add it to PATH.")
    try:
        job = paths.new_job()
    except OSError as exc:
        raise RenderError(f"Cannot create a render job: {exc}") from exc
    manifest = {"state": "preparing", "clip_id": str(clip_id), "source": str(source.path)}
    status_path = job / "status.json"

    def write_status() -> None:
        status_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    try:
        write_status()
        if source.cache is None or file_identity(source.path) != source.cache.identity:
            raise RenderError(
                "Source metadata is missing or stale. Reimport the source before export."
            )
        plan = build_segment_plan(
            source, clip, project.output, job / "segment.mov", ffmpeg=executable, options=options
        )
        manifest.update(
            command=list(plan.command),
            frame_count=plan.frame_count,
            duration=float(plan.duration),
            warnings=list(plan.warnings),
            state="running",
        )
        write_status()
        with (job / "ffmpeg.log").open("wb") as log:
            result = subprocess.run(
                plan.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        manifest["returncode"] = result.returncode
        if result.returncode:
            raise RenderError(
                f"FFmpeg exited with code {result.returncode}. See {job / 'ffmpeg.log'}"
            )
        if file_identity(source.path) != source.cache.identity:
            raise RenderError("Source changed during rendering. The segment was not accepted.")
        metadata = probe_media(plan.destination)
        validate_segment(plan, metadata)
        manifest.update(
            state="succeeded",
            output=str(plan.destination),
            metadata=metadata.model_dump(mode="json"),
        )
        write_status()
        return RenderResult(plan.destination, job, metadata, plan.warnings)
    except (OSError, MediaError, RenderError, subprocess.TimeoutExpired) as exc:
        error = (
            "Render exceeded its time limit."
            if isinstance(exc, subprocess.TimeoutExpired)
            else str(exc)
        )
        manifest.update(state="failed", error=error)
        try:
            write_status()
        except OSError:
            pass  # Preserve the original failure, including when the disk is full.
        raise RenderError(f"{error}\nRender diagnostics: {job}") from exc
