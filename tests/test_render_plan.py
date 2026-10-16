from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from stitch.models import (
    AudioSettings,
    AudioStream,
    Clip,
    Crop,
    FileIdentity,
    MediaMetadata,
    OutputSettings,
    ProbeCache,
    Source,
    VideoStream,
)
from stitch.render_plan import RenderError, RenderOptions, build_segment_plan, fit_dimensions


@pytest.fixture
def source(tmp_path):
    return Source(
        path=tmp_path / "ગુજરાતી clip's ;name.mp4",
        cache=ProbeCache(
            identity=FileIdentity(size=1, mtime_ns=1, ctime_ns=1, device=1, inode=1),
            metadata=MediaMetadata(
                format_name="mov",
                duration=12,
                video=VideoStream(
                    index=2,
                    codec="hevc",
                    width=3840,
                    height=2160,
                    duration=10,
                    start_time=2,
                    bit_depth=10,
                    sample_aspect_ratio="1/1",
                    color_range="tv",
                ),
                audio=(AudioStream(index=3, codec="aac", start_time=2.25),),
            ),
        ),
    )


def test_plan_is_pure_deterministic_and_uses_original_path(source, tmp_path):
    clip = Clip(
        source_id=source.id,
        in_point=1.1,
        out_point=3.2,
        crop=Crop(x=20, y=30, width=1920, height=1080),
    )
    output = OutputSettings(frame_rate="30000/1001")
    plan = build_segment_plan(source, clip, output, tmp_path / "segment.mov")
    assert plan == build_segment_plan(source, clip, output, tmp_path / "segment.mov")
    assert not plan.destination.exists()
    args = plan.command
    assert args[args.index("-i") + 1] == str(source.path)
    assert args[args.index("-ss") + 1] == "3.100000000"
    graph = args[args.index("-filter_complex") + 1]
    assert graph.index("crop=") < graph.index("scale=") < graph.index("pad=")
    assert "[0:2]trim=start=3.100000000:end=5.200000000" in graph
    assert "[0:3]atrim=start=3.100000000" in graph
    assert "STARTPTS" not in graph
    assert "first_pts=0" in graph
    assert "scale=w=1920:h=1080" in graph
    assert plan.frame_count == 63 and plan.duration == Fraction(63063, 30000)
    assert args[args.index("-pix_fmt") + 1] == "+yuv420p10le"
    assert "pcm_s24le" in args and "-n" in args and "-y" not in args


@pytest.mark.parametrize(
    "profile,pixel_format,encoder",
    [
        ("hevc_main10", "yuv420p10le", "libx265"),
        ("h264", "yuv420p", "libx264"),
        ("prores_hq", "yuv422p10le", "prores_ks"),
    ],
)
def test_profiles_explicitly_select_depth(source, tmp_path, profile, pixel_format, encoder):
    plan = build_segment_plan(
        source, Clip(source_id=source.id), OutputSettings(profile=profile), tmp_path / "out.mov"
    )
    assert plan.pixel_format == pixel_format and encoder in plan.command
    assert bool(plan.warnings) == (profile == "h264")


def test_fit_respects_sar_rotation_and_upscaling():
    video = VideoStream(index=0, codec="mpeg4", width=720, height=576, sample_aspect_ratio="16/15")
    assert fit_dimensions(video, None, OutputSettings()) == (768, 576)
    rotated = video.model_copy(update={"rotation": 90})
    assert fit_dimensions(rotated, None, OutputSettings()) == (540, 720)
    small = VideoStream(index=0, codec="h264", width=640, height=480)
    assert fit_dimensions(small, None, OutputSettings()) == (640, 480)
    assert fit_dimensions(small, None, OutputSettings(allow_upscale=True)) == (2880, 2160)
    assert fit_dimensions(small, Crop(width=301, height=201), OutputSettings()) == (300, 200)
    with pytest.raises(RenderError, match="narrow"):
        fit_dimensions(small, Crop(width=1, height=200), OutputSettings())
    with pytest.raises(RenderError, match="rotation"):
        fit_dimensions(small.model_copy(update={"rotation": 45}), None, OutputSettings())


def test_silent_segment_and_gain(source, tmp_path):
    clip = Clip(source_id=source.id, audio=AudioSettings(enabled=False))
    plan = build_segment_plan(source, clip, OutputSettings(), tmp_path / "out.mov")
    graph = plan.command[plan.command.index("-filter_complex") + 1]
    assert "anullsrc=r=48000:cl=stereo" in graph and "[0:3]" not in graph
    plan = build_segment_plan(
        source,
        clip.model_copy(update={"audio": AudioSettings(gain_db=-6)}),
        OutputSettings(),
        tmp_path / "out.mov",
    )
    assert "volume=-6dB" in plan.command[plan.command.index("-filter_complex") + 1]


def test_invalid_or_unsupported_input_is_rejected(source, tmp_path):
    clip = Clip(source_id=source.id)
    with pytest.raises(RenderError, match="Inspect"):
        build_segment_plan(
            source.model_copy(update={"cache": None}), clip, OutputSettings(), tmp_path / "out.mov"
        )
    video = source.cache.metadata.video.model_copy(update={"color_transfer": "smpte2084"})
    hdr = source.model_copy(
        update={
            "cache": source.cache.model_copy(
                update={"metadata": source.cache.metadata.model_copy(update={"video": video})}
            )
        }
    )
    with pytest.raises(RenderError, match="HDR"):
        build_segment_plan(hdr, clip, OutputSettings(), tmp_path / "out.mov")
    with pytest.raises(ValidationError, match="duration"):
        build_segment_plan(
            source,
            clip.model_copy(update={"out_point": 99}),
            OutputSettings(),
            tmp_path / "out.mov",
        )
    with pytest.raises(RenderError, match="absolute"):
        build_segment_plan(source, clip, OutputSettings(), Path("relative.mov"))


@pytest.mark.parametrize("changes", [{"threads": 0}, {"preset": "fake"}, {"crf": 52}])
def test_invalid_encoder_options(changes):
    with pytest.raises(RenderError):
        replace(RenderOptions(), **changes)
