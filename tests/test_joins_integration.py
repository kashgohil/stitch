import array
import json
import math
import shutil
import threading

import pytest
from test_render_integration import ffmpeg, require_encoder

from stitch.document import change_project
from stitch.export import export_project
from stitch.models import AudioSettings, OutputSettings, Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.render_plan import RenderError
from stitch.sources import add_video, set_overlay

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
    ),
]


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


@pytest.fixture
def project(tmp_path):
    project = Project(
        output=OutputSettings(width=160, height=90, frame_rate="30/1", profile="h264")
    )
    for i, (color, fps, hz) in enumerate((("red", 24, 440), ("blue", 60, 880), ("lime", 30, 1320))):
        source = tmp_path / f"ગુજરાતી clip {i}.mov"
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size=160x90:rate={fps}:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={hz}:sample_rate=44100:duration=0.5",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s24le",
            source,
        )
        project = add_video(project, source)
    return change_project(
        project,
        output=project.output.model_copy(
            update={"join_mode": "crossfade", "crossfade_duration": 0.2}
        ),
    )


@pytest.mark.parametrize("profile", ["h264", "hevc_main10", "prores_hq"])
def test_three_clip_blends_timing_and_original_only_commands(project, paths, tmp_path, profile):
    require_encoder(
        {"h264": "libx264", "hevc_main10": "libx265", "prores_hq": "prores_ks"}[profile]
    )
    project = change_project(project, output=project.output.model_copy(update={"profile": profile}))
    destination = tmp_path / ("final.mov" if profile == "prores_hq" else "final.mp4")
    updates = []
    result = export_project(project, destination, paths, progress=updates.append)
    assert result.metadata.video.duration == pytest.approx(1.1, abs=0.001)
    assert result.metadata.audio[0].duration == pytest.approx(1.1, abs=0.025)
    pixels = ffmpeg("-i", destination, "-map", "0:v:0", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
    assert len(pixels) == 33 * 160 * 90 * 3

    def pixel(frame):
        offset = frame * 160 * 90 * 3 + (45 * 160 + 80) * 3
        return tuple(pixels[offset : offset + 3])

    assert pixel(0)[0] > 240
    r, g, b = pixel(12)  # Midpoint of first blend.
    assert 105 < r < 145 and 105 < b < 145 and g < 15
    r, g, b = pixel(21)  # Midpoint of second blend.
    assert 105 < g < 145 and 105 < b < 145 and r < 15
    assert pixel(32)[1] > 240
    samples = array.array(
        "f", ffmpeg("-i", destination, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    )
    assert max(abs(x) for x in samples) < 0.14

    # Both tones must be present and exchange gain across the first overlap.
    def tone_amplitude(start, hz):
        values = samples[round(start * 48000) : round((start + 0.05) * 48000)]
        return (
            2
            * abs(
                sum(
                    value
                    * complex(
                        math.cos(2 * math.pi * hz * i / 48000),
                        math.sin(2 * math.pi * hz * i / 48000),
                    )
                    for i, value in enumerate(values)
                )
            )
            / len(values)
        )

    assert tone_amplitude(0.325, 440) > tone_amplitude(0.425, 440) * 2
    assert tone_amplitude(0.425, 880) > tone_amplitude(0.325, 880) * 2
    assert updates[-1].fraction == 1
    assert [u.fraction for u in updates] == sorted(u.fraction for u in updates)
    for status in (result.job / "segments").glob("*/status.json"):
        command = json.loads(status.read_text())["command"]
        inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
        assert all(path in {str(s.path) for s in project.sources} for path in inputs)
        assert len(inputs) <= 2
    assert not list(result.job.rglob("segment.mov"))


@pytest.mark.parametrize("behavior", ["loop", "stop"])
def test_overlay_and_muted_source_audio_use_shortened_duration(project, paths, tmp_path, behavior):
    overlay = tmp_path / "overlay.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=1100:duration=0.25", "-c:a", "pcm_s24le", overlay)
    project = set_overlay(project, overlay)
    project = change_project(
        project,
        clips=tuple(
            c.model_copy(update={"audio": AudioSettings(enabled=False)}) for c in project.clips
        ),
        overlay=project.overlay.model_copy(update={"end_behavior": behavior}),
        output=project.output.model_copy(update={"profile": "prores_hq"}),
    )
    result = export_project(project, tmp_path / "audio.mov", paths)
    samples = array.array(
        "f", ffmpeg("-i", result.path, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    )
    assert len(samples) == 52800
    tail = max(abs(x) for x in samples[48000:])
    assert tail > 0.05 if behavior == "loop" else tail == 0


def test_crossfade_cancellation_preserves_existing_output(project, paths, tmp_path):
    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"keep")
    cancel = threading.Event()

    def progress(update):
        if "crossfade" in update.message:
            cancel.set()

    with pytest.raises(RenderCancelled):
        export_project(
            project, destination, paths, overwrite=True, cancel=cancel, progress=progress
        )
    assert destination.read_bytes() == b"keep"


@pytest.mark.parametrize("size", [(3840, 2160), (7680, 4320)])
@pytest.mark.parametrize(
    "profile,pixel_format",
    [("h264", "yuv420p"), ("hevc_main10", "yuv420p10le"), ("prores_hq", "yuv422p10le")],
)
def test_high_resolution_high_fps_crossfade(tmp_path, paths, size, profile, pixel_format):
    require_encoder(
        {"h264": "libx264", "hevc_main10": "libx265", "prores_hq": "prores_ks"}[profile]
    )
    width, height = size
    source = tmp_path / "native10bit.mkv"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=blue:size={width}x{height}:rate=120",
        "-frames:v",
        "3",
        "-c:v",
        "ffv1",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p10le",
        source,
    )
    project = Project(
        output=OutputSettings(
            width=width,
            height=height,
            frame_rate="120/1",
            profile=profile,
            join_mode="crossfade",
            crossfade_duration=0.008,
        )
    )
    project = add_video(add_video(project, source), source)
    result = export_project(
        project, tmp_path / ("high.mov" if profile == "prores_hq" else "high.mp4"), paths
    )
    video = result.metadata.video
    assert (video.width, video.height, video.pixel_format) == (width, height, pixel_format)
    assert video.average_frame_rate == "120/1"
    assert video.duration == pytest.approx(5 / 120, abs=0.000001)
    # No source audio: synthesized silence spans bodies and overlap.
    assert not any(ffmpeg("-i", result.path, "-map", "0:a:0", "-f", "s16le", "-"))


def test_fractional_rate_and_middle_clip_without_body(project, paths, tmp_path):
    project = change_project(
        project,
        output=project.output.model_copy(
            update={
                "crossfade_duration": 0.25,
                "frame_rate": "120/1",
                "profile": "prores_hq",
            }
        ),
    )
    result = export_project(project, tmp_path / "no-middle-body.mov", paths)
    assert result.metadata.video.duration == 1
    project = change_project(
        project,
        output=project.output.model_copy(
            update={
                "crossfade_duration": 0.1,
                "frame_rate": "60000/1001",
            }
        ),
    )
    result = export_project(project, tmp_path / "fractional.mov", paths)
    assert result.metadata.video.average_frame_rate == "60000/1001"
    assert result.metadata.video.duration == pytest.approx(78 * 1001 / 60000, abs=0.000001)
    assert result.metadata.audio[0].duration == pytest.approx(78 * 1001 / 60000, abs=1 / 48000)


def test_failed_crossfade_and_disk_preflight_preserve_existing_output(
    project, paths, tmp_path, monkeypatch
):
    from dataclasses import replace
    from types import SimpleNamespace

    from stitch.render_plan import build_segment_plan

    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"keep")
    with monkeypatch.context() as patch:
        patch.setattr("stitch.export.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
        with pytest.raises(RenderError, match="space"):
            export_project(project, destination, paths, overwrite=True)
    assert not list(paths.jobs.rglob("ffmpeg.log"))

    def broken(*args, **kwargs):
        plan = build_segment_plan(*args, **kwargs)
        command = list(plan.command)
        command[command.index("-c:v") + 1] = "stitch_missing_encoder"
        return replace(plan, command=tuple(command))

    monkeypatch.setattr("stitch.export.build_segment_plan", broken)
    with pytest.raises(RenderError, match="FFmpeg"):
        export_project(project, destination, paths, overwrite=True)
    assert destination.read_bytes() == b"keep"
    assert any("stitch_missing_encoder" in p.read_text() for p in paths.jobs.rglob("ffmpeg.log"))


@pytest.mark.parametrize(
    "mode,overlap,single",
    [("hard_cut", 0.2, False), ("crossfade", 0, False), ("crossfade", 10, True)],
)
def test_no_transition_cases_keep_original_render_path(
    project, paths, tmp_path, monkeypatch, mode, overlap, single
):
    project = change_project(
        project,
        clips=project.clips[:1] if single else project.clips,
        output=project.output.model_copy(update={"join_mode": mode, "crossfade_duration": overlap}),
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("No crossfade should render")

    monkeypatch.setattr("stitch.export.render_join_chunk", unexpected)
    result = export_project(project, tmp_path / "cuts.mp4", paths)
    assert result.metadata.video.duration == pytest.approx(0.5 if single else 1.5, abs=0.000001)
