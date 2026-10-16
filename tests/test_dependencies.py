import subprocess

import pytest

from stitch.dependencies import probe_tool, probe_tools


def test_missing_tools_do_not_launch_a_process(monkeypatch):
    monkeypatch.setattr("stitch.dependencies.shutil.which", lambda _: None)

    def unexpected(*args, **kwargs):
        pytest.fail("A missing executable must not be launched")

    monkeypatch.setattr("stitch.dependencies.subprocess.run", unexpected)
    statuses = probe_tools()
    assert len(statuses) == 2
    assert all(not s.available and "PATH" in s.error for s in statuses)


def test_resolved_path_is_passed_without_a_shell(monkeypatch):
    path = "/a folder/with spaces/ffmpeg"
    monkeypatch.setattr("stitch.dependencies.shutil.which", lambda _: path)

    def run(args, **kwargs):
        assert args == [path, "-version"]
        assert not kwargs.get("shell", False)
        assert kwargs["timeout"] == 5.0
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(args, 0, "ffmpeg version test\nconfiguration", "")

    monkeypatch.setattr("stitch.dependencies.subprocess.run", run)
    status = probe_tool("ffmpeg")
    assert status.available
    assert status.path == path
    assert status.version == "ffmpeg version test"


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (subprocess.TimeoutExpired("ffmpeg", 5), "did not respond"),
        (PermissionError("denied"), "Cannot run"),
        (subprocess.CompletedProcess([], 1, "", "broken"), "exited with code 1"),
        (subprocess.CompletedProcess([], 0, "", ""), "unrecognized"),
        (subprocess.CompletedProcess([], 0, "another program", ""), "unrecognized"),
    ],
)
def test_broken_tools_are_not_reported_as_available(monkeypatch, outcome, expected):
    monkeypatch.setattr("stitch.dependencies.shutil.which", lambda _: "/tools/ffmpeg")

    def run(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("stitch.dependencies.subprocess.run", run)
    status = probe_tool("ffmpeg")
    assert not status.available
    assert expected in status.error
