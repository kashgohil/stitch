import array
import shutil
import threading

import pytest
from test_render_integration import ffmpeg, require_encoder

from stitch.document import change_project
from stitch.export import export_project
from stitch.media import probe_media
from stitch.models import OutputSettings, Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.project_io import load_project, save_project
from stitch.proxy import ProxySettings, cached_proxy, generate_proxy
from stitch.render_plan import RenderError
from stitch.sources import add_video, refresh_source

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required"
    ),
]


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


def make_source(tmp_path):
    path = tmp_path / "source.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=60:duration=1",
        "-itsoffset",
        "0.4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=0.6",
        "-c:v",
        "mpeg4",
        "-c:a",
        "pcm_s24le",
        "-output_ts_offset",
        "5",
        path,
    )
    return add_video(Project(), path)


@pytest.mark.parametrize("height", [720, 1080])
def test_proxy_clock_audio_cache_reopen_and_changed_source(tmp_path, paths, height):
    require_encoder("libx264")
    project = make_source(tmp_path)
    source = project.sources[0]
    progress = []
    settings = ProxySettings(height)
    result = generate_proxy(source, settings, paths, progress=progress.append)
    metadata = probe_media(result)
    assert metadata.video.height == height and metadata.video.average_frame_rate == "30/1"
    assert metadata.video.start_time == 0 and metadata.video.duration == pytest.approx(1)
    samples = array.array(
        "f", ffmpeg("-i", result, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    )
    assert max(abs(x) for x in samples[: int(0.3 * 48000)]) < 0.001
    assert max(abs(x) for x in samples[int(0.5 * 48000) : int(0.8 * 48000)]) > 0.05
    assert progress == sorted(progress) and progress[-1] == 1
    saved = tmp_path / "project.json"
    save_project(project, saved)
    reopened = load_project(saved)
    assert cached_proxy(reopened.sources[0], settings, paths) == result
    before = result.stat()
    assert generate_proxy(source, settings, paths) == result
    assert result.stat() == before
    # Identity changes invalidate old media, even at the same path.
    source.path.touch()
    assert cached_proxy(source, settings, paths) is None
    refreshed = refresh_source(source)
    regenerated = generate_proxy(refreshed, settings, paths)
    assert regenerated != result
    # A truncated cache entry is not reused.
    regenerated.write_bytes(b"broken")
    assert cached_proxy(refreshed, settings, paths) is None
    assert probe_media(generate_proxy(refreshed, settings, paths)).video.codec == "h264"


@pytest.mark.parametrize("dimensions", ["3840x2160", "7680x4320"])
def test_high_resolution_high_fps_hevc_proxy(tmp_path, paths, dimensions):
    require_encoder("libx265")
    require_encoder("libx264")
    source = tmp_path / "high-resolution.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:size={dimensions}:rate=120",
        "-frames:v",
        "1",
        "-c:v",
        "libx265",
        "-pix_fmt",
        "yuv420p10le",
        "-preset",
        "ultrafast",
        "-x265-params",
        "pools=2:frame-threads=2:log-level=error",
        "-tag:v",
        "hvc1",
        source,
    )
    project = add_video(Project(), source)
    result = generate_proxy(project.sources[0], ProxySettings(), paths)
    video = probe_media(result).video
    assert (video.width, video.height, video.codec) == (1280, 720, "h264")
    decoded = ffmpeg("-i", result, "-map", "0:v:0", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
    assert len(decoded) == 1280 * 720 * 3
    assert decoded[2] > 200 and decoded[0] < 10


def test_proxy_never_enters_export_and_project_stays_original(tmp_path, paths, monkeypatch):
    project = make_source(tmp_path)
    saved_model = project.model_dump()
    proxy = generate_proxy(project.sources[0], ProxySettings(), paths)
    assert project.model_dump() == saved_model
    from stitch.process import run_ffmpeg

    commands = []

    def record(command, *args, **kwargs):
        commands.append(command)
        return run_ffmpeg(command, *args, **kwargs)

    monkeypatch.setattr("stitch.export.run_ffmpeg", record)
    monkeypatch.setattr("stitch.render.run_ffmpeg", record)
    project = change_project(
        project, output=OutputSettings(width=160, height=90, frame_rate="60/1", profile="h264")
    )
    result = export_project(project, tmp_path / "final.mp4", paths)
    assert result.path.exists()
    assert any(str(project.sources[0].path) in command for command in commands)
    assert all(str(proxy) not in command for command in commands)
    assert all(str(paths.proxies) not in " ".join(command) for command in commands)
    assert probe_media(result.path).video.average_frame_rate == "60/1"


def test_cancel_or_source_change_during_generation_does_not_publish(tmp_path, paths, monkeypatch):
    project = make_source(tmp_path)
    source = project.sources[0]
    cancel = threading.Event()

    def stop(fraction):
        cancel.set()

    with pytest.raises(RenderCancelled):
        generate_proxy(source, ProxySettings(), paths, cancel=cancel, progress=stop)
    assert cached_proxy(source, ProxySettings(), paths) is None
    assert not list(paths.proxies.glob("*.mp4"))
    from stitch.process import run_ffmpeg

    def replace_source(*args, **kwargs):
        code = run_ffmpeg(*args, **kwargs)
        source.path.touch()
        return code

    monkeypatch.setattr("stitch.proxy.run_ffmpeg", replace_source)
    with pytest.raises(RenderError, match="Reimport"):
        generate_proxy(source, ProxySettings(), paths)
    assert not list(paths.proxies.glob("*.mp4"))
