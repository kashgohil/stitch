import json

import pytest

from stitch.app import main
from stitch.models import Project, Source
from stitch.project_io import save_project


def test_probe_failure_returns_nonzero_without_traceback(tmp_path, capsys):
    assert main(["--probe", str(tmp_path / "missing.mp4")]) == 1
    assert "missing" in capsys.readouterr().err


def test_inspect_project_reports_missing_sources(tmp_path, capsys):
    path = tmp_path / "project.json"
    save_project(Project(sources=(Source(path=tmp_path / "missing.mp4"),)), path)
    assert main(["--inspect-project", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["sources"][0]["state"] == "missing"


def test_schema_command_needs_no_media_tools(capsys):
    assert main(["--project-schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Project"


def test_render_cli_rejects_empty_project_without_gui(tmp_path, monkeypatch, capsys):
    from stitch.paths import AppPaths

    paths = AppPaths(*(tmp_path / name for name in ("p", "c", "j", "l")))
    monkeypatch.setattr("stitch.app.AppPaths.default", lambda: paths)
    monkeypatch.setattr("stitch.app.configure_logging", lambda paths: None)
    path = tmp_path / "project.json"
    save_project(Project(), path)
    assert main(["--render-clip", str(path)]) == 1
    assert "0 clips" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args", [["--clip-index", "1"], ["--render-clip", "a.json", "--clip-index", "0"]]
)
def test_render_cli_validates_clip_index(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
