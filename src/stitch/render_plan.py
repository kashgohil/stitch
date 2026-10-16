"""Pure, deterministic plans for original-media segment renders. No process or I/O."""

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from stitch.models import Clip, Crop, OutputSettings, Project, Source, VideoStream


class RenderError(ValueError):
    """A segment cannot be rendered safely with the current settings."""


@dataclass(frozen=True)
class RenderOptions:
    crf: int = 18
    preset: str = "medium"
    threads: int = 2

    def __post_init__(self) -> None:
        if type(self.crf) is not int or not 0 <= self.crf <= 51:
            raise RenderError("CRF must be an integer between 0 and 51.")
        if self.preset not in {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        }:
            raise RenderError("Unsupported software encoder preset.")
        if type(self.threads) is not int or not 1 <= self.threads <= 16:
            raise RenderError("Encoder threads must be an integer between 1 and 16.")


@dataclass(frozen=True)
class SegmentPlan:
    command: tuple[str, ...]
    source: Source
    destination: Path
    frame_count: int
    duration: Fraction
    audio_samples: int
    pixel_format: str
    video_codec: str
    output: OutputSettings
    warnings: tuple[str, ...]


def decimal(value: float | Fraction) -> str:
    return f"{float(value):.9f}"


def fit_dimensions(
    video: VideoStream, crop: Crop | None, output: OutputSettings
) -> tuple[int, int]:
    """Fit oriented display pixels to an even, square-pixel canvas without stretching."""
    if video.rotation % 90:
        raise RenderError("Only 0, 90, 180, and 270 degree source rotation is supported.")
    width, height = video.display_dimensions
    if crop:
        width, height = crop.width, crop.height
    sar = Fraction(video.sample_aspect_ratio or "1/1")
    if video.rotation % 180 == 90:
        sar = 1 / sar
    display_width = width * sar
    factor = min(Fraction(output.width) / display_width, Fraction(output.height, height))
    if not output.allow_upscale:
        factor = min(factor, Fraction(1))
    fitted = (int(display_width * factor) // 2 * 2, int(height * factor) // 2 * 2)
    if min(fitted) < 2:
        raise RenderError("Crop is too narrow to fit as even-sized video without stretching.")
    return fitted


def build_segment_plan(
    source: Source,
    clip: Clip,
    output: OutputSettings,
    destination: Path,
    *,
    ffmpeg: str = "ffmpeg",
    options: RenderOptions = RenderOptions(),
) -> SegmentPlan:
    # Revalidate references, bounds, and even dimensions, including model_copy callers.
    project = Project.model_validate(
        {
            "sources": [source.model_dump()],
            "clips": [clip.model_dump()],
            "output": output.model_dump(),
        }
    )
    source, clip, output = project.sources[0], project.clips[0], project.output
    if source.cache is None or source.cache.metadata.video is None:
        raise RenderError("Inspect the original video before rendering it.")
    if not destination.is_absolute() or destination.suffix.lower() != ".mov":
        raise RenderError("Normalized segments require an absolute .mov destination.")
    if destination == source.path:
        raise RenderError("A segment must not overwrite source media.")
    video = source.cache.metadata.video
    if video.color_transfer in {"smpte2084", "arib-std-b67"}:
        raise RenderError("HDR input is not supported yet. Convert it to SDR explicitly first.")
    width, height = fit_dimensions(video, clip.crop, output)
    end = (
        clip.out_point
        if clip.out_point is not None
        else (video.duration or source.cache.metadata.duration)
    )
    fps = Fraction(output.frame_rate)
    requested_duration = Fraction(str(end)) - Fraction(str(clip.in_point))
    frame_count = math.ceil(requested_duration * fps)
    duration = frame_count / fps
    audio_samples = round(duration * 48000)
    origin = Fraction(str(video.start_time or 0)) + Fraction(str(clip.in_point))
    absolute_end = origin + requested_duration
    pixel_format, codec, encoder = {
        "hevc_main10": ("yuv420p10le", "hevc", "libx265"),
        "h264": ("yuv420p", "h264", "libx264"),
        "prores_hq": ("yuv422p10le", "prores", "prores_ks"),
    }[output.profile]
    filters = [
        f"trim=start={decimal(origin)}:end={decimal(absolute_end)}",
        f"setpts=PTS-({decimal(origin)})/TB",
    ]
    if clip.crop:
        crop = clip.crop
        filters.append(f"crop=w={crop.width}:h={crop.height}:x={crop.x}:y={crop.y}:exact=1")
    # Explicit range conversion avoids tagging full-range samples as limited or vice versa.
    input_range = "pc" if video.color_range == "pc" else "tv"
    filters += [
        f"scale=w={width}:h={height}:flags=lanczos:in_range={input_range}:out_range=tv",
        "setsar=1",
        f"format=pix_fmts={pixel_format}",
        f"pad=w={output.width}:h={output.height}:x=(ow-iw)/2:y=(oh-ih)/2:color=black",
        # Pad at most the rounded segment end; never extend a short source indefinitely.
        f"tpad=stop_mode=clone:stop_duration={decimal(duration)}",
        f"fps=fps={output.frame_rate}:start_time=0:round=near:eof_action=pass",
        f"trim=end_frame={frame_count}",
        f"setpts=N/(({output.frame_rate})*TB)",
    ]
    graph = f"[0:{video.index}]" + ",".join(filters) + "[v]"
    audios = source.cache.metadata.audio
    if audios and clip.audio.enabled:
        audio = audios[0]
        # Keep the common video clock: resetting each stream to STARTPTS would erase delay.
        graph += (
            f";[0:{audio.index}]atrim=start={decimal(origin)}:end={decimal(absolute_end)},"
            f"asetpts=PTS-({decimal(origin)})/TB,"
            "aresample=48000:async=1:first_pts=0:osf=dbl:ochl=stereo,"
            f"volume={clip.audio.gain_db:g}dB:precision=double,aresample=osf=s32,"
            f"apad=whole_len={audio_samples},"
            f"atrim=end_sample={audio_samples},asetpts=N/SR/TB[a]"
        )
    else:
        graph += (
            ";anullsrc=r=48000:cl=stereo,aresample=osf=s32,"
            f"atrim=end_sample={audio_samples},asetpts=N/SR/TB[a]"
        )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-xerror",
        "-nostdin",
        "-n",
        "-filter_complex_threads",
        str(options.threads),
        "-copyts",
        "-threads",
        str(options.threads),
        "-protocol_whitelist",
        "file",
    ]
    # Seek to a preceding keyframe on the original clock, then let trim/atrim do
    # sample-accurate removal. FFmpeg's implicit accurate-seek discard can apply
    # the container offset twice when copyts and seek_timestamp are combined.
    if clip.in_point > 0:
        command += ["-noaccurate_seek", "-seek_timestamp", "1", "-ss", decimal(origin)]
    command += [
        "-i",
        str(source.path),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-metadata:s:v:0",
        "rotate=0",
        "-c:v",
        encoder,
        "-pix_fmt",
        "+" + pixel_format,
        "-threads:v",
        str(options.threads),
        "-fps_mode:v",
        "cfr",
        "-r:v",
        output.frame_rate,
        "-video_track_timescale",
        str(fps.numerator),
        "-color_range",
        "tv",
    ]
    for option, value in (
        ("-color_primaries", video.color_primaries),
        ("-color_trc", video.color_transfer),
        ("-colorspace", video.color_matrix),
    ):
        if value:
            command += [option, value]
    if output.profile == "prores_hq":
        command += ["-profile:v", "3"]
    else:
        command += ["-preset", options.preset, "-crf", str(options.crf)]
        if output.profile == "hevc_main10":
            command += [
                "-profile:v",
                "main10",
                "-tag:v",
                "hvc1",
                "-x265-params",
                f"pools={options.threads}:frame-threads={options.threads}:log-level=error",
            ]
        else:
            command += ["-profile:v", "high"]
    # PCM intermediates avoid AAC encoder delay at every clip boundary. Final delivery
    # audio is encoded once during sequence assembly/mixing (a separate operation).
    command += [
        "-c:a",
        "pcm_s24le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-t",
        decimal(duration),
        "-f",
        "mov",
        str(destination),
    ]
    warnings = []
    if output.profile == "h264" and video.bit_depth and video.bit_depth > 8:
        warnings.append("The compatibility profile deliberately converts source video to 8-bit.")
    if not video.color_range:
        warnings.append("Source color range is unknown; limited range is assumed.")
    if not video.sample_aspect_ratio:
        warnings.append("Source pixel aspect ratio is unknown; square pixels are assumed.")
    return SegmentPlan(
        tuple(command),
        source,
        destination,
        frame_count,
        duration,
        audio_samples,
        pixel_format,
        codec,
        output,
        tuple(warnings),
    )
