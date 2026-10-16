"""Background imports return snapshots; only the GUI thread installs results."""

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from stitch.document import match_first_clip
from stitch.export import export_project
from stitch.models import Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
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


class ExportWorker(QThread):
    progress = Signal(object)

    def __init__(
        self,
        project: Project,
        destination: Path,
        paths: AppPaths,
        overwrite: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.project = project
        self.destination = destination
        self.paths = paths
        self.overwrite = overwrite
        self.cancel = threading.Event()
        self.result = None
        self.error = ""
        self.cancelled = False

    def run(self) -> None:
        try:
            self.result = export_project(
                self.project,
                self.destination,
                self.paths,
                overwrite=self.overwrite,
                cancel=self.cancel,
                progress=self.progress.emit,
            )
        except RenderCancelled as exc:
            self.cancelled = True
            self.error = str(exc)
        except Exception as exc:
            logger.exception("Export failed")
            self.error = str(exc)
