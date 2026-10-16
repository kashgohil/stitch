"""Background imports return snapshots; only the GUI thread installs results."""

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from stitch.document import match_first_clip
from stitch.models import Project
from stitch.sources import add_video

logger = logging.getLogger(__name__)


class ImportWorker(QThread):
    progress = Signal(int, int, str)

    def __init__(self, project: Project, paths: list[Path], match_output: bool, parent=None):
        super().__init__(parent)
        self.project = project
        self.paths = paths
        self.match_output = match_output
        self.errors: list[str] = []
        self.imported = 0

    def run(self) -> None:
        for index, path in enumerate(self.paths):
            if self.isInterruptionRequested():
                break
            self.progress.emit(index + 1, len(self.paths), path.name)
            try:
                updated = add_video(self.project, path)
                if self.match_output and self.imported == 0:
                    updated = match_first_clip(updated)
                self.project = updated
                self.imported += 1
            except Exception as exc:
                logger.exception("Import failed for %s", path)
                self.errors.append(f"{path.name}: {exc}")
