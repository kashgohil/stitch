"""Supervise FFmpeg without buffering media or an unbounded progress queue."""

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from stitch.render_plan import RenderError


class RenderCancelled(RenderError):
    """Cancellation completed; no final output should be published."""


def check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise RenderCancelled("Export cancelled.")


def progress_command(command: Sequence[str]) -> tuple[str, ...]:
    return (command[0], "-progress", "pipe:1", "-nostats", *command[1:])


def run_ffmpeg(
    command: Sequence[str],
    log_path: Path,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> int:
    """Report output seconds on the calling worker thread; terminate, then kill if needed."""
    check_cancelled(cancel)
    started = time.monotonic()
    latest = [0.0]
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            progress_command(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        def read_progress() -> None:
            # Only the latest timestamp is retained, independent of export duration.
            while line := process.stdout.readline(4096):
                key, _, value = line.partition(b"=")
                if key == b"out_time_us":
                    try:
                        latest[0] = max(latest[0], int(value) / 1_000_000)
                    except ValueError:
                        pass

        reader = threading.Thread(target=read_progress, name="ffmpeg-progress", daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                check_cancelled(cancel)
                if timeout is not None and time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if progress:
                    progress(latest[0])
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            check_cancelled(cancel)
            reader.join(timeout=2)
            if progress:
                progress(latest[0])
            return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=2)
            process.stdout.close()
