"""Release matrix gaps: 1080p rates, mixed channel layouts, and real encoder failure."""

import array
import json
import shutil
from dataclasses import replace

import pytest
from test_render_integration import ffmpeg, require_encoder

from stitch.export import export_project
from stitch.models import OutputSettings, Project
from stitch.paths import AppPaths
from stitch.render_plan import RenderError
from stitch.sources import add_video

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required"
    ),
]


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


@pytest.mark.parametrize("fps", [24, 30, 60, 120])
def test_1080p_delivery_rates(tmp_path, paths, fps):
    require_encoder("libx264")
    source = tmp_path / "1080p.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=red:size=1920x1080:rate={fps}",
        "-frames:v",
        "2",
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-preset",
        "ultrafast",
        source,
    )
    project = add_video(
        Project(
            output=OutputSettings(width=1920, height=1080, frame_rate=f"{fps}/1", profile="h264")
        ),
        source,
    )
    result = export_project(project, tmp_path / "output.mp4", paths)
    assert result.metadata.video.average_frame_rate == f"{fps}/1"
    assert result.metadata.video.duration == pytest.approx(2 / fps, abs=0.001)
    frames = ffmpeg("-i", result.path, "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "gray", "-")
    assert len(frames) == 2 * 1920 * 1080


@pytest.mark.parametrize("profile", ["h264", "hevc_main10", "prores_hq"])
def test_mixed_sample_rates_channel_layouts_and_unusual_paths(tmp_path, paths, profile):
    require_encoder(
        {"h264": "libx264", "hevc_main10": "libx265", "prores_hq": "prores_ks"}[profile]
    )
    project = Project(
        output=OutputSettings(width=160, height=90, frame_rate="30/1", profile=profile)
    )
    for i, (rate, layout, frequency) in enumerate(((44100, "mono", 440), (96000, "5.1", 880))):
        # Backslashes and apostrophes must remain literal paths, never shell/filter syntax.
        source = tmp_path / f"ગુજરાતી {i} ' quote \\ space.mov"
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=160x90:rate=30:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate={rate}:duration=0.5",
            "-af",
            f"aformat=channel_layouts={layout}",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s24le",
            source,
        )
        project = add_video(project, source)
    assert [s.cache.metadata.audio[0].channels for s in project.sources] == [1, 6]
    result = export_project(
        project, tmp_path / ("mixed.mov" if profile == "prores_hq" else "mixed.mp4"), paths
    )
    audio = result.metadata.audio[0]
    assert (audio.channels, audio.sample_rate) == (2, 48000)
    samples = array.array(
        "f", ffmpeg("-i", result.path, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    )
    for start, expected in ((0.1, 88), (0.6, 176)):
        chunk = samples[int(start * 48000) : int((start + 0.2) * 48000)]
        assert max(abs(s) for s in chunk) > 0.01
        assert sum(a <= 0 < b for a, b in zip(chunk, chunk[1:])) == pytest.approx(expected, abs=2)


def test_missing_encoder_and_missing_source_preserve_destination(tmp_path, paths, monkeypatch):
    source = tmp_path / "source.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=size=160x90:rate=30:duration=0.2", "-c:v", "mpeg4", source)
    project = add_video(
        Project(output=OutputSettings(width=160, height=90, profile="h264")), source
    )
    target = tmp_path / "existing.mp4"
    target.write_bytes(b"keep this existing output")
    from stitch.render_plan import build_segment_plan

    def unavailable_encoder(*args, **kwargs):
        plan = build_segment_plan(*args, **kwargs)
        command = list(plan.command)
        command[command.index("-c:v") + 1] = "stitch_nonexistent_encoder"
        return replace(plan, command=tuple(command))

    monkeypatch.setattr("stitch.render.build_segment_plan", unavailable_encoder)
    with pytest.raises(RenderError, match="FFmpeg"):
        export_project(project, target, paths, overwrite=True)
    logs = list(paths.jobs.rglob("ffmpeg.log"))
    assert len(logs) == 1 and "stitch_nonexistent_encoder" in logs[0].read_text()
    assert any(
        json.loads(p.read_text())["state"] == "failed" for p in paths.jobs.glob("*/status.json")
    )
    assert target.read_bytes() == b"keep this existing output"
    source.unlink()
    with pytest.raises(RenderError, match="missing"):
        export_project(project, target, paths, overwrite=True)
    assert target.read_bytes() == b"keep this existing output"
