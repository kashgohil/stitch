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
