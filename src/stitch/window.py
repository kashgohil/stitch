"""Desktop shell. Media operations will be added after the startup gate."""

import logging

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stitch.dependencies import ToolStatus, installation_help, probe_tools
from stitch.paths import AppPaths

logger = logging.getLogger(__name__)


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
        self.tools_ready = False
        self._closing = False
        self._worker: DependencyWorker | None = None
        self.setWindowTitle("Stitch")
        self.resize(960, 640)
        self.setMinimumSize(640, 480)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(16)
        title = QLabel("Stitch")
        font = title.font()
        font.setPointSize(28)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        subtitle = QLabel("Bring your clips together.")
        layout.addWidget(subtitle)

        empty = QGroupBox("Your sequence")
        empty_layout = QVBoxLayout(empty)
        message = QLabel("No clips yet\n\nClip import and editing are coming in the next build.")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message.setWordWrap(True)
        empty_layout.addWidget(message)
        layout.addWidget(empty, 1)

        checks = QGroupBox("Setup")
        checks_layout = QVBoxLayout(checks)
        self.status = QLabel("Checking media tools…")
        self.status.setWordWrap(True)
        checks_layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(110)
        self.details.setAccessibleName("Media tool details")
        checks_layout.addWidget(self.details)
        self.help = QLabel(installation_help())
        self.help.setWordWrap(True)
        self.help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.help.hide()
        checks_layout.addWidget(self.help)
        buttons = QHBoxLayout()
        self.retry = QPushButton("Check again")
        self.retry.setEnabled(False)
        self.retry.clicked.connect(self.check_dependencies)
        buttons.addWidget(self.retry)
        logs = QPushButton("Open logs")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs))))
        buttons.addWidget(logs)
        buttons.addStretch()
        checks_layout.addLayout(buttons)
        layout.addWidget(checks)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Checking setup")
        QTimer.singleShot(0, self.check_dependencies)

    @Slot()
    def check_dependencies(self) -> None:
        if self._closing or (self._worker is not None and self._worker.isRunning()):
            return
        self.tools_ready = False
        self.retry.setEnabled(False)
        self.status.setText("Checking media tools…")
        self.statusBar().showMessage("Checking setup")
        self.help.hide()
        self.details.clear()
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = DependencyWorker(self)
        self._worker.finished.connect(self._checks_finished)
        self._worker.start()

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
        self.retry.setEnabled(True)
        self.statusBar().showMessage(
            "Setup ready" if self.tools_ready else "Media work unavailable"
        )
        for tool in results:
            logger.info("%s: %s (%s)", tool.name, tool.version or tool.error, tool.path)
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None and self._worker.isRunning():
            # Let bounded subprocess checks finish before destroying their Qt thread.
            self._closing = True
            self.hide()
            event.ignore()
            return
        event.accept()
