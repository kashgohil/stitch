"""Bounded, shell-free checks for the external media tools."""

import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolStatus:
    name: str
    path: str | None
    version: str | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.version is not None and self.error is None


def installation_help() -> str:
    if sys.platform == "darwin":
        return "Install FFmpeg with Homebrew: brew install ffmpeg. Then click Check again."
    if sys.platform == "win32":
        return (
            "Install an FFmpeg build containing ffmpeg.exe and ffprobe.exe, "
            "add its bin folder to PATH, then restart Stitch."
        )
    return (
        "Install FFmpeg using your distribution's package manager "
        "(Ubuntu/Debian: sudo apt install ffmpeg), then click Check again."
    )


def probe_tool(name: str, *, timeout: float = 5.0) -> ToolStatus:
    path = shutil.which(name)
    if path is None:
        return ToolStatus(name, None, error=f"{name} was not found on PATH.")
    try:
        result = subprocess.run(
            [path, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return ToolStatus(name, path, error=f"{name} did not respond within {timeout:g} seconds.")
    except OSError as exc:
        return ToolStatus(name, path, error=f"Cannot run {name}: {exc}")
    if result.returncode != 0:
        return ToolStatus(name, path, error=f"{name} exited with code {result.returncode}.")
    lines = result.stdout.splitlines()
    if not lines or not lines[0].startswith(f"{name} version "):
        return ToolStatus(name, path, error=f"{name} returned an unrecognized version response.")
    return ToolStatus(name, path, version=lines[0])


def probe_tools() -> tuple[ToolStatus, ToolStatus]:
    """Run off the GUI thread. An installed binary does not imply codec support."""
    return probe_tool("ffmpeg"), probe_tool("ffprobe")
