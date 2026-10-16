"""Bounded overlay preparation and explicit whole-sequence mixing commands."""

from fractions import Fraction
from pathlib import Path

from stitch.models import Project, Source
from stitch.render_plan import PEAK_LIMITER, RenderError, decimal


def active_overlay(project: Project) -> Source | None:
    if project.overlay is None or not project.overlay.audio.enabled:
        return None
    source = next(source for source in project.sources if source.id == project.overlay.source_id)
    if source.cache is None or not source.cache.metadata.audio:
        raise RenderError("Inspect the overlay audio before exporting.")
    return source


def overlay_duration(source: Source, sequence_duration: Fraction) -> float:
    audio = source.cache.metadata.audio[0]
    return min(audio.duration or source.cache.metadata.duration, float(sequence_duration))


def normalize_overlay_command(
    ffmpeg: str,
    source: Source,
    destination: Path,
    sequence_duration: Fraction,
) -> tuple[str, ...]:
    """Decode once to PCM. Loop the normalized audio, not video/container padding."""
    audio = source.cache.metadata.audio[0]
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-xerror",
        "-n",
        "-filter_threads",
        "2",
        "-protocol_whitelist",
        "file",
        "-i",
        str(source.path),
        "-map",
        f"0:{audio.index}",
        "-vn",
        "-map_metadata",
        "-1",
        "-af",
        "asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0:ochl=stereo",
        "-c:a",
        "pcm_s24le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-t",
        decimal(overlay_duration(source, sequence_duration)),
        "-rf64",
        "auto",
        "-f",
        "wav",
        str(destination),
    )


def overlay_mix_graph(project: Project, duration: Fraction) -> str:
    """Input zero is the assembled sequence; input one is normalized overlay PCM."""
    samples = round(duration * 48000)
    gain = project.overlay.audio.gain_db
    return (
        "[0:a:0]aresample=48000:async=1:first_pts=0,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"apad=whole_len={samples},atrim=end_sample={samples}[base];"
        "[1:a:0]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"volume={gain:g}dB,atrim=end_sample={samples},apad=whole_len={samples}[overlay];"
        "[base][overlay]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"{PEAK_LIMITER},atrim=end_sample={samples},asetpts=N/SR/TB[mix]"
    )
