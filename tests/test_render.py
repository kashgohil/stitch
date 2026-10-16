import json
import subprocess
from dataclasses import replace

import pytest

from stitch.media import file_identity
from stitch.models import (
    AudioStream,
    Clip,
    MediaMetadata,
    OutputSettings,
    ProbeCache,
    Project,
    Source,
    VideoStream,
)
from stitch.paths import AppPaths
from stitch.process import progress_command
from stitch.render import render_segment, validate_segment
from stitch.render_plan import RenderError, build_segment_plan


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "source.mov"
    path.write_bytes(b"original media")
    metadata = MediaMetadata(
        format_name="mov",
        duration=1,
        video=VideoStream(
            index=0,
            codec="h264",
            width=320,
            height=180,
            duration=1,
            pixel_format="yuv420p",
            sample_aspect_ratio="1/1",
            average_frame_rate="30/1",
        ),
        audio=(
            AudioStream(
                index=1, codec="pcm_s24le", duration=1, sample_rate=48000, channels=2, bit_depth=24
            ),
        ),
    )
    source = Source(path=path, cache=ProbeCache(identity=file_identity(path), metadata=metadata))
    return Project(
        sources=(source,),
        clips=(Clip(source_id=source.id),),
        output=OutputSettings(width=320, height=180, frame_rate="30/1", profile="h264"),
    )


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


def test_runner_retains_command_and_validates_result(project, paths, monkeypatch):
    calls = []
    monkeypatch.setattr("stitch.render.shutil.which", lambda name: "ffmpeg-test")

    def run(command, log_path, **kwargs):
        calls.append((command, kwargs))
        assert not kwargs.get("shell")
        log_path.touch()
        return 0

    monkeypatch.setattr("stitch.render.run_ffmpeg", run)
    monkeypatch.setattr("stitch.render.probe_media", lambda path: project.sources[0].cache.metadata)
    result = render_segment(project, project.clips[0].id, paths, timeout=5)
    status = json.loads((result.job / "status.json").read_text())
    assert status["state"] == "succeeded" and status["frame_count"] == 30
    assert status["command"] == list(progress_command(calls[0][0]))
    assert result.path.parent == result.job and result.job.parent == paths.jobs
    assert (result.job / "ffmpeg.log").exists()


@pytest.mark.parametrize("failure", ["exit", "timeout", "launch", "changed", "bad_output"])
def test_failed_jobs_keep_diagnostics_without_touching_source(project, paths, monkeypatch, failure):
    monkeypatch.setattr("stitch.render.shutil.which", lambda name: "ffmpeg-test")

    def run(command, log_path, **kwargs):
        log_path.write_bytes(b"encoder diagnostic\n")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        if failure == "launch":
            raise OSError("cannot launch")
        return 1 if failure == "exit" else 0

    monkeypatch.setattr("stitch.render.run_ffmpeg", run)
    original_identity = project.sources[0].cache.identity
    identities = iter([original_identity, original_identity.model_copy(update={"size": 999})])
    if failure == "changed":
        monkeypatch.setattr("stitch.render.file_identity", lambda path: next(identities))
    metadata = project.sources[0].cache.metadata
    if failure == "bad_output":
        metadata = metadata.model_copy(
            update={"video": metadata.video.model_copy(update={"width": 2})}
        )
    monkeypatch.setattr("stitch.render.probe_media", lambda path: metadata)
    with pytest.raises(RenderError, match="diagnostics"):
        render_segment(project, project.clips[0].id, paths, timeout=5)
    (job,) = paths.jobs.iterdir()
    status = json.loads((job / "status.json").read_text())
    assert status["state"] == "failed" and status["error"]
    assert (job / "ffmpeg.log").read_text() == "encoder diagnostic\n"
    assert project.sources[0].path.read_bytes() == b"original media"


def test_stale_source_is_rejected_before_ffmpeg(project, paths, monkeypatch):
    monkeypatch.setattr("stitch.render.shutil.which", lambda name: "ffmpeg-test")
    project.sources[0].path.write_bytes(b"changed media")

    def never(*args, **kwargs):
        pytest.fail("FFmpeg must not run with stale metadata")

    monkeypatch.setattr("stitch.render.run_ffmpeg", never)
    with pytest.raises(RenderError, match="stale"):
        render_segment(project, project.clips[0].id, paths)


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 640},
        {"pixel_format": "yuv420p10le"},
        {"duration": 0.8},
        {"average_frame_rate": "24/1"},
        {"rotation": 90},
        {"start_time": 0.1},
    ],
)
def test_validation_rejects_incorrect_video(project, tmp_path, changes):
    plan = build_segment_plan(
        project.sources[0], project.clips[0], project.output, tmp_path / "out.mov"
    )
    metadata = project.sources[0].cache.metadata
    metadata = metadata.model_copy(update={"video": metadata.video.model_copy(update=changes)})
    with pytest.raises(RenderError):
        validate_segment(plan, metadata)


@pytest.mark.parametrize(
    "changes",
    [
        {"duration": 0.8},
        {"sample_rate": 44100},
        {"start_time": 0.1},
        {"channels": 1},
    ],
)
def test_validation_rejects_incorrect_audio(project, tmp_path, changes):
    plan = build_segment_plan(
        project.sources[0], project.clips[0], project.output, tmp_path / "out.mov"
    )
    metadata = project.sources[0].cache.metadata
    metadata = metadata.model_copy(
        update={"audio": (metadata.audio[0].model_copy(update=changes),)}
    )
    with pytest.raises(RenderError):
        validate_segment(plan, metadata)


def test_no_job_directory_or_encoder_reports_useful_error(project, paths, monkeypatch, tmp_path):
    monkeypatch.setattr("stitch.render.shutil.which", lambda name: None)
    with pytest.raises(RenderError, match="ffmpeg is missing"):
        render_segment(project, project.clips[0].id, paths)
    monkeypatch.setattr("stitch.render.shutil.which", lambda name: "ffmpeg-test")
    collision = tmp_path / "file-not-directory"
    collision.write_bytes(b"keep")
    with pytest.raises(RenderError, match="Cannot create"):
        render_segment(project, project.clips[0].id, replace(paths, jobs=collision))
    assert collision.read_bytes() == b"keep"
