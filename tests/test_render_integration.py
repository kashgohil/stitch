"""Real encode/decode checks. Short fixtures test pixels and samples, not just exit status."""

import array
import json
import shutil
import subprocess

import pytest

from stitch.document import replace_clip
from stitch.models import AudioSettings, Crop, OutputSettings, Project
from stitch.paths import AppPaths
from stitch.render import render_segment
from stitch.render_plan import RenderOptions
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
    return AppPaths(*(tmp_path / name for name in ("projects", "proxies", "jobs", "logs")))


def require_encoder(encoder):
    if encoder not in ffmpeg("-encoders").decode():
        pytest.skip(f"Local FFmpeg lacks {encoder}")


@pytest.mark.parametrize(
    "profile,encoder,format,depth",
    [
        ("h264", "libx264", "yuv420p", 8),
        ("hevc_main10", "libx265", "yuv420p10le", 10),
        ("prores_hq", "prores_ks", "yuv422p10le", 10),
    ],
)
def test_real_profiles_with_10bit_source(tmp_path, paths, profile, encoder, format, depth):
    require_encoder(encoder)
    source = tmp_path / "10-bit source.mkv"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=60",
        "-t",
        "0.2",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv420p10le",
        source,
    )
    project = add_video(
        Project(
            output=OutputSettings(width=320, height=180, frame_rate="60000/1001", profile=profile)
        ),
        source,
    )
    result = render_segment(project, project.clips[0].id, paths, timeout=30)
    assert result.metadata.video.pixel_format == format
    assert result.metadata.video.bit_depth == depth
    assert result.metadata.video.average_frame_rate == "60000/1001"
    assert result.metadata.audio[0].codec == "pcm_s24le"
    assert json.loads((result.job / "status.json").read_text())["state"] == "succeeded"
    # Silent inputs still supply a full-duration stereo track for later concatenation.
    samples = ffmpeg("-i", result.path, "-map", "0:a:0", "-f", "s16le", "-")
    assert samples and not any(samples)


@pytest.mark.parametrize("offset", [0, 5])
def test_trim_preserves_audio_delay_and_nonzero_source_clock(tmp_path, paths, offset):
    require_encoder("libx264")
    source = tmp_path / "delayed audio.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:size=160x90:rate=30:duration=1.2,"
        "drawbox=color=white:t=fill:enable='gte(t,0.5)'",
        "-itsoffset",
        "0.3",
        "-f",
        "lavfi",
        "-i",
        "sine=sample_rate=44100:duration=0.7",
        "-c:v",
        "mpeg4",
        "-c:a",
        "pcm_s24le",
        "-output_ts_offset",
        offset,
        source,
    )
    project = add_video(
        Project(output=OutputSettings(width=160, height=90, frame_rate="30/1", profile="h264")),
        source,
    )
    clip = project.clips[0].model_copy(update={"in_point": 0.1, "out_point": 0.9})
    project = replace_clip(project, clip)
    result = render_segment(project, clip.id, paths, timeout=30)
    assert result.metadata.video.duration == pytest.approx(0.8, abs=1e-5)
    pcm = ffmpeg("-i", result.path, "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-")
    samples = array.array("f", pcm)
    first_sound = next(i for i, value in enumerate(samples) if abs(value) > 0.001)
    assert first_sound / 48000 == pytest.approx(0.2, abs=0.002)
    raw = ffmpeg("-i", result.path, "-map", "0:v:0", "-pix_fmt", "gray", "-f", "rawvideo", "-")
    frame_size = 160 * 90
    means = [sum(raw[i : i + frame_size]) / frame_size for i in range(0, len(raw), frame_size)]
    first_white = next(i for i, mean in enumerate(means) if mean > 200)
    assert first_white == 12  # Source event at 0.5 seconds, minus a 0.1-second trim.


def test_crop_rotation_fit_and_mute(tmp_path, paths):
    require_encoder("libx264")
    source, rotated = tmp_path / "source.mp4", tmp_path / "rotated.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=red:size=160x90:rate=30:duration=0.2",
        "-f",
        "lavfi",
        "-i",
        "sine=duration=0.2",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        source,
    )
    ffmpeg("-display_rotation:v:0", "90", "-i", source, "-c", "copy", rotated)
    project = add_video(
        Project(output=OutputSettings(width=120, height=180, frame_rate="30/1", profile="h264")),
        rotated,
    )
    clip = project.clips[0].model_copy(
        update={
            "crop": Crop(x=10, y=20, width=60, height=100),
            "audio": AudioSettings(enabled=False),
        }
    )
    result = render_segment(replace_clip(project, clip), clip.id, paths, timeout=30)
    assert result.metadata.video.rotation == 0
    raw = ffmpeg("-i", result.path, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")

    def pixel(x, y):
        pos = (y * 120 + x) * 3
        return raw[pos : pos + 3]

    assert max(pixel(0, 0)) < 10
    assert pixel(60, 90)[0] > 200
    # No upscaling: 60x100 crop centered inside 120x180 canvas.
    assert max(pixel(20, 90)) < 10 and pixel(40, 90)[0] > 200
    assert not any(ffmpeg("-i", result.path, "-map", "0:a:0", "-f", "s16le", "-"))


@pytest.mark.parametrize(
    "width,height,profile,encoder",
    [
        (3840, 2160, "h264", "libx264"),
        (7680, 4320, "hevc_main10", "libx265"),
    ],
)
def test_real_4k_8k_120fps_render(tmp_path, paths, width, height, profile, encoder):
    require_encoder(encoder)
    source = tmp_path / "high-resolution.mkv"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=size={width}x{height}:rate=120",
        "-frames:v",
        "1",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv420p10le",
        source,
    )
    project = add_video(
        Project(
            output=OutputSettings(width=width, height=height, frame_rate="120/1", profile=profile)
        ),
        source,
    )
    result = render_segment(
        project, project.clips[0].id, paths, options=RenderOptions(preset="veryfast"), timeout=60
    )
    assert (result.metadata.video.width, result.metadata.video.height) == (width, height)
    assert result.metadata.video.average_frame_rate == "120/1"
    assert result.metadata.video.bit_depth == (10 if profile == "hevc_main10" else 8)


@pytest.mark.parametrize("in_point,out_point", [(0, 0.2), (0.8, 1.0)])
def test_audio_outside_trim_becomes_silence(tmp_path, paths, in_point, out_point):
    require_encoder("libx264")
    source = tmp_path / "short-audio.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=size=160x90:rate=30:duration=1.2",
        "-itsoffset",
        "0.3",
        "-f",
        "lavfi",
        "-i",
        "sine=duration=0.2",
        "-c:v",
        "mpeg4",
        "-c:a",
        "pcm_s24le",
        source,
    )
    project = add_video(
        Project(output=OutputSettings(width=160, height=90, frame_rate="30/1", profile="h264")),
        source,
    )
    clip = project.clips[0].model_copy(update={"in_point": in_point, "out_point": out_point})
    result = render_segment(replace_clip(project, clip), clip.id, paths, timeout=30)
    samples = ffmpeg("-i", result.path, "-map", "0:a:0", "-f", "s16le", "-")
    assert samples and not any(samples)


def test_segment_cli_renders_saved_project(tmp_path, paths, monkeypatch, capsys):
    from stitch.app import main
    from stitch.project_io import save_project

    require_encoder("libx264")
    source = tmp_path / "ગુજરાતી source.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30", "-t", "0.1", "-c:v", "mpeg4", source
    )
    project = add_video(
        Project(output=OutputSettings(width=160, height=90, frame_rate="30/1", profile="h264")),
        source,
    )
    path = tmp_path / "saved.json"
    save_project(project, path)
    monkeypatch.setattr("stitch.app.AppPaths.default", lambda: paths)
    monkeypatch.setattr("stitch.app.configure_logging", lambda paths: None)
    assert main(["--render-clip", str(path), "--clip-index", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["metadata"]["video"]["pixel_format"] == "yuv420p"
    assert report["path"].endswith("segment.mov")


def test_variable_frame_rate_sar_and_sdr_color_tags(tmp_path, paths):
    require_encoder("libx264")
    source = tmp_path / "variable-rate.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=30:duration=1",
        "-vf",
        "select='not(eq(mod(n,3),1))',setsar=4/3,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited",
        "-fps_mode:v",
        "vfr",
        "-c:v",
        "mpeg4",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        source,
    )
    project = add_video(
        Project(output=OutputSettings(width=320, height=180, frame_rate="60/1", profile="h264")),
        source,
    )
    assert project.sources[0].cache.metadata.video.average_frame_rate != "30/1"
    result = render_segment(project, project.clips[0].id, paths, timeout=30)
    video = result.metadata.video
    assert video.average_frame_rate == "60/1" and video.sample_aspect_ratio == "1/1"
    assert (video.color_primaries, video.color_transfer, video.color_matrix) == (
        "bt709",
        "bt709",
        "bt709",
    )
    # Decode confirms a regular frame count rather than merely trusting a rate tag.
    decoded = ffmpeg("-i", result.path, "-map", "0:v:0", "-pix_fmt", "gray", "-f", "rawvideo", "-")
    assert len(decoded) == 60 * 320 * 180
