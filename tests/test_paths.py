import pytest

from stitch.paths import AppPaths


def test_initialization_preserves_existing_data_and_isolates_jobs(tmp_path):
    paths = AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))
    paths.ensure()
    saved = paths.projects / "existing.json"
    saved.write_text("keep me")
    paths.ensure()
    assert saved.read_text() == "keep me"
    first, second = paths.new_job(), paths.new_job()
    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.parent == paths.jobs


def test_storage_failure_is_reported(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    paths = AppPaths(blocked / "projects", tmp_path / "p", tmp_path / "j", tmp_path / "l")
    with pytest.raises(OSError):
        paths.ensure()
