import threading

from stitch.dependencies import ToolStatus
from stitch.paths import AppPaths
from stitch.window import MainWindow


def test_missing_tool_can_be_rechecked_without_restarting(qtbot, monkeypatch, tmp_path):
    calls = []

    def probe():
        calls.append(threading.current_thread().ident)
        if len(calls) == 1:
            return (ToolStatus("ffmpeg", None, error="ffmpeg was not found on PATH."),)
        return (ToolStatus("ffmpeg", "/bin/ffmpeg", version="ffmpeg version test"),)

    monkeypatch.setattr("stitch.window.probe_tools", probe)
    paths = AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))
    window = MainWindow(paths)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.retry.isEnabled())
    assert not window.tools_ready
    assert window.help.isVisible()
    assert "PATH" in window.details.toPlainText()
    window.retry.click()
    assert not window.retry.isEnabled()
    qtbot.waitUntil(lambda: window.tools_ready)
    assert not window.help.isVisible()
    assert all(thread != threading.current_thread().ident for thread in calls)


def test_close_during_check_waits_without_blocking_ui(qtbot, monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()

    def probe():
        started.set()
        release.wait(timeout=3)
        return (ToolStatus("ffmpeg", None, error="missing"),)

    monkeypatch.setattr("stitch.window.probe_tools", probe)
    window = MainWindow(AppPaths(*(tmp_path / str(i) for i in range(4))))
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(started.is_set)
        window.close()
        assert not window.isVisible()
        assert window._worker.isRunning()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._worker.isRunning())
