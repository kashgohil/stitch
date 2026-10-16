import threading
from collections import namedtuple

import pytest

from stitch.media import file_identity
from stitch.models import MediaMetadata, ProbeCache, Source, VideoStream
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.proxy import ProxySettings, cached_proxy, generate_proxy, proxy_key, proxy_plan
from stitch.render_plan import RenderError


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "original.mp4"
    path.write_bytes(b"original")
    return Source(
        path=path,
        cache=ProbeCache(
            identity=file_identity(path),
            metadata=MediaMetadata(
                format_name="mp4",
                duration=120,
                video=VideoStream(
                    index=2,
                    codec="hevc",
                    width=7680,
                    height=4320,
                    average_frame_rate="120/1",
                    start_time=5,
                ),
            ),
        ),
    )


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


def test_key_tracks_identity_settings_and_metadata_not_source_id(source):
    settings = ProxySettings()
    key = proxy_key(source, settings)
    assert key == proxy_key(Source(path=source.path, cache=source.cache), settings)
    assert key != proxy_key(source, ProxySettings(1080))
    identity = source.cache.identity.model_copy(update={"mtime_ns": 42})
    changed = source.model_copy(
        update={"cache": source.cache.model_copy(update={"identity": identity})}
    )
    assert key != proxy_key(changed, settings)
    with pytest.raises(RenderError):
        ProxySettings(480)


def test_plan_preserves_source_time_without_edits_and_bounds_decode(source, tmp_path):
    command, plan = proxy_plan(source, ProxySettings(), tmp_path / "proxy.mp4", "ffmpeg")
    assert command[command.index("-i") + 1] == str(source.path)
    graph = command[command.index("-filter_complex") + 1]
    assert "[0:2]trim=start=5.000000000:end=125.000000000" in graph
    assert "setpts=PTS-(5.000000000)/TB" in graph
    assert "crop=" not in graph
    assert plan.output.frame_rate == "30/1" and plan.output.width == 1280
    assert command[command.index("-threads") + 1] == "2"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-g") + 1] == "30"


def test_stale_source_cancel_and_insufficient_disk_do_not_launch(source, paths, monkeypatch):
    monkeypatch.setattr("stitch.proxy.shutil.which", lambda _: "ffmpeg")
    monkeypatch.setattr("stitch.proxy.run_ffmpeg", lambda *a, **k: pytest.fail("must not launch"))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RenderCancelled):
        generate_proxy(source, ProxySettings(), paths, cancel=cancel)
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("stitch.proxy.shutil.disk_usage", lambda _: usage(100, 99, 1))
    with pytest.raises(RenderError, match="space"):
        generate_proxy(source, ProxySettings(), paths)
    assert not list(paths.proxies.iterdir())
    source.path.write_bytes(b"modified original")
    assert cached_proxy(source, ProxySettings(), paths) is None
    with pytest.raises(RenderError, match="Reimport"):
        generate_proxy(source, ProxySettings(), paths)


def test_cancel_during_encoding_removes_partial_media(source, paths, monkeypatch):
    monkeypatch.setattr("stitch.proxy.shutil.which", lambda _: "ffmpeg")

    def cancel(command, *args, **kwargs):
        from pathlib import Path

        Path(command[-1]).write_bytes(b"partial")
        raise RenderCancelled("cancelled")

    monkeypatch.setattr("stitch.proxy.run_ffmpeg", cancel)
    with pytest.raises(RenderCancelled):
        generate_proxy(source, ProxySettings(), paths)
    assert not list(paths.proxies.iterdir())
