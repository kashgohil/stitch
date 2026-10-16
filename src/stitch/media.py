"""Read bounded metadata with ffprobe, without decoding the entire media file."""

import json
import math
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from stitch.models import (
    GENERATED_MEDIA_COMMENT,
    AudioStream,
    FileIdentity,
    MediaMetadata,
    VideoStream,
    rational,
)
from stitch.tool_setup import external_tool_environment


class MediaError(ValueError):
    """A local source cannot be inspected or used."""


class MissingSourceError(MediaError):
    """The referenced source no longer exists."""


def file_identity(path: Path) -> FileIdentity:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise MissingSourceError(f"Source is missing: {path}") from exc
    except OSError as exc:
        raise MediaError(f"Cannot access source {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise MediaError(f"Source must be a regular file: {path}")
    return FileIdentity(
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        device=info.st_dev,
        inode=info.st_ino,
    )


def _number(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _positive_int(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number > 0 and number.is_integer() else None


def _rate(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return rational(value.replace(":", "/"))
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value not in ("", "unknown", "N/A") else None


def _duration(stream: dict) -> float | None:
    value = _number(stream.get("duration"))
    return value if value is not None and value > 0 else None


def parse_metadata(raw: dict) -> MediaMetadata:
    """Normalize absent fields without inventing codec, rate, or color information."""
    try:
        streams = raw.get("streams", [])
        video_streams = [
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ]
        # Prefer the container's default stream, otherwise retain stream order.
        video_streams.sort(key=lambda s: not s.get("disposition", {}).get("default", 0))
        video = None
        if video_streams:
            stream = video_streams[0]
            rotation = next(
                (
                    item["rotation"]
                    for item in stream.get("side_data_list", [])
                    if "rotation" in item
                ),
                stream.get("tags", {}).get("rotate", 0),
            )
            pixel_format = _text(stream.get("pix_fmt"))
            # Component depth is not bits_per_pixel (e.g. yuv420p is 8-bit, not 12-bit).
            descriptor = next(
                (p for p in raw.get("pixel_formats", []) if p.get("name") == pixel_format), {}
            )
            depths = [_positive_int(c.get("bit_depth")) for c in descriptor.get("components", [])]
            bit_depth = _positive_int(stream.get("bits_per_raw_sample"))
            if bit_depth is None and depths and all(d is not None for d in depths):
                bit_depth = max(depths)
            video = VideoStream(
                index=stream["index"],
                codec=stream["codec_name"],
                width=stream["width"],
                height=stream["height"],
                rotation=(_number(rotation) or 0) % 360,
                pixel_format=pixel_format,
                bit_depth=bit_depth,
                average_frame_rate=_rate(stream.get("avg_frame_rate")),
                real_frame_rate=_rate(stream.get("r_frame_rate")),
                time_base=_rate(stream.get("time_base")),
                sample_aspect_ratio=_rate(stream.get("sample_aspect_ratio")),
                duration=_duration(stream),
                start_time=_number(stream.get("start_time")),
                color_primaries=_text(stream.get("color_primaries")),
                color_transfer=_text(stream.get("color_transfer")),
                color_matrix=_text(stream.get("color_space")),
                color_range=_text(stream.get("color_range")),
            )
        audio = tuple(
            AudioStream(
                index=s["index"],
                codec=s["codec_name"],
                sample_rate=_positive_int(s.get("sample_rate")),
                channels=_positive_int(s.get("channels")),
                channel_layout=_text(s.get("channel_layout")),
                sample_format=_text(s.get("sample_fmt")),
                bit_depth=_positive_int(s.get("bits_per_raw_sample"))
                or _positive_int(s.get("bits_per_sample")),
                duration=_duration(s),
                start_time=_number(s.get("start_time")),
                time_base=_rate(s.get("time_base")),
            )
            for s in streams
            if s.get("codec_type") == "audio"
        )
        if video is None and not audio:
            raise MediaError("No usable video or audio stream was found (cover art is not video).")
        duration = _duration(raw.get("format", {}))
        if duration is None:
            durations = [s.duration for s in ([video] if video else []) + list(audio) if s.duration]
            duration = max(durations, default=None)
        if duration is None:
            raise MediaError(
                "Source has no known positive duration; use a finite video/audio file."
            )
        return MediaMetadata(
            format_name=raw.get("format", {}).get("format_name", "unknown"),
            duration=duration,
            video=video,
            audio=audio,
            stitch_generated=raw.get("format", {}).get("tags", {}).get("comment")
            == GENERATED_MEDIA_COMMENT,
        )
    except (ValidationError, KeyError, TypeError, AttributeError) as exc:
        raise MediaError(f"ffprobe returned incomplete or invalid stream metadata: {exc}") from exc


def probe_media(path: Path, *, timeout: float = 30.0) -> MediaMetadata:
    path = path.expanduser().resolve()
    file_identity(path)
    executable = shutil.which("ffprobe")
    if executable is None:
        raise MediaError("ffprobe is missing. Install FFmpeg and add its binary directory to PATH.")
    entries = (
        "format=format_name,duration:format_tags=comment:"
        "stream=index,codec_type,codec_name,width,height,pix_fmt,bits_per_raw_sample,"
        "bits_per_sample,avg_frame_rate,r_frame_rate,time_base,sample_aspect_ratio,"
        "duration,start_time,color_primaries,color_transfer,color_space,color_range,"
        "sample_rate,channels,channel_layout,sample_fmt:"
        "stream_disposition=default,attached_pic:stream_tags=rotate:stream_side_data=rotation:"
        "pixel_format=name:component=bit_depth"
    )
    command = [
        executable,
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-probesize",
        "10000000",
        "-analyzeduration",
        "10000000",
        "-show_entries",
        entries,
        "-show_pixel_formats",
        "-of",
        "json",
        "-i",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=external_tool_environment(),
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"Inspection of {path.name} exceeded {timeout:g} seconds.") from exc
    except OSError as exc:
        raise MediaError(f"Cannot launch ffprobe for {path.name}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip()[-1000:] or "unsupported or damaged media"
        raise MediaError(f"Cannot inspect {path.name}: {detail}")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path.name}.") from exc
    if not isinstance(raw, dict):
        raise MediaError(f"ffprobe returned an invalid metadata object for {path.name}.")
    return parse_metadata(raw)
