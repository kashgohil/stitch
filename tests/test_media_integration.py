"""Tiny generated fixtures test the real FFmpeg contract without source footage."""

import shutil
import subprocess

import pytest

from stitch.media import MediaError, probe_media

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
        reason="FFmpeg and ffprobe are required for integration tests",
    ),
]


def generate(*args):
    subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-nostdin", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_real_video_with_rotation_and_fractional_fps(tmp_path):
    path = tmp_path / "ગુજરાતી video.mp4"
    rotated = tmp_path / "portrait rotation.mp4"
    generate(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=30000/1001",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "0.3",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-shortest",
        path,
    )
    generate("-display_rotation:v:0", "90", "-i", path, "-c", "copy", rotated)
    info = probe_media(rotated)
    assert info.video.average_frame_rate == "30000/1001"
    assert info.video.display_dimensions == (90, 160)
    assert info.video.bit_depth == 8
    assert info.audio[0].sample_rate == 48000
    assert info.duration == pytest.approx(0.3, abs=0.05)


@pytest.mark.parametrize("dimensions", [(3840, 2160), (7680, 4320)])
def test_real_high_resolution_10bit_120fps_metadata(tmp_path, dimensions):
    width, height = dimensions
    path = tmp_path / "high resolution.mkv"
    generate(
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
        "-vf",
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        path,
    )
    info = probe_media(path)
    assert (info.video.width, info.video.height) == dimensions
    assert info.video.average_frame_rate == "120/1"
    assert info.video.bit_depth == 10
    assert info.video.color_transfer == "bt709"
    assert not info.audio


def test_real_24bit_audio_and_corrupt_file(tmp_path):
    path = tmp_path / "audio.wav"
    generate(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=96000",
        "-t",
        "0.1",
        "-c:a",
        "pcm_s24le",
        path,
    )
    info = probe_media(path)
    assert info.video is None
    assert info.audio[0].bit_depth == 24
    assert info.audio[0].sample_rate == 96000
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not video")
    with pytest.raises(MediaError, match="Cannot inspect"):
        probe_media(broken)
