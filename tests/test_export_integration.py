import array
import json
import shutil
import subprocess
import threading

import pytest

from stitch.export import export_project
from stitch.models import OutputSettings, Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.render_plan import RenderError
from stitch.sources import add_video

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
        reason="FFmpeg is required",
    ),
]


def ffmpeg(*args):
    return subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-nostdin", *map(str, args)],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("p", "c", "jobs", "logs")))


@pytest.fixture
def project(tmp_path):
    project = Project(
        output=OutputSettings(width=320, height=180, frame_rate="30/1", profile="h264")
    )
    for index, (size, rate, color, frequency) in enumerate(
        [
            ("160x90", "24", "red", 440),
            ("320x180", "60", "blue", 880),
        ]
    ):
        path = tmp_path / f"ગુજરાતી clip'{index}.mp4"
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size={size}:rate={rate}:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=44100:duration=0.5",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            path,
        )
        project = add_video(project, path)
    return project


@pytest.mark.parametrize("profile", ["h264", "hevc_main10", "prores_hq"])
def test_mixed_clips_export_without_video_reencode(project, paths, tmp_path, profile):
    encoder = {"h264": "libx264", "hevc_main10": "libx265", "prores_hq": "prores_ks"}[profile]
    if encoder not in ffmpeg("-encoders").decode():
        pytest.skip(f"Local FFmpeg lacks {encoder}")
    project = project.model_copy(
        update={"output": project.output.model_copy(update={"profile": profile})}
    )
    destination = tmp_path / ("final.mov" if profile == "prores_hq" else "final.mp4")
    updates = []
    result = export_project(project, destination, paths, progress=updates.append)
    assert destination.exists()
    assert result.metadata.video.duration == pytest.approx(1, abs=0.001)
    assert result.metadata.audio[0].duration == pytest.approx(1, abs=0.025)
    report = json.loads((result.job / "status.json").read_text())
    assert report["state"] == "succeeded"
    command = report["command"]
    assert command[command.index("-c:v") + 1] == "copy"
    assert updates[-1].fraction == 1 and updates[-1].stage == "complete"
    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)
    assert not list(result.job.rglob("segment.mov"))
    assert list(result.job.rglob("ffmpeg.log"))
    assert not list(tmp_path.glob(".stitch-export-*"))
    # Decode frames and samples on both sides of the boundary, not just container metadata.
    pixels = ffmpeg("-i", destination, "-map", "0:v:0", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
    frame_bytes = 320 * 180 * 3
    assert len(pixels) == frame_bytes * 30
    center = (90 * 320 + 160) * 3
    assert pixels[center] > 200 and pixels[center + 2] < 20
    center += 20 * frame_bytes
    assert pixels[center + 2] > 200 and pixels[center] < 20
    pcm = array.array(
        "f", ffmpeg("-i", destination, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    )

    def crossings(start):
        data = pcm[int(start * 48000) : int((start + 0.2) * 48000)]
        return sum(a <= 0 < b for a, b in zip(data, data[1:]))

    assert crossings(0.1) == pytest.approx(88, abs=2)
    assert crossings(0.6) == pytest.approx(176, abs=2)


@pytest.mark.parametrize("stage", ["rendering", "assembling", "validating"])
def test_cancel_preserves_existing_output(project, paths, tmp_path, stage):
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"precious old output")
    cancel = threading.Event()

    def update(progress):
        if progress.stage == stage:
            cancel.set()

    with pytest.raises(RenderCancelled):
        export_project(project, output, paths, overwrite=True, cancel=cancel, progress=update)
    assert output.read_bytes() == b"precious old output"
    assert not list(tmp_path.glob(".stitch-export-*"))
    (job,) = paths.jobs.iterdir()
    assert json.loads((job / "status.json").read_text())["state"] == "cancelled"


def test_failed_assembly_retains_segments_logs_and_previous_output(
    project, paths, tmp_path, monkeypatch
):
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"old output")

    def fail(command, log_path, **kwargs):
        log_path.write_text("simulated encoder/disk failure")
        return 1

    monkeypatch.setattr("stitch.export.run_ffmpeg", fail)
    with pytest.raises(RenderError, match="Assembly failed"):
        export_project(project, output, paths, overwrite=True)
    assert output.read_bytes() == b"old output"
    (job,) = paths.jobs.iterdir()
    assert len(list(job.rglob("segment.mov"))) == 2
    assert (job / "assembly.log").read_text() == "simulated encoder/disk failure"
    assert json.loads((job / "status.json").read_text())["state"] == "failed"


def test_disk_preflight_refuses_before_starting_render(project, paths, tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("stitch.export.shutil.disk_usage", lambda path: SimpleNamespace(free=1))

    def never(*args, **kwargs):
        pytest.fail("Render must not start on an unsafe disk estimate")

    monkeypatch.setattr("stitch.export.render_segment", never)
    with pytest.raises(RenderError, match="disk space"):
        export_project(project, tmp_path / "final.mp4", paths)


def test_cli_export_and_overwrite_guard(project, paths, tmp_path, monkeypatch, capsys):
    from stitch.app import main
    from stitch.project_io import save_project

    monkeypatch.setattr("stitch.app.AppPaths.default", lambda: paths)
    monkeypatch.setattr("stitch.app.configure_logging", lambda paths: None)
    project_file = tmp_path / "project.json"
    save_project(project, project_file)
    output = tmp_path / "final.mp4"
    assert main(["--export", str(project_file), "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == str(output)
    original = output.read_bytes()
    assert main(["--export", str(project_file), "--output", str(output)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == original
