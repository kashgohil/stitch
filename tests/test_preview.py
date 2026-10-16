import threading

import pytest
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QMessageBox

from stitch.dependencies import ToolStatus
from stitch.media import file_identity
from stitch.models import Clip, MediaMetadata, ProbeCache, Source, VideoStream
from stitch.paths import AppPaths
from stitch.preview import PreviewPanel, time_label
from stitch.process import RenderCancelled
from stitch.sources import source_status
from stitch.window import MainWindow


class Player(QObject):
    """Deterministic backend for controls and worker lifecycle tests."""

    MediaStatus = QMediaPlayer.MediaStatus
    PlaybackState = QMediaPlayer.PlaybackState
    Error = QMediaPlayer.Error
    positionChanged = Signal(int)
    mediaStatusChanged = Signal(object)
    playbackStateChanged = Signal(object)
    errorOccurred = Signal(object, str)
    seekableChanged = Signal(bool)

    def __init__(self, parent):
        super().__init__(parent)
        self.url = QUrl()
        self.pos = 0
        self.state = self.PlaybackState.StoppedState

    def setAudioOutput(self, output):
        pass

    def setVideoOutput(self, output):
        pass

    def setSource(self, url):
        self.url = url
        if not url.isEmpty():
            self.mediaStatusChanged.emit(self.MediaStatus.LoadedMedia)

    def setPosition(self, position):
        if self.pos != position:
            self.pos = position
            self.positionChanged.emit(position)

    def position(self):
        return self.pos

    def isSeekable(self):
        return not self.url.isEmpty()

    def playbackState(self):
        return self.state

    def play(self):
        self.state = self.PlaybackState.PlayingState
        self.playbackStateChanged.emit(self.state)

    def pause(self):
        self.state = self.PlaybackState.PausedState
        self.playbackStateChanged.emit(self.state)

    def stop(self):
        self.state = self.PlaybackState.StoppedState
        self.setPosition(0)
        self.playbackStateChanged.emit(self.state)


@pytest.fixture
def setup_preview(monkeypatch, tmp_path):
    monkeypatch.setattr("stitch.preview.QMediaPlayer", Player)
    metadata = MediaMetadata(
        format_name="mov",
        duration=20,
        video=VideoStream(
            index=0,
            codec="hevc",
            width=7680,
            height=4320,
            start_time=5,
        ),
    )
    path = tmp_path / "original.mov"
    path.write_bytes(b"original")
    source = Source(path=path, cache=ProbeCache(identity=file_identity(path), metadata=metadata))
    clip = Clip(source_id=source.id, in_point=2, out_point=10)
    paths = AppPaths(*(tmp_path / str(i) for i in range(4)))
    cached = {}
    monkeypatch.setattr(
        "stitch.preview.cached_proxy",
        lambda src, settings, _: (
            cached.get((src.path, settings.height)) if source_status(src).state == "ready" else None
        ),
    )
    return source, clip, paths, cached


@pytest.fixture
def panel(qtbot, setup_preview):
    source, clip, paths, cached = setup_preview
    panel = PreviewPanel(paths)
    qtbot.addWidget(panel)
    panel.set_available(True)
    panel.load(clip, source)
    panel.show()
    yield panel
    panel.shutdown()
    qtbot.waitUntil(lambda: panel.worker is None)


def test_cached_play_seek_trim_and_source_time(panel, setup_preview):
    source, clip, paths, cached = setup_preview
    assert "No valid" in panel.state.text() and not panel.play.isEnabled()
    cached[(source.path, 720)] = paths.proxies / "720.mp4"
    panel.load(clip, source)
    assert panel.path == cached[(source.path, 720)] and panel.play.isEnabled()
    assert panel.player.position() == 2000  # Not the container timestamp 5 + 2.
    panel.play.click()
    assert panel.player.playbackState() == Player.PlaybackState.PlayingState
    panel.player.setPosition(10100)
    assert panel.player.playbackState() == Player.PlaybackState.PausedState
    assert panel.player.position() == 10000
    panel.play.click()
    assert panel.player.position() == 2000
    panel.seek.setValue(7500)
    assert panel.player.position() == 7500
    assert panel.time.text() == "00:00:07.500"
    assert panel.player.playbackState() == Player.PlaybackState.PausedState
    marked = []
    panel.mark_in.connect(marked.append)
    panel.set_in.click()
    assert marked == [7.5]
    assert clip.in_point == 2 and source.path.name == "original.mov"
    assert time_label(7_200_125) == "02:00:00.125"


def test_quality_change_missing_original_and_playback_failure(panel, setup_preview):
    source, clip, paths, cached = setup_preview
    cached[(source.path, 720)] = paths.proxies / "720.mp4"
    cached[(source.path, 1080)] = paths.proxies / "1080.mp4"
    panel.load(clip, source)
    panel.quality.setCurrentIndex(1)
    assert panel.path == cached[(source.path, 1080)]
    panel.player.errorOccurred.emit(Player.Error.ResourceError, "backend unavailable")
    assert not panel.play.isEnabled() and "backend unavailable" in panel.state.text()
    source.path.unlink()
    assert not panel._check_freshness()
    assert panel.path is None and panel.player.url.isEmpty()
    assert "missing" in panel.state.text()


def test_generation_is_background_cancellable_and_switch_discards_late_result(
    panel,
    setup_preview,
    monkeypatch,
    qtbot,
):
    source, clip, paths, cached = setup_preview
    started, release = threading.Event(), threading.Event()
    threads = []

    def generate(src, settings, paths, *, cancel, progress):
        threads.append(threading.get_ident())
        started.set()
        release.wait(timeout=3)
        assert cancel.is_set()
        # Simulate completion racing with cancellation; it must not replace the new selection.
        cached[(src.path, settings.height)] = paths.proxies / "late.mp4"
        return cached[(src.path, settings.height)]

    monkeypatch.setattr("stitch.workers.generate_proxy", generate)
    panel.generate.click()
    try:
        qtbot.waitUntil(started.is_set)
        assert panel.cancel.isEnabled() and not panel.generate.isEnabled()
        panel.load(None, None)
        assert panel.worker.cancel.is_set() and not panel.cancel.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: panel.worker is None)
    assert threads[0] != threading.get_ident()
    assert panel.path is None and "Select a clip" in panel.state.text()


def test_success_cancel_and_failure_states(panel, setup_preview, monkeypatch, qtbot):
    source, clip, paths, cached = setup_preview

    def generate(src, settings, paths, **kwargs):
        kwargs["progress"](0.5)
        cached[(src.path, settings.height)] = paths.proxies / "ready.mp4"
        return cached[(src.path, settings.height)]

    monkeypatch.setattr("stitch.workers.generate_proxy", generate)
    panel.generate.click()
    qtbot.waitUntil(lambda: panel.worker is None)
    assert panel.path and panel.play.isEnabled() and "Ready" in panel.state.text()
    cached.clear()

    def cancelled(*args, **kwargs):
        raise RenderCancelled("cancelled")

    monkeypatch.setattr("stitch.workers.generate_proxy", cancelled)
    panel.generate.click()
    qtbot.waitUntil(lambda: panel.worker is None)
    assert "cancelled" in panel.state.text() and panel.path is None

    def failed(*args, **kwargs):
        raise ValueError("encoder unavailable")

    monkeypatch.setattr("stitch.workers.generate_proxy", failed)
    panel.generate.click()
    qtbot.waitUntil(lambda: panel.worker is None)
    assert "encoder unavailable" in panel.state.text() and panel.generate.isEnabled()


def test_window_marks_original_edits_and_close_waits_for_proxy(
    setup_preview,
    qtbot,
    monkeypatch,
    tmp_path,
):
    source, clip, paths, cached = setup_preview
    monkeypatch.setattr(
        "stitch.window.probe_tools", lambda: (ToolStatus("ffmpeg", "/bin/ffmpeg", version="test"),)
    )
    monkeypatch.setattr("stitch.sources.probe_media", lambda _: source.cache.metadata)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    window = MainWindow(paths)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.tools_ready)
    window.import_paths([source.path])
    qtbot.waitUntil(lambda: not window._importing)
    window.preview.mark_in.emit(1.25)
    window.preview.mark_out.emit(8.5)
    assert window.clip_editor.pending
    window.apply_edits()
    edited = window.document.project.clips[0]
    assert edited.in_point == 1.25 and edited.out_point == 8.5
    assert window.document.project.sources[0].path == source.path
    assert window.save_to_path(tmp_path / "project.json")
    started, release = threading.Event(), threading.Event()

    def generate(*args, cancel, **kwargs):
        started.set()
        release.wait(timeout=3)
        assert cancel.is_set()
        raise RenderCancelled("cancelled")

    monkeypatch.setattr("stitch.workers.generate_proxy", generate)
    window.preview.start_generation()
    try:
        qtbot.waitUntil(started.is_set)
        window.close()
        assert window.isVisible() and window._close_after_proxy
        assert window.preview.worker.cancel.is_set()
    finally:
        release.set()
        qtbot.waitUntil(lambda: window.preview.worker is None)
    qtbot.waitUntil(lambda: not window.isVisible())
