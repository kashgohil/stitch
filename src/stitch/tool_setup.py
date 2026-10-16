"""GUI-selected external tools and Finder-safe startup; never bundle a shell."""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from stitch.paths import AppPaths

logger = logging.getLogger(__name__)


def _prepend(directory: Path) -> None:
    entries = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [str(directory), *(p for p in entries if p != str(directory))]
    )


def configure_media_tools(paths: AppPaths) -> None:
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        # Finder does not inherit the user's shell startup files. Do not run a login shell.
        entries = os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
        entries += [p for p in ("/opt/homebrew/bin", "/usr/local/bin") if p not in entries]
        os.environ["PATH"] = os.pathsep.join(entries)
    preference = paths.projects.parent / "media-tools.json"
    try:
        if preference.stat().st_size > 8192:
            raise ValueError("tool preference is too large")
        directory = Path(json.loads(preference.read_text(encoding="utf-8"))["directory"])
        if not directory.is_absolute():
            raise ValueError("tool folder must be absolute")
        if directory.is_dir():
            _prepend(directory)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Ignoring invalid media tool preferences: %s", exc)


def save_tool_directory(directory: Path, paths: AppPaths) -> None:
    directory = directory.expanduser().resolve()
    suffix = ".exe" if sys.platform == "win32" else ""
    for name in ("ffmpeg", "ffprobe"):
        candidate = directory / (name + suffix)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(
                f"Choose a folder containing executable {name}{suffix} and its companion tool."
            )
    preference = paths.projects.parent / "media-tools.json"
    preference.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=preference.parent, encoding="utf-8", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump({"directory": str(directory)}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, preference)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _prepend(directory)


def external_tool_environment() -> dict[str, str]:
    """Do not pass bundled library overrides to an independently installed FFmpeg."""
    environment = dict(os.environ)
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        bundle = Path(sys._MEIPASS).resolve()
        for key in ("DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
            if key not in environment:
                continue
            entries = [
                p
                for p in environment[key].split(os.pathsep)
                if p and not Path(p).resolve().is_relative_to(bundle)
            ]
            if entries:
                environment[key] = os.pathsep.join(entries)
            else:
                environment.pop(key)
    return environment
