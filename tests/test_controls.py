import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from stitch.dependencies import ToolStatus
from stitch.models import AudioStream, MediaMetadata, VideoStream
from stitch.paths import AppPaths
from stitch.window import MainWindow


@pytest.fixture
def metadata():
    return MediaMetadata(
        format_name="mov,mp4",
        duration=20,
        video=VideoStream(
            index=0,
            codec="hevc",
            width=7680,
            height=4320,
            average_frame_rate="120000/1001",
            bit_depth=10,
        ),
        audio=(AudioStream(index=1, codec="aac", sample_rate=48000, channels=2),),
    )


@pytest.fixture
def window(qtbot, monkeypatch, tmp_path, metadata):
    monkeypatch.setattr(
        "stitch.window.probe_tools",
        lambda: (ToolStatus("ffmpeg", "/bin/ffmpeg", version="ffmpeg version test"),),
    )
    monkeypatch.setattr("stitch.sources.probe_media", lambda path: metadata)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    window = MainWindow(AppPaths(*(tmp_path / str(i) for i in range(4))))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.tools_ready)
    yield window
    if window._importing:
        window.stop_import()
        qtbot.waitUntil(lambda: not window._importing, timeout=5000)
    window.close()


def import_clips(window, qtbot, tmp_path, count=1):
    paths = [tmp_path / f"clip-{i}.mp4" for i in range(count)]
    for path in paths:
        path.write_bytes(b"test source")
    window.import_paths(paths)
    qtbot.waitUntil(lambda: not window._importing)
    assert window.table.rowCount() == count
    return paths


def test_choose_media_tools_folder_is_saved_and_rechecked(window, qtbot, monkeypatch, tmp_path):
    import json
    import os

    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    directory = tmp_path / "selected tools"
    directory.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        executable = directory / name
        executable.write_bytes(b"test placeholder; mocked dependency check")
        executable.chmod(0o755)
    monkeypatch.setattr("stitch.window.QFileDialog.getExistingDirectory", lambda *a: str(directory))
    window.choose_tools.click()
    qtbot.waitUntil(lambda: window.tools_ready)
    preference = window.paths.projects.parent / "media-tools.json"
    assert json.loads(preference.read_text())["directory"] == str(directory)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(directory)
    assert not window.document.dirty


def test_invalid_media_tools_folder_shows_error(window, monkeypatch, tmp_path):
    monkeypatch.setattr("stitch.window.QFileDialog.getExistingDirectory", lambda *a: str(tmp_path))
    window.choose_tools.click()
    assert "ffmpeg" in window.error.toPlainText()
    assert not (window.paths.projects.parent / "media-tools.json").exists()


def test_ten_clips_edits_reorder_and_save_reopen(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path, 10)
    assert window.document.project.output.frame_rate == "120000/1001"
    assert window.document.project.output.width == 7680
    window.table.setCurrentCell(3, 0)
    clip_id = window.document.project.clips[3].id
    editor = window.clip_editor
    editor.start.setValue(1.25)
    editor.source_end.setChecked(False)
    editor.end.setValue(9.5)
    editor.crop_enabled.setChecked(True)
    editor.x.setValue(100)
    editor.y.setValue(200)
    editor.width.setValue(1920)
    editor.height.setValue(1080)
    editor.keep_audio.setChecked(False)
    window.up.click()
    edited = window.document.project.clips[2]
    assert edited.id == clip_id and edited.crop.x == 100
    assert edited.in_point == 1.25 and edited.out_point == 9.5 and not edited.audio.enabled
    window.output_editor.name.setText("Weekend 8K")
    window.output_editor.fps.setCurrentText("60000/1001")
    window.output_editor.profile.setCurrentIndex(2)
    window.output_editor.upscale.setChecked(True)
    path = tmp_path / "project.json"
    assert window.save_to_path(path)
    expected = window.document.project
    assert not window.document.dirty and "*" not in window.windowTitle()
    window.new.click()
    assert window.table.rowCount() == 0
    assert window.open_path(path)
    assert window.document.project == expected
    window.table.setCurrentCell(2, 0)
    assert editor.clip.id == clip_id and editor.x.value() == 100
    assert window.output_editor.profile.currentData() == "prores_hq"
    assert window.output_editor.fps.currentText() == "60000/1001"
    assert window.output_editor.upscale.isChecked()


def test_invalid_drafts_block_selection_reorder_and_save(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path, 2)
    before = window.document.project
    editor = window.clip_editor
    editor.start.setValue(25)
    window.table.setCurrentCell(0, 0)
    assert window.table.currentRow() == 1
    assert window._selected == before.clips[1].id
    window.up.click()
    assert window.document.project == before
    assert "duration" in window.error.toPlainText()
    assert not window.save_to_path(tmp_path / "invalid.json")
    assert not (tmp_path / "invalid.json").exists()
    editor.reset.click()
    editor.source_end.setChecked(False)
    editor.end.setValue(0)
    editor.apply.click()
    assert "out_point" in window.error.toPlainText()
    assert window.document.project == before
    editor.reset.click()
    editor.crop_enabled.setChecked(True)
    editor.x.setValue(1)  # Full-width crop no longer fits.
    editor.apply.click()
    assert "crop" in window.error.toPlainText().lower()
    assert window.document.project == before
    editor.reset.click()
    window.output_editor.width.setValue(7679)
    window.output_editor.apply.click()
    assert "even" in window.error.toPlainText()
    assert window.document.project == before
    window.output_editor.reset.click()
    window.output_editor.fps.setCurrentText("not a rate")
    window.output_editor.apply.click()
    assert window.document.project == before
    assert window.error.toPlainText()


def test_remove_discards_clip_draft_and_preserves_remaining_ids(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path, 2)
    remaining = window.document.project.clips[0]
    window.clip_editor.start.setValue(99)
    window.remove.click()
    assert window.document.project.clips == (remaining,)
    assert len(window.document.project.sources) == 1
    window.remove.click()
    assert not window.document.project.sources and window.table.rowCount() == 0
    assert not window.clip_editor.apply.isEnabled()


def test_manual_output_not_replaced_by_import(window, qtbot, tmp_path):
    window.output_editor.width.setValue(1920)
    window.output_editor.height.setValue(1080)
    window.output_editor.fps.setCurrentText("24/1")
    import_clips(window, qtbot, tmp_path)
    assert window.document.project.output.width == 1920
    assert window.document.project.output.frame_rate == "24/1"


def test_folder_dialog_refresh_and_reopen_keep_reviewed_queue(window, qtbot, tmp_path, monkeypatch):
    folder = tmp_path / "folder with spaces"
    folder.mkdir()
    for i in (10, 2, 1):
        (folder / f"clip{i}.mp4").write_bytes(b"original")
    assert not window.refresh_folder.isEnabled()
    monkeypatch.setattr("stitch.window.QFileDialog.getExistingDirectory", lambda *a: str(folder))
    window.add_folder.click()
    qtbot.waitUntil(lambda: not window._importing)
    assert [window.table.item(i, 0).text() for i in range(3)] == [
        "clip1.mp4",
        "clip2.mp4",
        "clip10.mp4",
    ]
    assert window.refresh_folder.isEnabled() and str(folder) in window.folder_label.text()
    window.clip_editor.start.setValue(1)
    window.up.click()
    window.remove.click()  # Removed clip10 must not return on refresh.
    before = window.document.project
    saved = tmp_path / "folder.json"
    assert window.save_to_path(saved)
    window.new_project()
    assert not window.refresh_folder.isEnabled()
    assert window.open_path(saved)
    (folder / "clip11.mp4").write_bytes(b"new original")
    window.refresh_folder.click()
    qtbot.waitUntil(lambda: not window._importing)
    assert window.document.project.clips[:-1] == before.clips
    assert window.table.rowCount() == 3
    assert window.table.item(2, 0).text() == "clip11.mp4"
    assert not window._exporting
    window.refresh_folder.click()
    qtbot.waitUntil(lambda: not window._importing)
    assert window.table.rowCount() == 3
    assert "Imported 0" in window.statusBar().currentMessage()


def test_folder_probe_and_discovery_run_off_ui_thread_and_cancel(
    window, qtbot, tmp_path, monkeypatch, metadata
):
    started, release = threading.Event(), threading.Event()
    thread_ids = []
    from stitch.folders import scan_folder

    def scan(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        return scan_folder(*args, **kwargs)

    def probe(_):
        thread_ids.append(threading.get_ident())
        started.set()
        release.wait(timeout=5)
        return metadata

    monkeypatch.setattr("stitch.workers.scan_folder", scan)
    monkeypatch.setattr("stitch.sources.probe_media", probe)
    for i in range(3):
        (tmp_path / f"clip{i}.mp4").write_bytes(b"original")
    try:
        window.import_folder_path(tmp_path)
        qtbot.waitUntil(started.is_set)
        assert not window.add_folder.isEnabled() and not window.refresh_folder.isEnabled()
        window.cancel_import.click()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._importing)
    assert all(t != threading.get_ident() for t in thread_ids)
    assert len(window.document.project.clips) == 1
    assert len(window.document.project.folder_history) == 1
    assert "Stopped" in window.statusBar().currentMessage()
    assert window.add_folder.isEnabled() and window.refresh_folder.isEnabled()


def test_crossfade_controls_validation_and_round_trip(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path, 3)
    editor = window.output_editor
    assert not editor.crossfade_duration.isEnabled()
    editor.join_mode.setCurrentIndex(1)
    assert editor.crossfade_duration.isEnabled()
    editor.crossfade_duration.setValue(11)
    before = window.document.project
    editor.apply.click()
    assert window.document.project == before
    assert "Clip 2" in window.error.toPlainText()
    editor.crossfade_duration.setValue(0.25)
    editor.apply.click()
    saved = tmp_path / "crossfade.json"
    assert window.save_to_path(saved)
    window.new_project()
    assert window.open_path(saved)
    assert editor.join_mode.currentData() == "crossfade"
    assert editor.crossfade_duration.value() == 0.25


def test_crossfade_invalid_reorder_is_safe_and_combined_drafts_are_atomic(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path, 3)
    window.output_editor.fps.setCurrentText("30/1")
    window.output_editor.join_mode.setCurrentIndex(1)
    window.output_editor.crossfade_duration.setValue(1)
    window.clip_editor.source_end.setChecked(False)
    window.clip_editor.end.setValue(1)  # End clip needs one overlap; middle needs two.
    window.apply_edits()
    before = window.document.project
    window.up.click()
    assert window.document.project == before
    assert "Clip 2" in window.error.toPlainText()
    # Apply both drafts at once: shortening is valid when disabling transitions.
    window.clip_editor.end.setValue(0.5)
    window.output_editor.join_mode.setCurrentIndex(0)
    window.apply_edits()
    assert window.document.project.clips[-1].out_point == 0.5
    assert window.document.project.output.join_mode == "hard_cut"


def test_project_name_alone_does_not_disable_native_output_matching(window, qtbot, tmp_path):
    window.output_editor.name.setText("My footage")
    import_clips(window, qtbot, tmp_path)
    assert window.document.project.name == "My footage"
    assert window.document.project.output.width == 7680


def test_reopen_missing_source_retains_edits_and_shows_warning(window, qtbot, tmp_path):
    paths = import_clips(window, qtbot, tmp_path)
    window.clip_editor.start.setValue(2)
    project_path = tmp_path / "project.json"
    assert window.save_to_path(project_path)
    before = window.document.project
    paths[0].unlink()
    assert window.open_path(project_path)
    assert window.document.project == before
    assert window.table.item(0, 4).text() == "missing"
    assert window.clip_editor.start.value() == 2


def test_import_is_background_partial_failure_and_cancellation(
    window,
    qtbot,
    tmp_path,
    monkeypatch,
    metadata,
):
    started, release = threading.Event(), threading.Event()
    thread_ids = []

    def slow_probe(path):
        thread_ids.append(threading.current_thread().ident)
        started.set()
        release.wait(timeout=3)
        return metadata

    monkeypatch.setattr("stitch.sources.probe_media", slow_probe)
    path = tmp_path / "slow.mp4"
    path.write_bytes(b"source")
    try:
        window.import_paths([tmp_path / "missing.mp4", path, path])
        qtbot.waitUntil(started.is_set)
        assert not window.add.isEnabled() and not window.save.isEnabled()
        assert not window.sequence.isEnabled()
        window.cancel_import.click()
        assert not window.cancel_import.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._importing)
    assert len(window.document.project.clips) == 1
    assert "missing.mp4" in window.error.toPlainText()
    assert window.add.isEnabled() and window.save.isEnabled()
    assert thread_ids and threading.current_thread().ident not in thread_ids


def test_close_during_import_waits_and_then_honors_cancel(
    window,
    qtbot,
    tmp_path,
    monkeypatch,
    metadata,
):
    started, release = threading.Event(), threading.Event()

    def slow_probe(path):
        started.set()
        release.wait(timeout=3)
        return metadata

    monkeypatch.setattr("stitch.sources.probe_media", slow_probe)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    path = tmp_path / "slow.mp4"
    path.write_bytes(b"source")
    try:
        window.import_paths([path, path])
        qtbot.waitUntil(started.is_set)
        window.close()
        assert window.isVisible() and window._import_worker.isRunning()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._importing)
    assert window.isVisible() and len(window.document.project.clips) == 1
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)


def test_failed_open_save_and_unsaved_prompts(window, qtbot, tmp_path, monkeypatch):
    import_clips(window, qtbot, tmp_path)
    before = window.document.project
    assert not window.open_path(tmp_path / "missing.json")
    assert window.document.project == before
    assert not window.save_to_path(tmp_path / "missing" / "project.json")
    assert window.document.dirty
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    window.new.click()
    window.close()
    assert window.isVisible() and window.document.project == before
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Save)
    path = tmp_path / "saved.json"
    monkeypatch.setattr("stitch.window.QFileDialog.getSaveFileName", lambda *args: (str(path), ""))
    window.new.click()
    assert path.exists() and not window.document.project.clips
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)


def test_typed_spin_values_are_pending_before_focus_moves(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path)
    editor = window.clip_editor
    editor.start.setFocus()
    editor.start.selectAll()
    qtbot.keyClicks(editor.start, "2.5")
    assert editor.pending
    assert window.save_to_path(tmp_path / "typed.json")
    assert window.document.project.clips[0].in_point == 2.5
    window.output_editor.width.setFocus()
    window.output_editor.width.selectAll()
    qtbot.keyClicks(window.output_editor.width, "3840")
    qtbot.keyClick(window.output_editor.width, Qt.Key.Key_Return)
    assert window.save_to_path(tmp_path / "typed.json")
    assert window.document.project.output.width == 3840


def test_export_disables_edits_reports_progress_and_restores_controls(
    window,
    qtbot,
    tmp_path,
    monkeypatch,
    metadata,
):
    from stitch.export import ExportProgress, ExportResult

    import_clips(window, qtbot, tmp_path)
    started, release = threading.Event(), threading.Event()
    thread_ids = []
    output = tmp_path / "final.mp4"

    def export(project, destination, paths, **kwargs):
        thread_ids.append(threading.current_thread().ident)
        kwargs["progress"](ExportProgress("rendering", 1, 1, 0.5, 0.45, 2, "Rendering clip 1/1"))
        started.set()
        release.wait(timeout=3)
        return ExportResult(destination, tmp_path / "job", metadata, ())

    monkeypatch.setattr("stitch.workers.export_project", export)
    try:
        window.start_export(output)
        qtbot.waitUntil(started.is_set)
        qtbot.waitUntil(lambda: window.export_progress.value() == 450)
        assert window.busy and not window.add.isEnabled() and not window.save.isEnabled()
        assert not window.export.isEnabled() and window.cancel_export.isEnabled()
        assert not window.output_editor.isEnabled()
        assert "50%" in window.export_status.text()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._exporting)
    assert window.export_progress.value() == 1000
    assert window.export.isEnabled() and window.add.isEnabled()
    assert str(output) in window.export_status.text()
    assert threading.current_thread().ident not in thread_ids


def test_close_during_export_cancels_and_waits_for_worker(window, qtbot, tmp_path, monkeypatch):
    from stitch.process import RenderCancelled

    import_clips(window, qtbot, tmp_path)
    started = threading.Event()

    def export(project, destination, paths, **kwargs):
        started.set()
        kwargs["cancel"].wait(timeout=3)
        raise RenderCancelled("Export cancelled. Diagnostics retained.")

    monkeypatch.setattr("stitch.workers.export_project", export)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    try:
        window.start_export(tmp_path / "final.mp4")
        qtbot.waitUntil(started.is_set)
        window.close()
        assert window.isVisible()
        qtbot.waitUntil(lambda: not window._exporting)
        assert window.isVisible()  # Unsaved-project cancellation is still respected afterward.
        assert window.export_status.text() == "Export cancelled."
        assert "Diagnostics" in window.error.toPlainText()
    finally:
        window.stop_export()
        qtbot.waitUntil(lambda: not window._exporting)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard
        )


def test_export_failure_restores_controls(window, qtbot, tmp_path, monkeypatch):
    import_clips(window, qtbot, tmp_path)

    def export(*args, **kwargs):
        raise OSError("Disk is full")

    monkeypatch.setattr("stitch.workers.export_project", export)
    window.start_export(tmp_path / "final.mp4")
    qtbot.waitUntil(lambda: not window._exporting)
    assert window.export_status.text() == "Export failed."
    assert "Disk is full" in window.error.toPlainText()
    assert window.export.isEnabled() and window.save.isEnabled()


def test_overlay_ui_gain_loop_save_reopen_replace_remove(window, qtbot, tmp_path):
    import_clips(window, qtbot, tmp_path)
    window.clip_editor.gain.setValue(-4.5)
    music = tmp_path / "music.wav"
    music.write_bytes(b"music")
    window.import_overlay_path(music)
    qtbot.waitUntil(lambda: not window._importing)
    assert window.document.project.clips[0].audio.gain_db == -4.5
    editor = window.overlay_editor
    window.project_tabs.setCurrentWidget(editor)
    assert "music.wav" in editor.metadata.text()
    editor.gain.setValue(-8.5)
    editor.behavior.setCurrentIndex(editor.behavior.findData("loop"))
    assert editor.pending and "*" in window.windowTitle()
    saved = tmp_path / "audio-project.json"
    assert window.save_to_path(saved)
    expected = window.document.project
    window.new_project()
    assert window.document.project.overlay is None
    assert window.open_path(saved)
    assert window.document.project == expected
    assert editor.gain.value() == -8.5 and editor.behavior.currentData() == "loop"
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"replacement")
    window.import_overlay_path(replacement)
    qtbot.waitUntil(lambda: not window._importing)
    assert editor.gain.value() == -8.5 and len(window.document.project.sources) == 2
    assert "replacement.wav" in editor.metadata.text()
    editor.remove.click()
    assert window.document.project.overlay is None
    assert len(window.document.project.sources) == 1
    assert music.exists() and replacement.exists()


def test_failed_or_cancelled_audio_replacement_preserves_overlay(
    window,
    qtbot,
    tmp_path,
    monkeypatch,
    metadata,
):
    music = tmp_path / "music.wav"
    music.write_bytes(b"music")
    window.import_overlay_path(music)
    qtbot.waitUntil(lambda: not window._importing)
    before = window.document.project
    window.import_overlay_path(tmp_path / "missing.wav")
    qtbot.waitUntil(lambda: not window._importing)
    assert window.document.project == before
    assert "missing" in window.error.toPlainText()
    started, release = threading.Event(), threading.Event()

    def slow_probe(path):
        started.set()
        release.wait(timeout=3)
        return metadata

    monkeypatch.setattr("stitch.sources.probe_media", slow_probe)
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"replacement")
    try:
        window.import_overlay_path(replacement)
        qtbot.waitUntil(started.is_set)
        assert not window.overlay_editor.add.isEnabled()
        window.stop_import()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._importing)
    assert window.document.project == before


def test_overlay_drafts_reset_and_unsaved_prompt(window, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "music.wav"
    path.write_bytes(b"music")
    window.import_overlay_path(path)
    qtbot.waitUntil(lambda: not window._importing)
    assert window.save_to_path(tmp_path / "project.json")
    editor = window.overlay_editor
    editor.enabled.setChecked(False)
    assert editor.pending and not editor.gain.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    window.new_project()
    assert window.document.project.overlay is not None and editor.pending
    editor.reset.click()
    assert editor.enabled.isChecked() and not editor.pending
    assert "*" not in window.windowTitle()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)


def test_audio_first_import_does_not_override_first_video_output(window, qtbot, tmp_path):
    path = tmp_path / "music.wav"
    path.write_bytes(b"music")
    window.import_overlay_path(path)
    qtbot.waitUntil(lambda: not window._importing)
    assert not window.document.output_configured
    import_clips(window, qtbot, tmp_path)
    assert window.document.project.output.width == 7680
