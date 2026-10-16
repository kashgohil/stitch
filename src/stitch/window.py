"""Desktop project controls; media inspection runs outside the GUI thread."""

import logging
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stitch.dependencies import ToolStatus, installation_help, probe_tools
from stitch.document import Document, change_project, move_clip, remove_clip, replace_clip
from stitch.editors import ClipEditor, OutputEditor
from stitch.paths import AppPaths
from stitch.project_io import ProjectError
from stitch.sources import source_status
from stitch.workers import ExportWorker, ImportWorker

logger = logging.getLogger(__name__)


def readable_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "\n".join(item["msg"].removeprefix("Value error, ") for item in error.errors())
    return str(error)


class DependencyWorker(QThread):
    results: tuple[ToolStatus, ...] = ()

    def run(self) -> None:
        try:
            self.results = probe_tools()
        except Exception:
            logger.exception("Dependency check failed")
            self.results = (ToolStatus("Media tools", None, error="Check failed. See the log."),)


class MainWindow(QMainWindow):
    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self.paths = paths
        self.document = Document()
        self.tools_ready = False
        self._closing = False
        self._close_after_import = False
        self._worker: DependencyWorker | None = None
        self._import_worker: ImportWorker | None = None
        self._importing = False
        self._exporting = False
        self._export_worker: ExportWorker | None = None
        self._close_after_export = False
        self._refreshing = False
        self._selected: UUID | None = None
        self.resize(1200, 850)
        self.setMinimumSize(1000, 740)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        toolbar = QHBoxLayout()
        title = QLabel("Stitch")
        font = title.font()
        font.setPointSize(26)
        font.setBold(True)
        title.setFont(font)
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.new = QPushButton("New")
        self.open = QPushButton("Open…")
        self.save = QPushButton("Save")
        self.save_as = QPushButton("Save as…")
        self.add = QPushButton("Add videos…")
        self.export = QPushButton("Export…")
        self.cancel_import = QPushButton("Stop import")
        for button, callback in (
            (self.new, self.new_project),
            (self.open, self.choose_project),
            (self.save, self.save_current),
            (self.save_as, self.choose_save_as),
            (self.add, self.choose_videos),
            (self.export, self.choose_export),
            (self.cancel_import, self.stop_import),
        ):
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.error = QPlainTextEdit()
        self.error.setReadOnly(True)
        self.error.setMaximumHeight(80)
        self.error.setAccessibleName("Editing errors")
        self.error.hide()
        layout.addWidget(self.error)
        splitter = QSplitter()
        self.sequence = QGroupBox("Your sequence")
        sequence_layout = QVBoxLayout(self.sequence)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Clip", "Length", "Video", "Audio", "Source"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(self._selection_changed)
        sequence_layout.addWidget(self.table)
        self.empty = QLabel("Add videos to build your sequence. Preview is coming next.")
        self.empty.setWordWrap(True)
        sequence_layout.addWidget(self.empty)
        ordering = QHBoxLayout()
        self.up, self.down, self.remove = (
            QPushButton("Move up"),
            QPushButton("Move down"),
            QPushButton("Remove clip"),
        )
        self.up.clicked.connect(lambda: self.move_selected(-1))
        self.down.clicked.connect(lambda: self.move_selected(1))
        self.remove.clicked.connect(self.remove_selected)
        for button in (self.up, self.down, self.remove):
            ordering.addWidget(button)
        ordering.addStretch()
        sequence_layout.addLayout(ordering)
        splitter.addWidget(self.sequence)
        self.clip_editor = ClipEditor()
        self.clip_editor.apply_requested.connect(self.apply_edits)
        self.clip_editor.reset_requested.connect(self._load_selected)
        self.clip_editor.changed.connect(self._update_title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.clip_editor)
        scroll.setMinimumWidth(300)
        splitter.addWidget(scroll)
        splitter.setSizes([760, 360])
        layout.addWidget(splitter, 1)
        self.output_editor = OutputEditor()
        self.output_editor.apply_requested.connect(self.apply_edits)
        self.output_editor.reset_requested.connect(self._reset_output)
        self.output_editor.changed.connect(self._update_title)
        layout.addWidget(self.output_editor)
        self.export_panel = QGroupBox("Export")
        export_layout = QVBoxLayout(self.export_panel)
        self.export_status = QLabel()
        self.export_status.setWordWrap(True)
        export_layout.addWidget(self.export_status)
        export_row = QHBoxLayout()
        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 1000)
        self.export_progress.setAccessibleName("Whole export progress")
        export_row.addWidget(self.export_progress, 1)
        self.cancel_export = QPushButton("Cancel export")
        self.cancel_export.clicked.connect(self.stop_export)
        export_row.addWidget(self.cancel_export)
        export_layout.addLayout(export_row)
        self.export_panel.hide()
        layout.addWidget(self.export_panel)
        checks = QGroupBox("Media tools")
        checks_layout = QVBoxLayout(checks)
        buttons = QHBoxLayout()
        self.status = QLabel("Checking media tools…")
        buttons.addWidget(self.status, 1)
        self.retry = QPushButton("Check again")
        self.retry.clicked.connect(self.check_dependencies)
        buttons.addWidget(self.retry)
        logs = QPushButton("Open logs")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs))))
        buttons.addWidget(logs)
        checks_layout.addLayout(buttons)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(64)
        self.details.setAccessibleName("Media tool details")
        self.details.hide()
        checks_layout.addWidget(self.details)
        self.help = QLabel(installation_help())
        self.help.setWordWrap(True)
        self.help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.help.hide()
        checks_layout.addWidget(self.help)
        layout.addWidget(checks)
        self.setCentralWidget(root)
        menu = self.menuBar().addMenu("File")
        self.file_actions = []
        for title, shortcut, callback in (
            ("New", QKeySequence.StandardKey.New, self.new_project),
            ("Open…", QKeySequence.StandardKey.Open, self.choose_project),
            ("Save", QKeySequence.StandardKey.Save, self.save_current),
            ("Save as…", QKeySequence.StandardKey.SaveAs, self.choose_save_as),
        ):
            action = QAction(title, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            menu.addAction(action)
            self.file_actions.append(action)
        self._refresh()
        QTimer.singleShot(0, self.check_dependencies)

    def _show_error(self, message: str) -> None:
        self.error.setPlainText(message)
        self.error.setVisible(bool(message))

    def _update_title(self) -> None:
        dirty = self.document.dirty or self.clip_editor.pending or self.output_editor.pending
        self.setWindowTitle(f"{self.document.project.name}{' *' if dirty else ''} — Stitch")

    def _update_controls(self) -> None:
        idle = not self.busy
        for widget in (
            self.new,
            self.open,
            self.save,
            self.save_as,
            self.sequence,
            self.clip_editor,
            self.output_editor,
        ):
            widget.setEnabled(idle)
        for action in self.file_actions:
            action.setEnabled(idle)
        self.add.setEnabled(idle and self.tools_ready)
        self.export.setEnabled(idle and self.tools_ready and bool(self.document.project.clips))
        self.retry.setEnabled(idle and self._worker is not None and not self._worker.isRunning())
        self.cancel_import.setVisible(self._importing)
        row = self.table.currentRow()
        self.up.setEnabled(idle and row > 0)
        self.down.setEnabled(idle and 0 <= row < self.table.rowCount() - 1)
        self.remove.setEnabled(idle and row >= 0)

    def _refresh(self, selected: UUID | None = None) -> None:
        self._refreshing = True
        project = self.document.project
        sources = {source.id: source for source in project.sources}
        states = {source.id: source_status(source).state for source in project.sources}
        self.table.setRowCount(len(project.clips))
        total = 0.0
        unknown = False
        selected_row = 0
        for row, clip in enumerate(project.clips):
            source = sources[clip.source_id]
            metadata = source.cache.metadata if source.cache else None
            video = metadata.video if metadata else None
            end = clip.out_point
            if end is None and metadata:
                end = (video.duration if video else None) or metadata.duration
            duration = end - clip.in_point if end is not None else None
            total += duration or 0
            unknown |= duration is None
            values = [
                source.path.name,
                f"{duration:.3f} s" if duration is not None else "Unknown",
                f"{video.display_dimensions[0]} × {video.display_dimensions[1]}\n"
                f"{video.average_frame_rate or video.real_frame_rate or '?'} fps"
                if video
                else "Unknown",
                "Muted"
                if not clip.audio.enabled
                else "Yes"
                if metadata and metadata.audio
                else "None",
                states[source.id],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, clip.id)
                item.setToolTip(str(source.path))
                self.table.setItem(row, column, item)
            if clip.id == selected:
                selected_row = row
        self.table.resizeRowsToContents()
        self._selected = project.clips[selected_row].id if project.clips else None
        if project.clips:
            self.table.setCurrentCell(selected_row, 0)
        self.empty.setVisible(not project.clips)
        self.summary.setText(
            f"{len(project.clips)} clips · {'At least ' if unknown else ''}"
            f"{total / 60:.2f} min · Preview not yet available"
        )
        self._refreshing = False
        self._load_selected()
        self.output_editor.load(project)
        self._update_controls()
        self._update_title()

    def _load_selected(self) -> None:
        project = self.document.project
        clip = next((clip for clip in project.clips if clip.id == self._selected), None)
        source = next((s for s in project.sources if clip and s.id == clip.source_id), None)
        self.clip_editor.load(clip, source)
        self._update_title()

    def _reset_output(self) -> None:
        self.output_editor.load(self.document.project)
        self._update_title()

    def _apply_pending(self) -> bool:
        project = self.document.project
        try:
            if self.clip_editor.pending:
                project = replace_clip(project, self.clip_editor.value())
            if self.output_editor.pending:
                name, output = self.output_editor.value()
                project = change_project(project, name=name, output=output)
        except (ValidationError, ProjectError) as exc:
            self._show_error(readable_error(exc))
            return False
        if project.output != self.document.project.output:
            self.document.output_configured = True
        self.document.project = project
        self.clip_editor.pending = self.output_editor.pending = False
        self._show_error("")
        self._update_title()
        return True

    @Slot()
    def apply_edits(self) -> None:
        if not self.busy and self._apply_pending():
            self._refresh(self._selected)

    def _selection_changed(self, row: int, column: int, old_row: int, old_column: int) -> None:
        if self._refreshing or row < 0:
            return
        selected = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if selected == self._selected:
            return
        if not self._apply_pending():
            self._refreshing = True
            self.table.setCurrentCell(old_row, max(old_column, 0))
            self._refreshing = False
            return
        self._refresh(selected)

    def move_selected(self, offset: int) -> None:
        if not self.busy and self._selected and self._apply_pending():
            self.document.project = move_clip(self.document.project, self._selected, offset)
            self._refresh(self._selected)

    @Slot()
    def remove_selected(self) -> None:
        if self.busy or not self._selected:
            return
        pending = self.clip_editor.pending
        self.clip_editor.pending = False  # Removing a clip also discards its draft edits.
        if not self._apply_pending():
            self.clip_editor.pending = pending
            return
        self.document.project = remove_clip(self.document.project, self._selected)
        self._refresh()

    @Slot()
    def choose_videos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add videos", "", "All files (*)")
        self.import_paths([Path(path) for path in paths])

    def import_paths(self, paths: list[Path]) -> None:
        if self.busy or not self.tools_ready or not paths or not self._apply_pending():
            return
        if self._import_worker:
            self._import_worker.deleteLater()
        self._importing = True
        self.cancel_import.setEnabled(True)
        self.cancel_import.setText("Stop import")
        self._import_worker = ImportWorker(
            self.document.project,
            paths,
            not self.document.project.clips and not self.document.output_configured,
            self,
        )
        self._import_worker.progress.connect(self._import_progress)
        self._import_worker.finished.connect(self._import_finished)
        self._update_controls()
        self._import_worker.start()

    @Slot(int, int, str)
    def _import_progress(self, current: int, total: int, name: str) -> None:
        self.statusBar().showMessage(f"Inspecting {current}/{total}: {name}")

    @Slot()
    def stop_import(self) -> None:
        if self._importing:
            self._import_worker.requestInterruption()
            self.cancel_import.setEnabled(False)
            self.cancel_import.setText("Finishing current file…")
            self.statusBar().showMessage("Stopping after current inspection (up to 30 seconds).")

    @Slot()
    def _import_finished(self) -> None:
        worker = self._import_worker
        self._importing = False
        self.document.project = worker.project
        if worker.imported:
            self.document.output_configured = True
        self._refresh(worker.project.clips[-1].id if worker.project.clips else None)
        self._show_error("\n".join(worker.errors))
        self.statusBar().showMessage(
            f"Imported {worker.imported} clips. {len(worker.errors)} failed."
        )
        if self._close_after_import:
            self._close_after_import = False
            self.close()

    def _confirm_replace(self) -> bool:
        if not (self.document.dirty or self.clip_editor.pending or self.output_editor.pending):
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved project",
            "Save changes before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            return self.save_current()
        return choice == QMessageBox.StandardButton.Discard

    @Slot()
    def new_project(self) -> None:
        if not self.busy and self._confirm_replace():
            self.document = Document()
            self._refresh()
            self._show_error("")

    @Slot()
    def choose_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            str(self.paths.projects),
            "Stitch projects (*.json);;All files (*)",
        )
        if path:
            self.open_path(Path(path))

    def open_path(self, path: Path) -> bool:
        if self.busy or not self._confirm_replace():
            return False
        try:
            self.document.open(path)
        except ProjectError as exc:
            self._show_error(str(exc))
            return False
        self._refresh()
        self._show_error("")
        return True

    @Slot()
    def save_current(self) -> bool:
        if self.busy:
            return False
        return (
            self.save_to_path(self.document.path) if self.document.path else self.choose_save_as()
        )

    @Slot()
    def choose_save_as(self) -> bool:
        if self.busy:
            return False
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project",
            str(self.document.path or self.paths.projects / "Untitled.stitch.json"),
            "Stitch projects (*.json);;All files (*)",
        )
        return self.save_to_path(Path(path)) if path else False

    def save_to_path(self, path: Path) -> bool:
        if self.busy or not self._apply_pending():
            return False
        try:
            self.document.save(path)
        except ProjectError as exc:
            self._show_error(str(exc))
            return False
        self._refresh(self._selected)
        self.statusBar().showMessage(f"Saved {path.name}")
        return True

    @Slot()
    def check_dependencies(self) -> None:
        if self._closing or self.busy or (self._worker and self._worker.isRunning()):
            return
        self.tools_ready = False
        self.status.setText("Checking media tools…")
        self.help.hide()
        self.details.hide()
        if self._worker:
            self._worker.deleteLater()
        self._worker = DependencyWorker(self)
        self._worker.finished.connect(self._checks_finished)
        self._worker.start()
        self._update_controls()

    @Slot()
    def _checks_finished(self) -> None:
        results = self._worker.results
        self.tools_ready = bool(results) and all(tool.available for tool in results)
        self.status.setText(
            "Media tools are ready." if self.tools_ready else "Media setup needs attention."
        )
        self.details.setPlainText(
            "\n\n".join(
                f"{tool.version or tool.error}\n{tool.path or 'No executable found'}"
                for tool in results
            )
        )
        self.help.setVisible(not self.tools_ready)
        self.details.setVisible(not self.tools_ready)
        self._update_controls()
        self.statusBar().showMessage(
            "Setup ready" if self.tools_ready else "Media work unavailable"
        )
        for tool in results:
            logger.info("%s: %s (%s)", tool.name, tool.version or tool.error, tool.path)
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._exporting:
            self._close_after_export = True
            self.stop_export()
            event.ignore()
            return
        if self._importing:
            self._close_after_import = True
            self.stop_import()
            event.ignore()
            return
        if not self._closing and not self._confirm_replace():
            event.ignore()
            return
        if self._worker and self._worker.isRunning():
            self._closing = True
            self.hide()
            event.ignore()
            return
        event.accept()

    @property
    def busy(self) -> bool:
        return self._importing or self._exporting

    @Slot()
    def choose_export(self) -> None:
        if self.busy or not self._apply_pending():
            return
        suffix = ".mov" if self.document.project.output.profile == "prores_hq" else ".mp4"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export sequence",
            str(self.paths.projects / ("Untitled" + suffix)),
            f"Video (*{suffix})",
        )
        if path:
            # The native save dialog has already asked to confirm replacement, if needed.
            self.start_export(Path(path), overwrite=Path(path).exists())

    def start_export(self, destination: Path, *, overwrite: bool = False) -> None:
        if self.busy or not self.tools_ready or not self._apply_pending():
            return
        if self.document.path and destination.resolve() == self.document.path.resolve():
            self._show_error("Export cannot overwrite the open project file.")
            return
        self._refresh(self._selected)
        if self._export_worker:
            self._export_worker.deleteLater()
        self._export_worker = ExportWorker(
            self.document.project, destination, self.paths, overwrite=overwrite, parent=self
        )
        self._export_worker.progress.connect(self._export_update)
        self._export_worker.finished.connect(self._export_finished)
        self._exporting = True
        self.export_panel.show()
        self.export_status.setText("Checking sources and disk space…")
        self.export_progress.setValue(0)
        self.cancel_export.setEnabled(True)
        self._update_controls()
        self._export_worker.start()

    @Slot(object)
    def _export_update(self, update) -> None:
        self.export_progress.setValue(round(update.fraction * 1000))
        detail = f" · Clip {update.clip_fraction:.0%}" if update.stage == "rendering" else ""
        self.export_status.setText(
            f"{update.message}{detail} · Overall {update.fraction:.0%} "
            f"· Elapsed {update.elapsed:.0f}s"
        )

    @Slot()
    def stop_export(self) -> None:
        if self._exporting:
            self._export_worker.cancel.set()
            self.cancel_export.setEnabled(False)
            self.export_status.setText("Cancelling export; waiting for active media work to stop…")

    @Slot()
    def _export_finished(self) -> None:
        worker = self._export_worker
        self._exporting = False
        self.cancel_export.setEnabled(False)
        self._update_controls()
        if worker.result:
            self.export_progress.setValue(1000)
            self.export_status.setText(f"Exported {worker.result.path}")
            self._show_error("\n".join(worker.result.warnings))
        else:
            self.export_status.setText(
                "Export cancelled." if worker.cancelled else "Export failed."
            )
            self._show_error(worker.error)
        if self._close_after_export:
            self._close_after_export = False
            self.close()
