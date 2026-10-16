"""Background imports return snapshots; only the GUI thread installs results."""

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from stitch.document import match_first_clip
from stitch.export import export_project
from stitch.folders import scan_folder
from stitch.models import Project, Source
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.proxy import ProxySettings, generate_proxy
from stitch.sources import add_video, set_overlay

logger = logging.getLogger(__name__)


class ProxyWorker(QThread):
    progress = Signal(float)

    def __init__(self, source: Source, settings: ProxySettings, paths: AppPaths, parent=None):
        super().__init__(parent)
        self.source = source
        self.settings = settings
        self.paths = paths
        self.cancel = threading.Event()
        self.result: Path | None = None
        self.error = ""
        self.cancelled = False

    def run(self) -> None:
        try:
            self.result = generate_proxy(
                self.source,
                self.settings,
                self.paths,
                cancel=self.cancel,
                progress=self.progress.emit,
            )
        except RenderCancelled:
            self.cancelled = True
        except Exception as exc:
            logger.exception("Proxy generation failed")
            self.error = str(exc)


class ImportWorker(QThread):
    progress = Signal(int, int, str)
    discovery = Signal(str)

    def __init__(
        self,
        project: Project,
        paths: list[Path],
        match_output: bool,
        parent=None,
        *,
        is_overlay: bool = False,
        folder: Path | None = None,
        excluded_roots: tuple[Path, ...] = (),
    ):
        super().__init__(parent)
        self.project = project
        self.paths = paths
        self.match_output = match_output
        self.errors: list[str] = []
        self.imported = 0
        self.is_overlay = is_overlay
        self.folder = folder
        self.excluded_roots = excluded_roots
        self.skipped = 0
        self.cancelled = False

    def run(self) -> None:
        if self.folder is not None:
            try:
                result = scan_folder(
                    self.project,
                    self.folder,
                    excluded_roots=self.excluded_roots,
                    cancelled=self.isInterruptionRequested,
                    progress=self.progress.emit,
                    discovery=self.discovery.emit,
                    match_output=self.match_output,
                )
                self.project = result.project
                self.imported = result.imported
                self.skipped = result.skipped
                self.cancelled = result.cancelled
                self.errors = result.errors
            except Exception as exc:
                logger.exception("Folder import failed")
                self.errors.append(str(exc))
            return
        for index, path in enumerate(self.paths):
            if self.isInterruptionRequested():
                break
            self.progress.emit(index + 1, len(self.paths), path.name)
            try:
                updated = (
                    set_overlay(self.project, path)
                    if self.is_overlay
                    else add_video(self.project, path)
                )
                if self.is_overlay and self.isInterruptionRequested():
                    break  # Cancelling replacement leaves the existing overlay intact.
                if self.match_output and self.imported == 0 and not self.is_overlay:
                    updated = match_first_clip(updated)
                self.project = updated
                self.imported += 1
            except Exception as exc:
                logger.exception("Import failed for %s", path)
                self.errors.append(f"{path.name}: {exc}")
        self.cancelled = self.isInterruptionRequested()


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
