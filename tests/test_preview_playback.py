"""Real Qt decoder check, separate from deterministic control/worker tests."""

import shutil

import pytest
from PySide6.QtMultimedia import QMediaPlayer
from test_render_integration import ffmpeg, require_encoder

from stitch.dependencies import ToolStatus
from stitch.paths import AppPaths
from stitch.window import MainWindow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required"
    ),
]


def test_native_proxy_generation_playback_seeking_and_reopen(qtbot, monkeypatch, tmp_path):
    require_encoder("libx264")
    source = tmp_path / "test-source.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=60:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        source,
    )
    monkeypatch.setattr(
        "stitch.window.probe_tools",
        lambda: (ToolStatus("ffmpeg", shutil.which("ffmpeg"), version="test"),),
    )
    paths = AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))
    window = MainWindow(paths)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.tools_ready)
    window.import_paths([source])
    qtbot.waitUntil(lambda: not window._importing)
    window.clip_editor.start.setValue(0.25)
    window.clip_editor.source_end.setChecked(False)
    window.clip_editor.end.setValue(1.5)
    window.apply_edits()
    project_path = tmp_path / "project.json"
    assert window.save_to_path(project_path)
    window.sequence_tabs.setCurrentIndex(1)
    preview = window.preview
    preview.mute.setChecked(True)  # No audio device is required to validate video playback.
    frames = []
    preview.video.videoSink().videoFrameChanged.connect(
        lambda frame: frames.append(frame.size()) if frame.isValid() else None
    )
    preview.generate.click()
    qtbot.waitUntil(lambda: preview.worker is None, timeout=30000)
    assert preview.path is not None, preview.state.text()
    qtbot.waitUntil(lambda: preview.play.isEnabled(), timeout=10000)
    proxy = preview.path
    stat = proxy.stat()
    assert preview.player.source().toLocalFile() == str(proxy)
    assert preview.player.position() == 250
    preview.play.click()
    qtbot.waitUntil(lambda: preview.player.position() >= 500 and bool(frames), timeout=10000)
    assert frames[-1].width() == 1280 and frames[-1].height() == 720
    frame_image = preview.video.videoSink().videoFrame().toImage()
    assert not frame_image.isNull()
    assert frame_image.save(str(tmp_path / "decoded-frame.png"))
    preview.play.click()
    preview.seek.setValue(1000)
    qtbot.waitUntil(lambda: abs(preview.player.position() - 1000) < 50)
    assert preview.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    assert window.grab().save(str(tmp_path / "preview.png"))
    # Reopen performs a fast cache lookup, with no generation worker or source substitution.
    window.new_project()
    assert window.open_path(project_path)
    qtbot.waitUntil(lambda: preview.play.isEnabled(), timeout=10000)
    assert preview.path == proxy and proxy.stat() == stat and preview.worker is None
    assert window.document.project.sources[0].path == source
    assert not window.document.dirty
    window.close()
