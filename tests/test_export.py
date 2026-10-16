from fractions import Fraction
from types import SimpleNamespace

import pytest

from stitch.export import (
    DiskEstimate,
    check_destination,
    concat_command,
    concat_script,
    destination_identity,
    estimate_disk,
    export_project,
    publish_output,
    require_space,
)
from stitch.models import Clip, OutputSettings, Overlay, Project, Source
from stitch.paths import AppPaths
from stitch.render_plan import RenderError


def test_no_clobber_publication_handles_new_or_changed_targets(tmp_path):
    staged, target = tmp_path / "staged.mp4", tmp_path / "target.mp4"
    staged.write_bytes(b"new")
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        publish_output(staged, target, None)
    assert target.read_bytes() == b"old"
    identity = destination_identity(target)
    target.write_bytes(b"changed after preflight")
    with pytest.raises(RenderError, match="changed"):
        publish_output(staged, target, identity)
    assert staged.read_bytes() == b"new"
    publish_output(staged, target, destination_identity(target))
    assert target.read_bytes() == b"new" and not staged.exists()


def test_publish_new_output_without_overwriting(tmp_path):
    staged, target = tmp_path / "staged.mp4", tmp_path / "target.mp4"
    staged.write_bytes(b"complete")
    publish_output(staged, target, None)
    assert target.read_bytes() == b"complete"


def test_destination_checks_source_aliases_suffix_and_permission(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    project = Project(sources=(Source(path=path),), output=OutputSettings(profile="h264"))
    with pytest.raises(RenderError, match="source media"):
        check_destination(project, path, overwrite=True)
    alias = tmp_path / "alias.mp4"
    alias.hardlink_to(path)
    with pytest.raises(RenderError, match="source media"):
        check_destination(project, alias, overwrite=True)
    with pytest.raises(RenderError, match="requires"):
        check_destination(project, tmp_path / "wrong.mov", overwrite=False)
    other = tmp_path / "other.mp4"
    other.write_bytes(b"keep")
    with pytest.raises(RenderError, match="confirm replacement"):
        check_destination(project, other, overwrite=False)
    assert check_destination(project, other, overwrite=True) == destination_identity(other)


def test_estimate_scales_with_duration_resolution_and_profile():
    base = Project(
        output=OutputSettings(width=1920, height=1080, frame_rate="30/1", profile="h264")
    )
    estimate = estimate_disk(base, 60)
    assert estimate_disk(base, 120).segments == estimate.segments * 2
    high = base.model_copy(
        update={"output": base.output.model_copy(update={"width": 3840, "height": 2160})}
    )
    assert estimate_disk(high, 60).segments > estimate.segments * 3
    master = base.model_copy(
        update={"output": base.output.model_copy(update={"profile": "prores_hq"})}
    )
    assert estimate_disk(master, 60).segments > estimate.segments


def test_shared_volume_requires_both_segments_and_output_space(tmp_path, monkeypatch):
    monkeypatch.setattr("stitch.export.shutil.disk_usage", lambda path: SimpleNamespace(free=350))
    with pytest.raises(RenderError, match="space"):
        require_space(tmp_path, tmp_path, DiskEstimate(100, 200, 100))
    require_space(tmp_path, tmp_path, DiskEstimate(100, 100, 100))


def test_concat_durations_do_not_accumulate_fractional_rate_rounding(tmp_path):
    durations = [Fraction(1001, 30000)] * 10000
    segments = [tmp_path / f"{i}.mov" for i in range(len(durations))]
    script = concat_script(tmp_path, segments, durations)
    encoded = [
        Fraction(line.split()[1]) for line in script.splitlines() if line.startswith("duration")
    ]
    actual = Fraction(0)
    expected = Fraction(0)
    for duration, source_duration in zip(encoded, durations, strict=True):
        actual += duration
        expected += source_duration
        assert abs(actual - expected) <= Fraction(1, 2000000)


def test_assembly_command_copies_video_and_encodes_audio_once(tmp_path):
    project = Project(output=OutputSettings(profile="hevc_main10"))
    command = concat_command(
        "ffmpeg", tmp_path / "sequence.ffconcat", tmp_path / "final.mp4", project, Fraction(10)
    )
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert "320k" in command and "hvc1" in command and "-n" in command


def test_overlay_is_rejected_instead_of_silently_omitted(tmp_path):
    source = Source(path=tmp_path / "source.mp4")
    project = Project(
        sources=(source,), clips=(Clip(source_id=source.id),), overlay=Overlay(source_id=source.id)
    )
    paths = AppPaths(*(tmp_path / name for name in ("p", "c", "jobs", "logs")))
    with pytest.raises(RenderError, match="Overlay audio"):
        export_project(project, tmp_path / "final.mp4", paths)
    assert not paths.jobs.exists()
