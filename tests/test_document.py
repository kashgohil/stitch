import pytest
from pydantic import ValidationError

from stitch.document import (
    Document,
    change_project,
    match_first_clip,
    move_clip,
    remove_clip,
    replace_clip,
)
from stitch.models import (
    Clip,
    Crop,
    FileIdentity,
    MediaMetadata,
    Overlay,
    ProbeCache,
    Project,
    Source,
    VideoStream,
)
from stitch.project_io import ProjectError


@pytest.fixture
def sequence(tmp_path):
    source = Source(
        path=tmp_path / "video.mp4",
        cache=ProbeCache(
            identity=FileIdentity(size=1, mtime_ns=1, ctime_ns=1, inode=1, device=1),
            metadata=MediaMetadata(
                format_name="mp4",
                duration=20,
                video=VideoStream(
                    index=0,
                    codec="hevc",
                    width=7680,
                    height=4320,
                    average_frame_rate="120000/1001",
                ),
            ),
        ),
    )
    return Project(
        sources=(source,),
        clips=tuple(Clip(source_id=source.id, in_point=i, out_point=i + 1) for i in range(10)),
    )


def test_reordering_retains_clip_identity_and_edits(sequence):
    original = sequence.clips[3]
    edited = Clip(**{**original.model_dump(), "crop": Crop(width=1920, height=1080)})
    project = replace_clip(sequence, edited)
    project = move_clip(project, original.id, -3)
    assert project.clips[0] == edited
    assert len(project.clips) == 10
    assert move_clip(project, original.id, -1) == project
    assert sequence.clips[3].crop is None


def test_invalid_edit_preserves_current_project(sequence):
    with pytest.raises(ValidationError, match="duration"):
        replace_clip(
            sequence, Clip(id=sequence.clips[0].id, source_id=sequence.sources[0].id, out_point=99)
        )
    assert sequence.clips[0].out_point == 1


def test_remove_retains_shared_sources_and_overlay_references(sequence, tmp_path):
    project = remove_clip(sequence, sequence.clips[0].id)
    assert len(project.sources) == 1
    for clip in project.clips:
        project = remove_clip(project, clip.id)
    assert not project.sources
    audio = Source(path=tmp_path / "audio.wav")
    project = Project(sources=(audio,), overlay=Overlay(source_id=audio.id))
    assert remove_clip(project, sequence.clips[0].id).sources == (audio,)


def test_first_clip_output_preserves_resolution_and_exact_rate(sequence):
    output = match_first_clip(sequence).output
    assert (output.width, output.height) == (7680, 4320)
    assert output.frame_rate == "120000/1001"


def test_save_point_changes_only_after_success(tmp_path, sequence):
    doc = Document()
    doc.project = sequence
    assert doc.dirty
    doc.save(tmp_path / "sequence.json")
    assert not doc.dirty
    doc.project = change_project(doc.project, name="Changed")
    with pytest.raises(ProjectError):
        doc.save(tmp_path / "missing" / "p.json")
    assert doc.dirty and doc.path == tmp_path / "sequence.json"
    with pytest.raises(ProjectError):
        doc.open(tmp_path / "missing.json")
    assert doc.project.name == "Changed"
    doc.open(tmp_path / "sequence.json")
    assert doc.project == sequence and not doc.dirty


def test_save_cannot_overwrite_source(sequence):
    path = sequence.sources[0].path
    path.write_bytes(b"precious source")
    doc = Document()
    doc.project = sequence
    with pytest.raises(ProjectError, match="source media"):
        doc.save(path)
    assert path.read_bytes() == b"precious source"
