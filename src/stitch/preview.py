"""Selected-source inspection. This player never changes project source paths."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from stitch.models import Clip, Source
from stitch.paths import AppPaths
from stitch.proxy import ProxySettings, cached_proxy
from stitch.sources import source_status
from stitch.workers import ProxyWorker


def time_label(milliseconds: int) -> str:
    seconds, ms = divmod(max(0, milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{ms:03}"


class PreviewPanel(QWidget):
    idle = Signal()
    mark_in = Signal(float)
    mark_out = Signal(float)

    def __init__(self, paths: AppPaths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.source: Source | None = None
        self.clip: Clip | None = None
        self.path: Path | None = None
        self.worker: ProxyWorker | None = None
        self.available = False
        self._loaded = False
        self._duration = 0
        layout = QVBoxLayout(self)
        self.state = QLabel("Select a clip to inspect it.")
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        row = QHBoxLayout()
        self.quality = QComboBox()
        self.quality.addItem("720p · 30 fps", 720)
        self.quality.addItem("1080p · 30 fps", 1080)
        self.quality.setAccessibleName("Proxy quality")
        self.quality.currentIndexChanged.connect(self._quality_changed)
        row.addWidget(self.quality)
        self.generate = QPushButton("Generate proxy")
        self.generate.clicked.connect(self.start_generation)
        row.addWidget(self.generate)
        self.cancel = QPushButton("Cancel proxy")
        self.cancel.clicked.connect(self.stop_generation)
        row.addWidget(self.cancel)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setAccessibleName("Proxy generation progress")
        row.addWidget(self.progress, 1)
        layout.addLayout(row)
        self.video = QVideoWidget()
        self.video.setMinimumSize(240, 135)
        layout.addWidget(self.video, 1)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.5)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._position_changed)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.playbackStateChanged.connect(self._playback_state)
        self.player.errorOccurred.connect(self._playback_error)
        self.player.seekableChanged.connect(lambda _: self._controls())
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Original source time")
        self.seek.setTracking(False)
        self.seek.valueChanged.connect(self.seek_to)
        layout.addWidget(self.seek)
        controls = QHBoxLayout()
        self.play = QPushButton("Play clip")
        self.play.clicked.connect(self.toggle_play)
        controls.addWidget(self.play)
        self.mute = QCheckBox("Mute preview")
        self.mute.toggled.connect(self.audio.setMuted)
        controls.addWidget(self.mute)
        self.time = QLabel(time_label(0))
        controls.addWidget(self.time)
        controls.addStretch()
        self.set_in = QPushButton("Set in")
        self.set_out = QPushButton("Set out")
        self.set_in.clicked.connect(lambda: self.mark_in.emit(self.player.position() / 1000))
        self.set_out.clicked.connect(lambda: self.mark_out.emit(self.player.position() / 1000))
        controls.addWidget(self.set_in)
        controls.addWidget(self.set_out)
        layout.addLayout(controls)
        self.note = QLabel(
            "Source inspection only: crop, gain and overlay effects are not previewed. "
            "Time is relative to the original video; seeking is not frame-accurate."
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        # A changed/deleted original invalidates a loaded proxy too, not just the next lookup.
        self.freshness = QTimer(self)
        self.freshness.setInterval(2000)
        self.freshness.timeout.connect(self._check_freshness)
        self.freshness.start()
        self._controls()

    @property
    def settings(self) -> ProxySettings:
        return ProxySettings(self.quality.currentData())

    def set_available(self, available: bool) -> None:
        self.available = available
        if not available:
            self.player.pause()
            self.stop_generation()
        self._controls()

    def _clear_player(self) -> None:
        self.path = None
        self._loaded = False
        self.player.stop()
        self.player.setSource(QUrl())

    def load(self, clip: Clip | None, source: Source | None) -> None:
        changed = source != self.source or clip != self.clip
        self.clip, self.source = clip, source
        if changed:
            self._clear_player()
        if self.worker:
            if source != self.worker.source or self.settings != self.worker.settings:
                self.stop_generation()
            self._controls()
            return
        self._resolve()

    def _quality_changed(self) -> None:
        self._clear_player()
        self.load(self.clip, self.source)

    def _resolve(self) -> None:
        self._duration = 0
        if not self.source or not self.clip:
            self.state.setText("Select a clip to inspect it.")
            self._controls()
            return
        status = source_status(self.source)
        if status.state != "ready":
            self._clear_player()
            self.state.setText(
                f"Source {status.state}. Reimport the original to refresh its proxy."
            )
            self._controls()
            return
        metadata = self.source.cache.metadata
        self._duration = round((metadata.video.duration or metadata.duration) * 1000)
        self.seek.setRange(0, self._duration)
        path = cached_proxy(self.source, self.settings, self.paths)
        if path:
            self.state.setText(f"Ready · {self.settings.height}p proxy · {self.source.path.name}")
            if path != self.path:
                self.path = path
                self._loaded = False
                self.player.setSource(QUrl.fromLocalFile(str(path)))
        else:
            self._clear_player()
            self.state.setText(f"No valid {self.settings.height}p proxy · {self.source.path.name}")
        self._controls()

    def _controls(self) -> None:
        running = self.worker is not None
        ready = bool(self.path and self._loaded and self.available and not running)
        self.generate.setEnabled(bool(self.source and self.available and not running))
        self.cancel.setVisible(running)
        self.cancel.setEnabled(running and not self.worker.cancel.is_set())
        self.progress.setVisible(running)
        self.play.setEnabled(ready)
        seekable = ready and self.player.isSeekable()
        self.seek.setEnabled(seekable)
        self.set_in.setEnabled(seekable)
        self.set_out.setEnabled(seekable)

    def start_generation(self) -> None:
        if self.worker or not self.source or not self.available:
            return
        self._clear_player()
        self.worker = ProxyWorker(self.source, self.settings, self.paths, self)
        self.worker.progress.connect(self._generation_progress)
        self.worker.finished.connect(self._generation_finished)
        self.state.setText(f"Generating {self.settings.height}p proxy · {self.source.path.name}")
        self.progress.setValue(0)
        self._controls()
        self.worker.start()

    def _generation_progress(self, fraction: float) -> None:
        if self.worker and not self.worker.cancel.is_set():
            self.progress.setValue(round(fraction * 1000))

    def stop_generation(self) -> None:
        if self.worker:
            self.worker.cancel.set()
            self.state.setText("Cancelling proxy; waiting for media work to stop…")
            self._controls()

    def _generation_finished(self) -> None:
        worker, self.worker = self.worker, None
        same = worker.source == self.source and worker.settings == self.settings
        self._resolve()
        if same and (worker.cancelled or worker.cancel.is_set()):
            self.state.setText("Proxy generation cancelled. Generate again when ready.")
        elif same and worker.error:
            self.state.setText(worker.error)
        worker.deleteLater()
        self.idle.emit()

    def _check_freshness(self) -> bool:
        if (
            self.path
            and self.source
            and cached_proxy(self.source, self.settings, self.paths) != self.path
        ):
            self._resolve()
            return False
        return True

    def _media_status(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if not self._loaded and self.clip:
                self._loaded = True
                self.player.setPosition(round(self.clip.in_point * 1000))
            self._controls()

    def _playback_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError and self.path:
            self._loaded = False
            self.state.setText(
                f"Preview playback failed: {message}. "
                "Try another proxy quality or check the Qt playback backend."
            )
            self._controls()

    def _playback_state(self, state) -> None:
        self.play.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play clip"
        )

    def toggle_play(self) -> None:
        if (
            not self.available
            or not self.path
            or not self._loaded
            or not self.clip
            or not self._check_freshness()
        ):
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            return
        start = round(self.clip.in_point * 1000)
        end = (
            round(self.clip.out_point * 1000) if self.clip.out_point is not None else self._duration
        )
        if not start <= self.player.position() < end:
            self.player.setPosition(start)
        self.player.play()

    def seek_to(self, position: int) -> None:
        if self.available and self.path and self._loaded:
            self.player.pause()
            self.player.setPosition(min(position, max(0, self._duration - 1)))

    def _position_changed(self, position: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.blockSignals(True)
            self.seek.setValue(position)
            self.seek.blockSignals(False)
        self.time.setText(time_label(position))
        if self.clip:
            end = (
                round(self.clip.out_point * 1000)
                if self.clip.out_point is not None
                else self._duration
            )
            if (
                position >= end
                and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            ):
                self.player.pause()
                self.player.setPosition(end)

    def shutdown(self) -> bool:
        self._clear_player()
        self.stop_generation()
        return self.worker is None
