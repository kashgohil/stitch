import json
import subprocess

import pytest

from stitch.media import MediaError, file_identity, parse_metadata, probe_media


@pytest.fixture
def payload():
    return {
        "format": {"format_name": "mov,mp4", "duration": "12.5"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 7680,
                "height": 4320,
                "pix_fmt": "yuv420p10le",
                "avg_frame_rate": "120000/1001",
                "r_frame_rate": "120/1",
                "time_base": "1/120000",
                "sample_aspect_ratio": "1:1",
                "color_primaries": "bt2020",
                "color_transfer": "smpte2084",
                "color_space": "bt2020nc",
                "side_data_list": [{"rotation": -90}],
                "duration": "12.5",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "sample_fmt": "fltp",
                "duration": "12.5",
            },
        ],
        "pixel_formats": [{"name": "yuv420p10le", "components": [{"bit_depth": 10}] * 3}],
    }


def test_normalizes_8k_fractional_fps_rotation_and_color(payload):
    info = parse_metadata(payload)
    assert info.duration == 12.5
    assert info.video.width == 7680
    assert info.video.display_dimensions == (4320, 7680)
    assert info.video.rotation == 270
    assert info.video.average_frame_rate == "120000/1001"
    assert info.video.real_frame_rate == "120/1"
    assert info.video.sample_aspect_ratio == "1/1"
    assert info.video.bit_depth == 10
    assert info.video.color_transfer == "smpte2084"
    assert info.audio[0].sample_rate == 48000
    assert info.audio[0].bit_depth is None  # Float decoder output is not source bit depth.


def test_unknown_optional_fields_stay_unknown(payload):
    stream = payload["streams"][0]
    stream.update(avg_frame_rate="0/0", r_frame_rate="N/A", color_transfer="unknown")
    payload.pop("pixel_formats")
    info = parse_metadata(payload)
    assert info.video.average_frame_rate is None
    assert info.video.real_frame_rate is None
    assert info.video.color_transfer is None
    assert info.video.bit_depth is None


def test_cover_art_is_not_selected_as_video_and_audio_only_is_supported(payload):
    payload["streams"][0]["disposition"] = {"attached_pic": 1}
    payload["format"]["duration"] = "N/A"
    info = parse_metadata(payload)
    assert info.video is None
    assert len(info.audio) == 1
    assert info.duration == 12.5


def test_default_video_is_selected_and_all_audio_streams_preserved(payload):
    default = dict(
        payload["streams"][0], index=2, width=1920, height=1080, disposition={"default": 1}
    )
    payload["streams"].extend([default, dict(payload["streams"][1], index=3)])
    info = parse_metadata(payload)
    assert info.video.index == 2
    assert [s.index for s in info.audio] == [1, 3]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"streams": None},
        {"streams": [None]},
        {"streams": [{"codec_type": "video", "index": 0, "width": 0}]},
        {"streams": [{"codec_type": "audio", "index": 0, "codec_name": "aac"}]},
    ],
)
def test_invalid_or_durationless_media_is_reported(raw):
    with pytest.raises(MediaError):
        parse_metadata(raw)


def test_probe_uses_absolute_local_path_without_shell_or_frame_scan(tmp_path, monkeypatch, payload):
    path = tmp_path / "ગુજરાતી take $(x).mp4"
    path.touch()
    monkeypatch.setattr("stitch.media.shutil.which", lambda _: "/tools/ffprobe")

    def run(command, **kwargs):
        assert command[-2:] == ["-i", str(path)]
        assert "-count_frames" not in command and "-show_frames" not in command
        assert command[command.index("-protocol_whitelist") + 1] == "file"
        assert kwargs["timeout"] == 30
        assert not kwargs.get("shell")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("stitch.media.subprocess.run", run)
    assert probe_media(path).video.width == 7680


@pytest.mark.parametrize(
    "outcome, message",
    [
        (subprocess.TimeoutExpired("ffprobe", 30), "exceeded"),
        (PermissionError("denied"), "Cannot launch"),
        (subprocess.CompletedProcess([], 1, "", "Invalid data"), "Invalid data"),
        (subprocess.CompletedProcess([], 0, "{", ""), "invalid JSON"),
        (subprocess.CompletedProcess([], 0, "[]", ""), "invalid metadata"),
    ],
)
def test_probe_errors_are_actionable(tmp_path, monkeypatch, outcome, message):
    path = tmp_path / "bad.mp4"
    path.touch()
    monkeypatch.setattr("stitch.media.shutil.which", lambda _: "/tools/ffprobe")

    def run(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("stitch.media.subprocess.run", run)
    with pytest.raises(MediaError, match=message):
        probe_media(path)


def test_missing_sources_and_directories_are_rejected_before_probe(tmp_path):
    with pytest.raises(MediaError, match="missing"):
        probe_media(tmp_path / "missing.mp4")
    with pytest.raises(MediaError, match="regular file"):
        file_identity(tmp_path)
