"""Platform-specific storage, independent of the current working directory."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from platformdirs import PlatformDirs


@dataclass(frozen=True)
class AppPaths:
    projects: Path
    proxies: Path
    jobs: Path
    logs: Path

    @classmethod
    def default(cls) -> "AppPaths":
        dirs = PlatformDirs("Stitch", appauthor=False)
        return cls(
            projects=dirs.user_data_path / "projects",
            proxies=dirs.user_cache_path / "proxies",
            jobs=dirs.user_cache_path / "jobs",
            logs=dirs.user_log_path,
        )

    def ensure(self) -> None:
        """Create directories without clearing existing data; propagate I/O errors."""
        for path in (self.projects, self.proxies, self.jobs, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def new_job(self) -> Path:
        """Reserve an isolated workspace for one future export."""
        path = self.jobs / uuid4().hex
        path.mkdir(parents=True, exist_ok=False)
        return path
