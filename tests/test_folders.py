from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident

import pytest
from pydantic import ValidationError

from stitch.document import change_project, move_clip, remove_clip, replace_clip
from stitch.folders import inspect_candidate, natural_key, scan_folder
from stitch.media import MediaError
from stitch.models import AudioSettings, AudioStream, Crop, MediaMetadata, Project, VideoStream
from stitch.project_io import load_project, save_project
from stitch.sources import add_video, set_overlay


@pytest.fixture
def metadata():
    return MediaMetadata(
        format_name="mov",
        duration=10,
        video=VideoStream(index=0, codec="h264", width=320, height=180, average_frame_rate="30/1"),
        audio=(AudioStream(index=1, codec="pcm_s24le", channels=2, sample_rate=48000),),
    )


@pytest.fixture
def folder(tmp_path, monkeypatch, metadata):
    directory = tmp_path / "ગુજરાતી clips with spaces"
    directory.mkdir()
    for i in (10, 8, 2, 6, 5, 3, 9, 1, 4, 7):
        (directory / f"clip{i}.MP4").write_bytes(f"original {i}".encode())
    monkeypatch.setattr("stitch.sources.probe_media", lambda _: metadata)
    return directory


def names(project):
    sources = {s.id: s.path.name for s in project.sources}
    return [sources[c.source_id] for c in project.clips]


def test_natural_sort_has_deterministic_case_and_zero_ties():
    paths = [Path(p) for p in ("clip10.mp4", "clip2.mp4", "clip01.mp4", "Clip1.mp4", "clip1.mp4")]
    assert sorted(paths, key=natural_key) == sorted(reversed(paths), key=natural_key)
    assert [p.name for p in sorted(paths, key=natural_key)][-2:] == ["clip2.mp4", "clip10.mp4"]


def test_ten_clips_refresh_preserves_order_edits_and_removed_history(folder, tmp_path):
    first = scan_folder(Project(), folder, match_output=True)
    assert not first.errors and first.imported == 10
    assert names(first.project) == [f"clip{i}.MP4" for i in range(1, 11)]
    project = first.project
    project = replace_clip(
        project,
        project.clips[0].model_copy(
            update={
                "in_point": 1,
                "out_point": 5,
                "crop": Crop(width=160, height=90),
                "audio": AudioSettings(gain_db=-6),
            }
        ),
    )
    project = change_project(
        project,
        output=project.output.model_copy(
            update={
                "join_mode": "crossfade",
                "crossfade_duration": 0.5,
            }
        ),
    )
    project = move_clip(project, project.clips[0].id, 4)
    removed = project.clips[-1]
    project = remove_clip(project, removed.id)
    path = tmp_path / "saved.json"
    save_project(project, path)
    project = load_project(path)
    assert project.folder == folder and len(project.folder_history) == 10
    (folder / "clip11.mp4").write_bytes(b"new original")
    refreshed = scan_folder(project, folder)
    assert refreshed.imported == 1 and not refreshed.errors
    assert refreshed.project.clips[:-1] == project.clips
    assert refreshed.project.output == project.output
    assert names(refreshed.project)[-1] == "clip11.mp4"
    assert "clip10.MP4" not in names(refreshed.project)
    again = scan_folder(refreshed.project, folder)
    assert again.imported == 0 and again.project == refreshed.project
    explicit = add_video(again.project, folder / "clip10.MP4")
    assert names(explicit)[-1] == "clip10.MP4"
    assert scan_folder(explicit, folder).imported == 0


def test_changed_and_missing_sources_are_not_replaced_or_deleted(folder):
    first = scan_folder(Project(), folder).project
    (folder / "clip1.MP4").write_bytes(b"changed source content")
    (folder / "clip2.MP4").unlink()
    result = scan_folder(first, folder)
    assert result.project == first and result.imported == 0
    assert any("clip1.MP4: changed" in e for e in result.errors)
    assert any("clip2.MP4: missing" in e for e in result.errors)
    explicit = add_video(result.project, folder / "clip1.MP4")
    assert not any("changed" in e for e in scan_folder(explicit, folder).errors)


def test_manual_imports_overlay_and_folder_switch_keep_history(folder, tmp_path):
    project = add_video(Project(), folder / "clip1.MP4")
    result = scan_folder(project, folder)
    assert result.imported == 9 and len(result.project.folder_history) == 10
    project = set_overlay(result.project, folder / "clip2.MP4")
    assert project.folder_history == result.project.folder_history
    other = tmp_path / "other"
    other.mkdir()
    project = scan_folder(project, other).project
    project = remove_clip(project, project.clips[0].id)
    assert scan_folder(project, folder).imported == 0


def test_existing_overlay_in_folder_is_not_reported_missing(folder, metadata, monkeypatch):
    project = scan_folder(Project(), folder).project
    overlay = folder / "music.wav"
    overlay.write_bytes(b"overlay")
    monkeypatch.setattr(
        "stitch.sources.probe_media", lambda _: metadata.model_copy(update={"video": None})
    )
    project = set_overlay(project, overlay)
    result = scan_folder(project, folder)
    assert not result.errors and result.project == project
    overlay.unlink()
    assert any("music.wav: missing" in e for e in scan_folder(project, folder).errors)


def test_candidates_ignore_nested_links_hidden_files_and_generated_media(
    folder, metadata, monkeypatch
):
    (folder / "nested").mkdir()
    (folder / "nested" / "hidden.mp4").write_bytes(b"nested")
    (folder / ".stitch-temp.mp4").write_bytes(b"temporary")
    (folder / "notes.txt").write_text("not video")
    (folder / "link.mp4").symlink_to(folder / "clip1.MP4")
    (folder / "broken.mp4").symlink_to(folder / "missing.mp4")
    for name in ("audio.mp4", "export.mp4", "corrupt.mp4"):
        (folder / name).write_bytes(b"test")
    probes = []

    def probe(path):
        probes.append(path)
        if path.name == "corrupt.mp4":
            raise MediaError("corrupt video")
        if path.name == "audio.mp4":
            return metadata.model_copy(update={"video": None})
        if path.name == "export.mp4":
            return metadata.model_copy(update={"stitch_generated": True})
        return metadata

    monkeypatch.setattr("stitch.sources.probe_media", probe)
    result = scan_folder(Project(), folder)
    assert result.imported == 10 and len(result.errors) == 1
    assert "corrupt video" in result.errors[0]
    assert result.skipped == 7
    assert all(
        p.name not in {"link.mp4", "broken.mp4", ".stitch-temp.mp4", "notes.txt"} for p in probes
    )


def test_cancellation_keeps_only_complete_imports_and_can_resume(folder, metadata, monkeypatch):
    stop = [False]

    def probe(path):
        stop[0] = True
        return metadata

    monkeypatch.setattr("stitch.sources.probe_media", probe)
    result = scan_folder(Project(), folder, cancelled=lambda: stop[0])
    assert result.cancelled and result.imported == 1
    assert len(result.project.clips) == len(result.project.folder_history) == 1
    monkeypatch.setattr("stitch.sources.probe_media", lambda _: metadata)
    assert scan_folder(result.project, folder).imported == 9
    before = Project()
    result = scan_folder(before, folder, cancelled=lambda: True)
    assert result.cancelled and result.project == before


def test_empty_missing_inaccessible_and_cache_directories(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = scan_folder(Project(), empty)
    assert result.project.folder == empty and result.imported == 0 and not result.errors
    missing = scan_folder(result.project, tmp_path / "missing")
    assert missing.errors and missing.project == result.project
    assert scan_folder(Project(), empty, excluded_roots=(tmp_path,)).errors

    def denied(_):
        raise PermissionError("Access denied")

    monkeypatch.setattr("stitch.folders.os.scandir", denied)
    denied_result = scan_folder(result.project, tmp_path)
    assert denied_result.project == result.project and "Access denied" in denied_result.errors[0]


def test_old_projects_default_to_no_folder_and_reject_invalid_history(folder):
    old = Project().model_dump()
    old.pop("folder")
    old.pop("folder_history")
    assert Project.model_validate(old).folder is None
    with pytest.raises(ValidationError, match="absolute"):
        Project(folder=Path("relative"))
    project = scan_folder(Project(), folder).project
    with pytest.raises(ValidationError, match="unique"):
        change_project(project, folder_history=project.folder_history * 2)


def test_incompatible_crossfade_import_does_not_remember_failed_clip(folder, metadata, monkeypatch):
    project = scan_folder(Project(), folder).project
    project = change_project(
        project,
        output=project.output.model_copy(
            update={"join_mode": "crossfade", "crossfade_duration": 2}
        ),
    )
    (folder / "new.mp4").write_bytes(b"too short")
    monkeypatch.setattr(
        "stitch.sources.probe_media", lambda _: metadata.model_copy(update={"duration": 1})
    )
    result = scan_folder(project, folder)
    assert result.imported == 0 and result.project == project
    assert any("crossfade" in e for e in result.errors)


def test_cancel_during_discovery_does_not_change_project(folder):
    stop = [False]

    def discovered(_):
        stop[0] = True

    original = Project()
    result = scan_folder(original, folder, discovery=discovered, cancelled=lambda: stop[0])
    assert result.cancelled and result.project == original and result.imported == 0


def test_unreadable_file_is_reported_without_blocking_other_imports(folder, monkeypatch):
    from stitch.media import file_identity

    def inspect(path):
        if path.name == "clip2.MP4":
            raise MediaError("Cannot access source: permission denied")
        return file_identity(path)

    monkeypatch.setattr("stitch.folders.file_identity", inspect)
    result = scan_folder(Project(), folder)
    assert result.imported == 9 and len(result.errors) == 1
    assert "clip2.MP4" in result.errors[0] and "permission denied" in result.errors[0]


def test_renamed_removed_file_is_not_restored(folder):
    project = scan_folder(Project(), folder).project
    project = remove_clip(project, project.clips[0].id)
    (folder / "clip1.MP4").rename(folder / "renamed.mp4")
    result = scan_folder(project, folder)
    assert result.imported == 0 and result.project.clips == project.clips
    assert any(e.path.name == "renamed.mp4" for e in result.project.folder_history)
    assert scan_folder(result.project, folder).imported == 0


def test_parallel_inspection_is_bounded_and_applied_in_order(folder, monkeypatch):
    release = Event()
    prefetched = Event()
    barrier = Barrier(4, timeout=5)
    lock = Lock()
    started = []
    completed = []
    coordinator = []
    applied_on = []
    from stitch.sources import append_video_source

    def inspect(path, *args):
        with lock:
            started.append(path.name)
        index = int(path.stem.removeprefix("clip"))
        if index <= 4:
            barrier.wait()
        result = inspect_candidate(path, *args)
        if index == 1:
            assert release.wait(5)
        with lock:
            completed.append(path.name)
            if len(completed) == 3:
                prefetched.set()
        return result

    def append(*args):
        applied_on.append(get_ident())
        return append_video_source(*args)

    def scan():
        coordinator.append(get_ident())
        return scan_folder(Project(), folder, match_output=True)

    monkeypatch.setattr("stitch.folders.inspect_candidate", inspect)
    monkeypatch.setattr("stitch.folders.append_video_source", append)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scan)
        try:
            assert prefetched.wait(5)
            assert set(started) == {f"clip{i}.MP4" for i in range(1, 5)}
            assert "clip1.MP4" not in completed
            assert not applied_on
        finally:
            release.set()
        result = future.result(timeout=5)
    assert not result.errors and result.imported == 10
    assert names(result.project) == [f"clip{i}.MP4" for i in range(1, 11)]
    assert applied_on == coordinator * 10
    assert result.project.output.width == 320


def test_cancel_with_four_running_probes_joins_workers_and_refresh_resumes(folder, monkeypatch):
    release = Event()
    ready = Event()
    stop = Event()
    barrier = Barrier(4, action=ready.set, timeout=5)
    finished = []

    def inspect(path, *args):
        # All four inspections have completed metadata work but are still running.
        result = inspect_candidate(path, *args)
        barrier.wait()
        assert release.wait(5)
        finished.append(path.name)
        return result

    monkeypatch.setattr("stitch.folders.inspect_candidate", inspect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scan_folder, Project(), folder, cancelled=stop.is_set)
        try:
            assert ready.wait(5)
            stop.set()
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.cancelled and not result.errors
    assert len(finished) == 4
    assert names(result.project) == ["clip1.MP4"]
    assert len(result.project.folder_history) == 1
    monkeypatch.setattr("stitch.folders.inspect_candidate", inspect_candidate)
    resumed = scan_folder(result.project, folder)
    assert resumed.imported == 9 and not resumed.errors


def test_prefetched_file_changed_before_acceptance_is_rejected(folder, monkeypatch):
    second_ready = Event()

    def inspect(path, *args):
        result = inspect_candidate(path, *args)
        if path.name == "clip2.MP4":
            second_ready.set()
        elif path.name == "clip1.MP4":
            assert second_ready.wait(5)
            (folder / "clip2.MP4").write_bytes(b"replaced after inspection")
        return result

    monkeypatch.setattr("stitch.folders.inspect_candidate", inspect)
    result = scan_folder(Project(), folder)
    assert result.imported == 9 and len(result.errors) == 1
    assert "clip2.MP4: Source changed after inspection" in result.errors[0]
    assert "clip2.MP4" not in names(result.project)
    assert all(e.path.name != "clip2.MP4" for e in result.project.folder_history)


def test_parallel_new_hard_links_are_deduplicated(folder):
    (folder / "clip1-alias.mp4").hardlink_to(folder / "clip1.MP4")
    result = scan_folder(Project(), folder)
    assert result.imported == 10 and result.skipped == 1 and not result.errors
    assert len(result.project.folder_history) == 11
    assert scan_folder(result.project, folder).imported == 0


@pytest.mark.parametrize("workers", [0, -1, 5, True, 2.5])
def test_invalid_probe_worker_limits_are_rejected(folder, workers):
    with pytest.raises(ValueError, match="probe_workers"):
        scan_folder(Project(), folder, probe_workers=workers)


def test_single_worker_mode_preserves_results(folder):
    result = scan_folder(Project(), folder, probe_workers=1)
    assert result.imported == 10 and not result.errors
    assert names(result.project) == [f"clip{i}.MP4" for i in range(1, 11)]
