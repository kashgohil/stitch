import subprocess
import sys
import threading
import time

import pytest

from stitch.process import RenderCancelled, run_ffmpeg


def test_progress_stream_is_drained_and_last_value_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("stitch.process.progress_command", tuple)
    command = [
        sys.executable,
        "-c",
        "import sys; [print('out_time_us='+str(i)) for i in range(100000)]; "
        "print('out_time_us=N/A'); print('diagnostic',file=sys.stderr)",
    ]
    updates = []
    assert run_ffmpeg(command, tmp_path / "log", progress=updates.append) == 0
    assert updates[-1] == 0.099999
    assert "diagnostic" in (tmp_path / "log").read_text()


@pytest.mark.parametrize("reason", ["cancel", "timeout", "callback"])
def test_interruption_reaps_child(tmp_path, monkeypatch, reason):
    monkeypatch.setattr("stitch.process.progress_command", tuple)
    children = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("stitch.process.subprocess.Popen", popen)
    cancel = threading.Event()

    def update(seconds):
        if reason == "cancel":
            cancel.set()
        elif reason == "callback":
            raise RuntimeError("callback failed")

    expected = {
        "cancel": RenderCancelled,
        "timeout": subprocess.TimeoutExpired,
        "callback": RuntimeError,
    }[reason]
    before = time.monotonic()
    with pytest.raises(expected):
        run_ffmpeg(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path / "log",
            progress=update,
            cancel=cancel,
            timeout=0.15 if reason == "timeout" else None,
        )
    assert time.monotonic() - before < 5
    assert children[0].poll() is not None and children[0].stdout.closed
