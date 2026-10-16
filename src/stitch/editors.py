"""Small draft editors; changes become project data only after validation."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from stitch.models import AudioSettings, Clip, Crop, OutputSettings, Project, Source


def integer_box(minimum: int, maximum: int = 131072) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setKeyboardTracking(True)
    return widget


def seconds_box() -> QDoubleSpinBox:
    widget = QDoubleSpinBox()
    widget.setDecimals(6)
    widget.setRange(0, 1_000_000_000)
    widget.setSuffix(" s")
    widget.setKeyboardTracking(True)
    return widget


class ClipEditor(QGroupBox):
    changed = Signal()
    apply_requested = Signal()
    reset_requested = Signal()

    def __init__(self) -> None:
        super().__init__("Selected clip")
        self.loading = False
        self.pending = False
        self.clip: Clip | None = None
        layout = QVBoxLayout(self)
        self.metadata = QLabel("Select a clip to edit its trim, crop, and audio.")
        self.metadata.setWordWrap(True)
        layout.addWidget(self.metadata)
        self.controls = QWidget()
        form = QFormLayout(self.controls)
        self.start = seconds_box()
        self.end = seconds_box()
        self.source_end = QCheckBox("Use source end")
        self.crop_enabled = QCheckBox("Crop this clip")
        self.keep_audio = QCheckBox("Keep source audio")
        form.addRow("In-point", self.start)
        form.addRow("Out-point", self.end)
        form.addRow(self.source_end)
        form.addRow(self.crop_enabled)
        self.crop_controls = QWidget()
        crop_form = QFormLayout(self.crop_controls)
        crop_form.setContentsMargins(0, 0, 0, 0)
        self.x, self.y = integer_box(0), integer_box(0)
        self.width, self.height = integer_box(1), integer_box(1)
        for title, widget in (
            ("Left (px)", self.x),
            ("Top (px)", self.y),
            ("Width (px)", self.width),
            ("Height (px)", self.height),
        ):
            crop_form.addRow(title, widget)
        form.addRow(self.crop_controls)
        form.addRow(self.keep_audio)
        layout.addWidget(self.controls)
        buttons = QHBoxLayout()
        self.apply = QPushButton("Apply clip changes")
        self.reset = QPushButton("Reset edits")
        self.apply.clicked.connect(self.apply_requested)
        self.reset.clicked.connect(self.reset_requested)
        buttons.addWidget(self.apply)
        buttons.addWidget(self.reset)
        layout.addLayout(buttons)
        layout.addStretch()
        for widget in (self.start, self.end, self.x, self.y, self.width, self.height):
            widget.valueChanged.connect(self._changed)
        for widget in (self.source_end, self.crop_enabled, self.keep_audio):
            widget.toggled.connect(self._changed)
        self.source_end.toggled.connect(lambda checked: self.end.setEnabled(not checked))
        self.crop_enabled.toggled.connect(self.crop_controls.setEnabled)
        self.load(None, None)

    def _changed(self, *args) -> None:
        if not self.loading:
            self.pending = True
            self.changed.emit()

    def load(self, clip: Clip | None, source: Source | None) -> None:
        self.loading = True
        self.clip = clip
        video = source.cache.metadata.video if source and source.cache else None
        editable = clip is not None and video is not None
        self.controls.setEnabled(editable)
        self.apply.setEnabled(editable)
        self.reset.setEnabled(editable)
        if clip and source and video:
            metadata = source.cache.metadata
            width, height = video.display_dimensions
            self.metadata.setText(
                f"{source.path.name}\n{width} × {height} · {video.codec} · "
                f"{video.average_frame_rate or video.real_frame_rate or 'Unknown'} fps\n"
                f"{video.duration or metadata.duration:.6f} s · "
                f"{str(video.bit_depth) + '-bit' if video.bit_depth else 'Unknown bit depth'}"
            )
            self.start.setValue(clip.in_point)
            self.end.setValue(
                clip.out_point
                if clip.out_point is not None
                else video.duration or metadata.duration
            )
            self.source_end.setChecked(clip.out_point is None)
            self.end.setEnabled(clip.out_point is not None)
            self.crop_enabled.setChecked(clip.crop is not None)
            self.crop_controls.setEnabled(clip.crop is not None)
            crop = clip.crop or Crop(width=width, height=height)
            for widget, value in (
                (self.x, crop.x),
                (self.y, crop.y),
                (self.width, crop.width),
                (self.height, crop.height),
            ):
                widget.setValue(value)
            self.keep_audio.setChecked(clip.audio.enabled)
        else:
            self.metadata.setText(
                "No cached video metadata. Reimport this source to inspect it."
                if clip
                else "Select a clip to edit its trim, crop, and audio."
            )
        self.pending = False
        self.loading = False

    def value(self) -> Clip:
        crop = (
            Crop(
                x=self.x.value(),
                y=self.y.value(),
                width=self.width.value(),
                height=self.height.value(),
            )
            if self.crop_enabled.isChecked()
            else None
        )
        return Clip(
            id=self.clip.id,
            source_id=self.clip.source_id,
            in_point=self.start.value(),
            out_point=None if self.source_end.isChecked() else self.end.value(),
            crop=crop,
            audio=AudioSettings(
                enabled=self.keep_audio.isChecked(), gain_db=self.clip.audio.gain_db
            ),
        )


class OutputEditor(QGroupBox):
    changed = Signal()
    apply_requested = Signal()
    reset_requested = Signal()

    def __init__(self) -> None:
        super().__init__("Project output")
        self.loading = False
        self.pending = False
        layout = QHBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow("Project name", self.name)
        layout.addLayout(form)
        dimensions = QFormLayout()
        self.width, self.height = integer_box(2), integer_box(2)
        dimensions.addRow("Width (px)", self.width)
        dimensions.addRow("Height (px)", self.height)
        layout.addLayout(dimensions)
        format_form = QFormLayout()
        self.fps = QComboBox()
        self.fps.setEditable(True)
        self.fps.addItems(
            [
                "24000/1001",
                "24/1",
                "25/1",
                "30000/1001",
                "30/1",
                "50/1",
                "60000/1001",
                "60/1",
                "120/1",
            ]
        )
        self.fps.setToolTip("Frames per second. Fractional rates remain exact, e.g. 60000/1001.")
        self.profile = QComboBox()
        for title, key in (
            ("HEVC 10-bit / AAC", "hevc_main10"),
            ("H.264 / AAC", "h264"),
            ("ProRes HQ / PCM", "prores_hq"),
        ):
            self.profile.addItem(title, key)
        format_form.addRow("Frame rate", self.fps)
        format_form.addRow("Profile", self.profile)
        layout.addLayout(format_form)
        actions = QVBoxLayout()
        self.upscale = QCheckBox("Allow upscaling")
        self.apply = QPushButton("Apply output")
        self.reset = QPushButton("Reset output edits")
        self.apply.clicked.connect(self.apply_requested)
        self.reset.clicked.connect(self.reset_requested)
        actions.addWidget(self.upscale)
        actions.addWidget(self.apply)
        actions.addWidget(self.reset)
        layout.addLayout(actions)
        self.name.textChanged.connect(self._changed)
        self.width.valueChanged.connect(self._changed)
        self.height.valueChanged.connect(self._changed)
        self.fps.currentTextChanged.connect(self._changed)
        self.profile.currentIndexChanged.connect(self._changed)
        self.upscale.toggled.connect(self._changed)

    def _changed(self, *args) -> None:
        if not self.loading:
            self.pending = True
            self.changed.emit()

    def load(self, project: Project) -> None:
        self.loading = True
        self.name.setText(project.name)
        self.width.setValue(project.output.width)
        self.height.setValue(project.output.height)
        self.fps.setCurrentText(project.output.frame_rate)
        self.profile.setCurrentIndex(self.profile.findData(project.output.profile))
        self.upscale.setChecked(project.output.allow_upscale)
        self.pending = False
        self.loading = False

    def value(self) -> tuple[str, OutputSettings]:
        return self.name.text().strip() or "Untitled", OutputSettings(
            width=self.width.value(),
            height=self.height.value(),
            frame_rate=self.fps.currentText().strip(),
            profile=self.profile.currentData(),
            allow_upscale=self.upscale.isChecked(),
        )
