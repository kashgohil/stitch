from fractions import Fraction

import pytest
from pydantic import ValidationError

from stitch.joins import plan_crossfades
from stitch.media import file_identity
from stitch.models import (
    Clip,
    MediaMetadata,
    OutputSettings,
    ProbeCache,
    Project,
    Source,
    VideoStream,
)
from stitch.project_io import load_project, save_project
from stitch.render_plan import build_segment_plan


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "source.mov"
    path.write_bytes(b"original")
    source = Source(
        path=path,
        cache=ProbeCache(
            identity=file_identity(path),
            metadata=MediaMetadata(
                format_name="mov",
                duration=1,
                video=VideoStream(index=0, codec="h264", width=160, height=90, duration=1),
            ),
        ),
    )
    return Project(
        sources=(source,),
        clips=tuple(Clip(source_id=source.id) for _ in range(3)),
        output=OutputSettings(
            width=160,
            height=90,
            frame_rate="30/1",
            profile="h264",
            join_mode="crossfade",
            crossfade_duration=0.25,
        ),
    )


def test_join_settings_backward_compatible_and_round_trip(project, tmp_path):
    assert Project().output.join_mode == "hard_cut"
    saved = tmp_path / "project.json"
    save_project(project, saved)
    assert load_project(saved) == project
    assert project.output.overlap_frames == 8
    assert project.output.overlap_duration == Fraction(4, 15)
    old = project.model_dump()
    old["output"].pop("join_mode")
    old["output"].pop("crossfade_duration")
    assert Project.model_validate(old).output.overlap_frames == 0


def test_middle_clip_cannot_have_overlapping_transitions(project):
    data = project.model_dump()
    data["output"]["crossfade_duration"] = 0.51
    with pytest.raises(ValidationError, match="Clip 2.*2 crossfade"):
        Project.model_validate(data)
    data["clips"] = data["clips"][:1]
    data["output"]["crossfade_duration"] = 100
    assert Project.model_validate(data)  # A single clip has no boundary.


def test_original_only_chunk_order_and_zero_bodies(project, tmp_path):
    project = project.model_copy(
        update={"output": project.output.model_copy(update={"crossfade_duration": 0.5})}
    )
    plans = [
        build_segment_plan(project.sources[0], c, project.output, tmp_path / f"{i}.mov")
        for i, c in enumerate(project.clips)
    ]
    chunks = plan_crossfades(project, plans, tmp_path)
    assert [c.kind for c in chunks] == ["body", "crossfade", "crossfade", "body"]
    assert sum(c.plan.duration for c in chunks) == 2
    for chunk in chunks:
        command = chunk.plan.command
        inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
        assert inputs == [str(project.sources[0].path)] * len(chunk.sources)
        assert len(inputs) <= 2 and command[command.index("-c:v") + 1] == "libx264"
    for mode, duration in (("hard_cut", 0.5), ("crossfade", 0)):
        changed = project.model_copy(
            update={
                "output": project.output.model_copy(
                    update={"join_mode": mode, "crossfade_duration": duration}
                )
            }
        )
        assert plan_crossfades(changed, plans, tmp_path) == []


def test_hevc_chunks_share_no_reorder_configuration(project, tmp_path):
    project = project.model_copy(
        update={"output": project.output.model_copy(update={"profile": "hevc_main10"})}
    )
    plans = [
        build_segment_plan(project.sources[0], c, project.output, tmp_path / f"{i}.mov")
        for i, c in enumerate(project.clips)
    ]
    for chunk in plan_crossfades(project, plans, tmp_path):
        command = chunk.plan.command
        assert command[command.index("-x265-params") + 1].endswith(":bframes=0")


def test_rounding_is_validated_not_silently_shortened(project):
    data = project.model_dump()
    data["output"].update(frame_rate="24/1", crossfade_duration=0.5)
    data["clips"][1]["out_point"] = 0.99
    with pytest.raises(ValidationError, match="Clip 2"):
        Project.model_validate(data)
