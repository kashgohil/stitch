"""Bounded two-input crossfades, encoded once directly from original media.

Each non-overlapped body and each adjacent overlap is an independent final-profile
segment. No intermediate video is decoded for another lossy encode. Tail overlaps
can reread a source prefix to preserve the exact normalized frame grid, including VFR.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from stitch.media import file_identity, probe_media
from stitch.models import Project, Source
from stitch.paths import AppPaths
from stitch.process import RenderCancelled, check_cancelled, progress_command, run_ffmpeg
from stitch.render import RenderResult, validate_segment
from stitch.render_plan import RenderError, SegmentPlan, decimal


@dataclass(frozen=True)
class JoinChunk:
    plan: SegmentPlan
    sources: tuple[Source, ...]
    kind: str


def chunk_plan(
    inputs: list[tuple[SegmentPlan, int]],
    frames: int,
    destination: Path,
) -> JoinChunk:
    """Inputs are normalized clip plans and their start-frame offsets."""
    base = inputs[0][0]
    fps = Fraction(base.output.frame_rate)
    duration = frames / fps
    samples = round(duration * 48000)
    command = list(base.command[: base.command.index("-copyts") + 1])
    graphs = []
    for index, (plan, start) in enumerate(inputs):
        # Input seek still refers to the original clip's in-point, not a proxy or a
        # rounded frame offset. The subsequent frame trim preserves its VFR grid.
        command += list(
            plan.command[plan.command.index("-copyts") + 1 : plan.command.index("-filter_complex")]
        )
        graph = plan.command[plan.command.index("-filter_complex") + 1]
        graph = (
            graph.replace("[0:", f"[{index}:")
            .replace("[v]", f"[v{index}base]")
            .replace("[a]", f"[a{index}base]")
        )
        graph += (
            f";[v{index}base]trim=start_frame={start}:end_frame={start + frames},"
            f"setpts=N/(({base.output.frame_rate})*TB)[v{index}]"
            f";[a{index}base]atrim=start_sample={round(start / fps * 48000)}:"
            f"end_sample={round((start + frames) / fps * 48000)},asetpts=N/SR/TB,"
            f"apad=whole_len={samples},atrim=end_sample={samples}[a{index}]"
        )
        graphs.append(graph)
    if len(inputs) == 2:
        graphs.append(
            f"[v0][v1]blend=all_expr='A*(1-T/{decimal(duration)})+B*T/{decimal(duration)}':"
            f"shortest=1,format=pix_fmts={base.pixel_format}[v];"
            f"[a0][a1]acrossfade=ns={samples}:c1=tri:c2=tri,aresample=osf=s32,"
            f"atrim=end_sample={samples},asetpts=N/SR/TB[a]"
        )
    else:
        graphs.append("[v0]null[v];[a0]anull[a]")
    command += ["-filter_complex", ";".join(graphs)]
    tail = list(base.command[base.command.index("-map") :])
    if "-x265-params" in tail:
        # One-frame 8K overlaps can expose invalid x265 reorder timestamps in MOV.
        # Use the same no-B-frame configuration for every HEVC chunk, including
        # bodies, so stream-copy assembly keeps consistent decoder parameters.
        tail[tail.index("-x265-params") + 1] += ":bframes=0"
    tail[tail.index("-t") + 1] = decimal(duration)
    tail[-1] = str(destination)
    command += tail
    return JoinChunk(
        replace(
            base,
            command=tuple(command),
            destination=destination,
            frame_count=frames,
            duration=duration,
            audio_samples=samples,
        ),
        tuple(p.source for p, _ in inputs),
        "crossfade" if len(inputs) == 2 else "body",
    )


def plan_crossfades(project: Project, plans: list[SegmentPlan], job: Path) -> list[JoinChunk]:
    overlap = project.output.overlap_frames
    if len(plans) < 2 or not overlap:
        return []
    chunks = []
    for index, plan in enumerate(plans):
        incoming = overlap if index else 0
        outgoing = overlap if index < len(plans) - 1 else 0
        body = plan.frame_count - incoming - outgoing
        if body < 0:
            raise RenderError(f"Clip {index + 1} is too short for the rounded crossfade overlaps.")
        if body:
            chunks.append(chunk_plan([(plan, incoming)], body, job / f"part-{len(chunks):05}.mov"))
        if outgoing:
            chunks.append(
                chunk_plan(
                    [(plan, plan.frame_count - overlap), (plans[index + 1], 0)],
                    overlap,
                    job / f"part-{len(chunks):05}.mov",
                )
            )
    return chunks


def render_join_chunk(
    chunk: JoinChunk,
    paths: AppPaths,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderResult:
    check_cancelled(cancel)
    job = paths.new_job()
    plan = replace(chunk.plan, destination=job / "segment.mov")
    plan = replace(plan, command=(*plan.command[:-1], str(plan.destination)))
    state = {
        "state": "running",
        "kind": chunk.kind,
        "command": list(progress_command(plan.command)),
        "duration": float(plan.duration),
        "frame_count": plan.frame_count,
    }
    status = job / "status.json"
    try:
        status.write_text(json.dumps(state, indent=2), encoding="utf-8")
        for source in chunk.sources:
            if file_identity(source.path) != source.cache.identity:
                raise RenderError("Crossfade source changed; reimport it before exporting.")
        code = run_ffmpeg(plan.command, job / "ffmpeg.log", cancel=cancel, progress=progress)
        if code:
            raise RenderError(f"Crossfade segment failed (FFmpeg {code}). See {job / 'ffmpeg.log'}")
        check_cancelled(cancel)
        for source in chunk.sources:
            if file_identity(source.path) != source.cache.identity:
                raise RenderError("Source changed during crossfade rendering.")
        metadata = probe_media(plan.destination)
        validate_segment(plan, metadata)
        check_cancelled(cancel)
        state.update(state="succeeded", metadata=metadata.model_dump(mode="json"))
        status.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return RenderResult(plan.destination, job, metadata, plan.warnings)
    except Exception as exc:
        state.update(
            state="cancelled" if isinstance(exc, RenderCancelled) else "failed", error=str(exc)
        )
        try:
            status.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError:
            pass
        raise
