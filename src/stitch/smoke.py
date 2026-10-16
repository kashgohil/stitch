"""Opt-in GUI-driven package check. Writes only to a new diagnostic directory."""

import json
import shutil
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from stitch.app import configure_logging
from stitch.paths import AppPaths
from stitch.tool_setup import configure_media_tools, external_tool_environment
from stitch.window import MainWindow


def run_smoke(directory: Path) -> int:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=False)  # Never replace previous diagnostics.
    paths = AppPaths(*(directory / name for name in ("projects", "proxies", "jobs", "logs")))
    paths.ensure()
    configure_media_tools(AppPaths.default())
    configure_logging(paths)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("Install FFmpeg before running the package smoke check.")
    video, audio = directory / "source.mp4", directory / "music.wav"
    for args in (
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ],
        ["-f", "lavfi", "-i", "sine=frequency=880:duration=0.25", "-c:a", "pcm_s24le", str(audio)],
    ):
        subprocess.run(
            [executable, "-nostdin", "-v", "error", *args],
            check=True,
            timeout=30,
            env=external_tool_environment(),
        )
    app = QApplication([])
    app.setApplicationName("Stitch")
    window = MainWindow(paths)
    window.show()
    report = {"status": "running", "profiles": [], "preview_frame_decoded": False}
    stage = ["tools"]
    started = time.monotonic()
    profiles = iter(("h264", "hevc_main10", "prores_hq"))
    selected_profile = [None]
    project_file = paths.projects / "smoke.json"

    def next_export():
        profile = next(profiles, None)
        if profile is None:
            report["status"] = "passed"
            stage[0] = "finish"
            return
        selected_profile[0] = profile
        window.output_editor.profile.setCurrentIndex(window.output_editor.profile.findData(profile))
        window.apply_edits()
        suffix = ".mov" if profile == "prores_hq" else ".mp4"
        window.start_export(directory / (profile + suffix))
        stage[0] = "export"

    def tick():
        try:
            if stage[0] != "finish" and time.monotonic() - started > 90:
                raise TimeoutError(f"Smoke check timed out during {stage[0]}")
            if stage[0] == "tools":
                if window._worker is None or window._worker.isRunning():
                    return
                assert window.tools_ready, window.details.toPlainText()
                window.import_paths([video, video])
                stage[0] = "import"
            elif stage[0] == "import" and not window._importing:
                assert len(window.document.project.clips) == 2, window.error.toPlainText()
                window.move_selected(-1)
                editor = window.clip_editor
                editor.start.setValue(0.1)
                editor.source_end.setChecked(False)
                editor.end.setValue(0.9)
                editor.crop_enabled.setChecked(True)
                editor.width.setValue(300)
                editor.height.setValue(160)
                window.apply_edits()
                window.import_overlay_path(audio)
                stage[0] = "audio"
            elif stage[0] == "audio" and not window._importing:
                assert window.document.project.overlay is not None, window.error.toPlainText()
                window.overlay_editor.gain.setValue(-9)
                window.apply_edits()
                assert window.save_to_path(project_file)
                expected = window.document.project
                window.new_project()
                assert window.open_path(project_file) and window.document.project == expected
                window.sequence_tabs.setCurrentIndex(1)
                window.preview.mute.setChecked(True)
                window.preview.start_generation()
                stage[0] = "proxy"
            elif stage[0] == "proxy" and window.preview.worker is None:
                assert window.preview.path is not None, window.preview.state.text()
                if not window.preview.play.isEnabled():
                    return
                window.preview.toggle_play()
                stage[0] = "playback"
            elif stage[0] == "playback":
                frame = window.preview.video.videoSink().videoFrame()
                if not frame.isValid() or window.preview.player.position() < 200:
                    return
                report["preview_frame_decoded"] = True
                frame.toImage().save(str(directory / "preview-frame.png"))
                window.preview.player.pause()
                next_export()
            elif stage[0] == "export" and not window._exporting:
                worker = window._export_worker
                assert worker and worker.result, window.error.toPlainText()
                report["profiles"].append(
                    {
                        "profile": selected_profile[0],
                        "path": str(worker.result.path),
                        "metadata": worker.result.metadata.model_dump(mode="json"),
                    }
                )
                next_export()
            if stage[0] == "finish":
                window.stop_import()
                window.stop_export()
                window.preview.stop_generation()
                if (
                    window.busy
                    or window.preview.worker
                    or (window._worker and window._worker.isRunning())
                ):
                    return
                timer.stop()
                window.save_to_path(project_file)
                window.document.saved = window.document.project
                window.clip_editor.pending = window.output_editor.pending = (
                    window.overlay_editor.pending
                ) = False
                report["elapsed_seconds"] = round(time.monotonic() - started, 2)
                (directory / "smoke-report.json").write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
                window.close()
                app.exit(0 if report["status"] == "passed" else 1)
        except Exception as exc:
            report.update(status="failed", error=str(exc), failed_stage=stage[0])
            stage[0] = "finish"

    timer = QTimer(window)
    timer.timeout.connect(tick)
    timer.start(50)
    return app.exec()
