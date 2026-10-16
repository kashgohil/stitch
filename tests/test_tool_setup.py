import json
import os
import sys

import pytest

from stitch.paths import AppPaths
from stitch.tool_setup import configure_media_tools, external_tool_environment, save_tool_directory


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


def test_finder_path_adds_known_directories_without_shell(monkeypatch, paths):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    configure_media_tools(paths)
    assert os.environ["PATH"] == "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"
    configure_media_tools(paths)
    assert os.environ["PATH"].count("/opt/homebrew/bin") == 1


def test_selected_folder_persists_and_invalid_selection_preserves_settings(
    monkeypatch, paths, tmp_path
):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    directory = tmp_path / "Tools with spaces"
    directory.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        binary = directory / name
        binary.write_bytes(b"test executable placeholder; never launched")
        binary.chmod(0o755)
    save_tool_directory(directory, paths)
    preference = paths.projects.parent / "media-tools.json"
    assert json.loads(preference.read_text()) == {"directory": str(directory)}
    assert os.environ["PATH"].split(os.pathsep)[0] == str(directory)
    monkeypatch.setenv("PATH", "/usr/bin")
    configure_media_tools(paths)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(directory)
    before = preference.read_bytes()
    with pytest.raises(ValueError, match="ffmpeg"):
        save_tool_directory(tmp_path, paths)
    assert preference.read_bytes() == before


@pytest.mark.parametrize("payload", ["{", "null", '{"directory": "relative"}', "[]"])
def test_bad_preferences_are_ignored(paths, monkeypatch, payload):
    monkeypatch.setenv("PATH", "/usr/bin")
    (paths.projects.parent / "media-tools.json").write_text(payload)
    configure_media_tools(paths)
    assert os.environ["PATH"] == "/usr/bin"


def test_external_tool_library_environment_does_not_change_app_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    library_path = f"{tmp_path}/bundle/lib:/custom/lib"
    monkeypatch.setenv("DYLD_LIBRARY_PATH", library_path)
    monkeypatch.setenv("DYLD_FRAMEWORK_PATH", str(tmp_path / "bundle/frameworks"))
    cleaned = external_tool_environment()
    assert cleaned["DYLD_LIBRARY_PATH"] == "/custom/lib"
    assert "DYLD_FRAMEWORK_PATH" not in cleaned
    assert os.environ["DYLD_LIBRARY_PATH"] == library_path
