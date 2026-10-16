import json

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
    Overlay,
    ProbeCache,
    Project,
    Source,
    VideoStream,
)
from stitch.project_io import ProjectError, load_project, save_project


@pytest.fixture
def source(tmp_path):
    return Source(
        path=tmp_path / "ગુજરાતી clips" / "take one.mp4",
        cache=ProbeCache(
            identity=FileIdentity(size=20, mtime_ns=1, ctime_ns=1, device=1, inode=1),
            metadata=MediaMetadata(
                format_name="mov,mp4",
                duration=10,
                video=VideoStream(
                    index=0,
                    codec="hevc",
                    width=3840,
                    height=2160,
                    average_frame_rate="60000/1001",
                    pixel_format="yuv420p10le",
                    bit_depth=10,
                ),
                audio=(AudioStream(index=1, codec="aac", sample_rate=48000, channels=2),),
            ),
        ),
    )


def test_complete_project_round_trip_without_source_files(tmp_path, source):
    clips = (
        Clip(source_id=source.id, in_point=2, out_point=7, crop=Crop(width=1920, height=1080)),
        Clip(source_id=source.id, audio=AudioSettings(enabled=False)),
    )
    project = Project(
        name="Long video 🎬",
        sources=(source,),
        clips=clips,
        overlay=Overlay(source_id=source.id, end_behavior="loop", audio=AudioSettings(gain_db=-12)),
        output=OutputSettings(width=7680, height=4320, frame_rate="60000/1001"),
    )
    path = tmp_path / "my project.stitch.json"
    save_project(project, path)
    assert load_project(path) == project
    assert not source.path.exists()  # Opening must keep offline sources and edits intact.
    assert load_project(path).output.frame_rate == "60000/1001"
    assert not list(tmp_path.glob(".stitch-*"))


@pytest.mark.parametrize("rate", ["0/0", "-24", "NaN", "inf", "0"])
def test_rejects_invalid_frame_rates(rate):
    with pytest.raises(ValidationError):
        OutputSettings(frame_rate=rate)


@pytest.mark.parametrize(
    "changes",
    [
        {"in_point": -1},
        {"in_point": float("nan")},
        {"in_point": 4, "out_point": 4},
        {"in_point": 6, "out_point": 3},
    ],
)
def test_rejects_invalid_trims(source, changes):
    with pytest.raises(ValidationError):
        Clip(source_id=source.id, **changes)


def test_rejects_unknown_references_and_duplicate_ids(source):
    clip = Clip(source_id=source.id)
    for args in (
        {"clips": (clip,)},
        {"sources": (source, source)},
        {"sources": (source,), "clips": (clip, clip)},
        {"overlay": Overlay(source_id=source.id)},
    ):
        with pytest.raises(ValidationError):
            Project(**args)


@pytest.mark.parametrize(
    "changes",
    [
        {"in_point": 10},
        {"out_point": 11},
        {"crop": Crop(x=3800, width=100, height=100)},
    ],
)
def test_checks_edits_against_cached_media(source, changes):
    with pytest.raises(ValidationError):
        Project(sources=(source,), clips=(Clip(source_id=source.id, **changes),))


def test_rotation_changes_crop_coordinate_bounds(source):
    raw = source.model_dump(mode="json")
    raw["cache"]["metadata"]["video"]["rotation"] = 90
    rotated = Source.model_validate(raw)
    Project(
        sources=(rotated,),
        clips=(
            Clip(
                source_id=rotated.id,
                crop=Crop(width=2160, height=3840),
            ),
        ),
    )
    with pytest.raises(ValidationError):
        Project(
            sources=(rotated,),
            clips=(
                Clip(
                    source_id=rotated.id,
                    crop=Crop(width=3840, height=2160),
                ),
            ),
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {},
        {"schema_version": 1, "unknown": "must not be silently discarded"},
        {"schema_version": 1, "output": {"width": 1921}},
    ],
)
def test_rejects_unsupported_or_invalid_files(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ProjectError):
        load_project(path)


def test_failed_replace_preserves_previous_project(tmp_path, monkeypatch):
    path = tmp_path / "project.json"
    original = Project(name="Original")
    save_project(original, path)

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("stitch.project_io.os.replace", fail)
    with pytest.raises(ProjectError, match="disk failure"):
        save_project(Project(name="Updated"), path)
    assert load_project(path) == original
    assert not list(tmp_path.glob(".stitch-*"))


def test_invalid_in_memory_edit_cannot_overwrite_project(tmp_path):
    path = tmp_path / "project.json"
    save_project(Project(), path)
    invalid = Project().model_copy(update={"schema_version": 99})
    with pytest.raises(ProjectError):
        save_project(invalid, path)
    assert load_project(path).schema_version == 1


def test_schema_describes_versioned_project():
    schema = Project.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == 1
    assert "Clip" in schema["$defs"]
