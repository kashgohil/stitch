"""Disposable preview media. Proxy paths never belong to a project or render plan."""

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from stitch.media import file_identity, probe_media
from stitch.models import Clip, OutputSettings, Source
from stitch.paths import AppPaths
from stitch.process import check_cancelled, run_ffmpeg
from stitch.render_plan import RenderError, RenderOptions, build_segment_plan


@dataclass(frozen=True)
class ProxySettings:
    height: int = 720

    def __post_init__(self) -> None:
        if type(self.height) is not int or self.height not in (720, 1080):
            raise RenderError("Proxy height must be 720 or 1080.")

    @property
    def width(self) -> int:
        return self.height * 16 // 9

    @property
    def bitrate(self) -> int:
        return 4_000_000 if self.height == 720 else 8_000_000


def proxy_key(source: Source, settings: ProxySettings) -> str:
    if not source.cache or not source.cache.metadata.video:
        raise RenderError("Inspect the original video before creating a proxy.")
    # Cache version includes the clock, codecs, 30 fps, and encoding recipe below.
    payload = {
        "version": 1,
        "path": str(source.path),
        "cache": source.cache.model_dump(mode="json"),
        "height": settings.height,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _require_current(source: Source) -> None:
    if source.cache is None or file_identity(source.path) != source.cache.identity:
        raise RenderError(
            "Source changed or is uninspected. Reimport it before generating a proxy."
        )


def cached_proxy(source: Source, settings: ProxySettings, paths: AppPaths) -> Path | None:
    """Bounded stat/manifest lookup, safe on the GUI thread. Never decode here."""
    try:
        _require_current(source)
        key = proxy_key(source, settings)
        path = paths.proxies / f"{key}.mp4"
        manifest_path = paths.proxies / f"{key}.json"
        if manifest_path.stat().st_size > 16_384:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("key") == key
            and manifest.get("identity") == file_identity(path).model_dump()
        ):
            return path
    except (OSError, ValueError, AttributeError):
        pass
    return None


def proxy_plan(source: Source, settings: ProxySettings, destination: Path, ffmpeg: str):
    """Use the renderer's original video clock, rotation, SAR, and audio alignment.

    No clip edits are baked in. Proxy second N always means original video second N.
    Preview is SDR-only, as is the current exporter.
    """
    plan = build_segment_plan(
        source,
        Clip(source_id=source.id),
        OutputSettings(
            width=settings.width, height=settings.height, frame_rate="30/1", profile="h264"
        ),
        destination.with_suffix(".mov"),
        ffmpeg=ffmpeg,
        options=RenderOptions(crf=24, preset="veryfast"),
    )
    command = list(plan.command)
    graph_index = command.index("-filter_complex") + 1
    command[graph_index] = command[graph_index].replace("[a]", ",aresample=osf=fltp[a]")
    command[command.index("-c:a") + 1] = "aac"
    command[command.index("-f") + 1] = "mp4"
    command[-1:] = [
        "-b:a",
        "128k",
        "-maxrate",
        str(settings.bitrate),
        "-bufsize",
        str(settings.bitrate * 2),
        "-g",
        "30",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    return tuple(command), plan


def generate_proxy(
    source: Source,
    settings: ProxySettings,
    paths: AppPaths,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> Path:
    """Worker-thread API. Validate before publication; discard incomplete media."""
    check_cancelled(cancel)
    _require_current(source)
    if existing := cached_proxy(source, settings, paths):
        return existing
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RenderError("ffmpeg is missing. Install FFmpeg and add it to PATH.")
    paths.proxies.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    key = proxy_key(source, settings)
    log_path = paths.logs / f"proxy-{uuid4().hex}.log"
    reserve = 64 * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix=".building-", dir=paths.proxies) as temporary:
        staging = Path(temporary) / "preview.mp4"
        command, plan = proxy_plan(source, settings, staging, executable)
        duration = float(plan.duration)
        required = int(duration * (settings.bitrate + 128_000) / 8 * 1.25) + reserve
        if shutil.disk_usage(paths.proxies).free < required:
            raise RenderError(
                f"Insufficient proxy cache space; approximately {required / 2**30:.2f} GiB needed."
            )

        def update(seconds: float) -> None:
            if shutil.disk_usage(paths.proxies).free < reserve:
                raise RenderError("Proxy generation stopped because cache disk space is low.")
            if progress:
                progress(min(0.99, seconds / duration))

        if run_ffmpeg(command, log_path, cancel=cancel, progress=update):
            raise RenderError(f"Proxy generation failed. See {log_path}")
        check_cancelled(cancel)
        metadata = probe_media(staging)
        video = metadata.video
        audio = metadata.audio[0] if len(metadata.audio) == 1 else None
        if (
            not video
            or (video.codec, video.pixel_format) != ("h264", "yuv420p")
            or (video.width, video.height) != (settings.width, settings.height)
            or video.average_frame_rate != "30/1"
            or video.sample_aspect_ratio != "1/1"
            or video.rotation % 360
            or video.duration is None
            or abs(video.duration - duration) > 0.001
            or abs(video.start_time or 0) > 0.001
            or not audio
            or (audio.codec, audio.sample_rate, audio.channels) != ("aac", 48000, 2)
            or audio.duration is None
            or abs(audio.duration - duration) > 0.05
            or abs(audio.start_time or 0) > 0.001
        ):
            raise RenderError(f"Proxy validation failed. See {log_path}")
        _require_current(source)
        check_cancelled(cancel)
        destination = paths.proxies / f"{key}.mp4"
        os.replace(staging, destination)
        manifest = Path(temporary) / "manifest.json"
        manifest.write_text(
            json.dumps({"key": key, "identity": file_identity(destination).model_dump()}),
            encoding="utf-8",
        )
        os.replace(manifest, paths.proxies / f"{key}.json")
    if progress:
        progress(1.0)
    return destination
