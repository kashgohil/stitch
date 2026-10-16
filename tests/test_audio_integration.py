import array
import json
import math
import shutil
import subprocess
import threading

import pytest

from stitch.document import change_project, replace_clip
from stitch.export import export_project
from stitch.models import AudioSettings, OutputSettings, Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.render_plan import RenderError
from stitch.sources import add_video, set_overlay

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
        output=OutputSettings(width=160, height=90, frame_rate="30/1", profile="prores_hq")
    )
    for index, frequency in enumerate((440, 880)):
        path = tmp_path / f"clip{index}.mov"
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "color=size=160x90:rate=30:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=44100:duration=0.5",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s24le",
            path,
        )
        project = add_video(project, path)
    audio = tmp_path / "ગુજરાતી music's.wav"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1100:sample_rate=96000:duration=0.25",
        "-c:a",
        "pcm_s24le",
        audio,
    )
    return set_overlay(project, audio)


def samples(path):
    return array.array("f", ffmpeg("-i", path, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-"))


def amplitude(pcm, frequency, start, duration=0.1):
    data = pcm[round(start * 48000) : round((start + duration) * 48000)]
    cosine = sum(v * math.cos(2 * math.pi * frequency * i / 48000) for i, v in enumerate(data))
    sine = sum(v * math.sin(2 * math.pi * frequency * i / 48000) for i, v in enumerate(data))
    return 2 * math.hypot(cosine, sine) / len(data)


@pytest.mark.parametrize(
    "profile,encoder", [("h264", "libx264"), ("hevc_main10", "libx265"), ("prores_hq", "prores_ks")]
)
@pytest.mark.parametrize("behavior", ["stop", "loop"])
def test_overlay_modes_and_source_gains_across_boundary(
    project, paths, tmp_path, profile, encoder, behavior
):
    if encoder not in ffmpeg("-encoders").decode():
        pytest.skip(f"Missing {encoder}")
    for index, clip in enumerate(project.clips):
        project = replace_clip(
            project, clip.model_copy(update={"audio": AudioSettings(gain_db=-6 * (index + 1))})
        )
    project = change_project(
        project,
        output=project.output.model_copy(update={"profile": profile}),
        overlay=project.overlay.model_copy(
            update={"end_behavior": behavior, "audio": AudioSettings(gain_db=-9)}
        ),
    )
    output = tmp_path / ("final.mov" if profile == "prores_hq" else "final.mp4")
    updates = []
    result = export_project(project, output, paths, progress=updates.append)
    pcm = samples(output)
    first = amplitude(pcm, 440, 0.1)
    second = amplitude(pcm, 880, 0.6)
    assert second / first == pytest.approx(10 ** (-6 / 20), abs=0.025)
    overlay_early = amplitude(pcm, 1100, 0.1)
    assert overlay_early / first == pytest.approx(10 ** (-3 / 20), abs=0.025)
    overlay_late = amplitude(pcm, 1100, 0.6)
    if behavior == "loop":
        assert overlay_late / overlay_early == pytest.approx(1, abs=0.04)
    else:
        assert overlay_late < 0.001
    assert amplitude(pcm, 440, 0.6) < 0.001  # Clip one must not bleed past its boundary.
    assert result.metadata.video.duration == 1
    assert result.metadata.audio[0].sample_rate == 48000
    assert result.metadata.audio[0].codec == ("pcm_s24le" if profile == "prores_hq" else "aac")
    assert not (result.job / "overlay.wav").exists()
    assert (result.job / "overlay.log").exists()
    fractions = [update.fraction for update in updates]
    assert fractions == sorted(fractions)


def test_overlay_video_container_uses_audio_length_and_origin(project, paths, tmp_path):
    # Video lasts longer than its audio and both have nonzero timestamps.
    container = tmp_path / "audio inside video.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=size=160x90:rate=30:duration=0.5",
        "-itsoffset",
        "0.1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1100:duration=0.2",
        "-c:v",
        "mpeg4",
        "-c:a",
        "pcm_s24le",
        "-output_ts_offset",
        "5",
        container,
    )
    project = set_overlay(project, container)
    for clip in project.clips:
        project = replace_clip(
            project, clip.model_copy(update={"audio": AudioSettings(enabled=False)})
        )
    project = change_project(
        project, overlay=project.overlay.model_copy(update={"end_behavior": "loop"})
    )
    result = export_project(project, tmp_path / "loop.mov", paths)
    pcm = samples(result.path)
    assert amplitude(pcm, 1100, 0.02) > 0.05
    assert amplitude(pcm, 1100, 0.62) > 0.05  # No padding to the video container's length.


def test_changed_overlay_rejected_before_render_and_muted_overlay_ignored(project, paths, tmp_path):
    source = next(s for s in project.sources if s.id == project.overlay.source_id)
    source.path.unlink()
    with pytest.raises(RenderError, match="missing"):
        export_project(project, tmp_path / "missing.mov", paths)
    muted = change_project(
        project, overlay=project.overlay.model_copy(update={"audio": AudioSettings(enabled=False)})
    )
    result = export_project(muted, tmp_path / "muted.mov", paths)
    assert amplitude(samples(result.path), 1100, 0.1) < 0.001


@pytest.mark.parametrize("stage", ["preparing_audio", "assembling"])
def test_cancel_overlay_work_preserves_output(project, paths, tmp_path, stage):
    output = tmp_path / "existing.mov"
    output.write_bytes(b"keep")
    cancel = threading.Event()

    def progress(update):
        if update.stage == stage:
            cancel.set()

    with pytest.raises(RenderCancelled):
        export_project(project, output, paths, overwrite=True, cancel=cancel, progress=progress)
    assert output.read_bytes() == b"keep"
    (job,) = paths.jobs.iterdir()
    assert json.loads((job / "status.json").read_text())["state"] == "cancelled"


def test_loud_source_gain_and_overlay_mix_limit_peaks_without_losing_duration(
    project, paths, tmp_path
):
    loud = tmp_path / "loud.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=size=160x90:rate=30:duration=0.5",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.8*sin(2*PI*440*t):s=48000:d=0.5",
        "-c:v",
        "mpeg4",
        "-c:a",
        "pcm_s24le",
        loud,
    )
    project = add_video(Project(output=project.output), loud)
    project = set_overlay(project, loud)
    project = replace_clip(
        project, project.clips[0].model_copy(update={"audio": AudioSettings(gain_db=12)})
    )
    project = change_project(
        project, overlay=project.overlay.model_copy(update={"audio": AudioSettings(gain_db=12)})
    )
    result = export_project(project, tmp_path / "limited.mov", paths)
    stereo = array.array("f", ffmpeg("-i", result.path, "-map", "0:a:0", "-f", "f32le", "-"))
    peak = max(abs(value) for value in stereo)
    assert 0.9 < peak <= 0.95001
    assert len(stereo) == 24000 * 2
    assert result.metadata.video.duration == result.metadata.audio[0].duration == 0.5
