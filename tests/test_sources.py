import pytest

from stitch.media import MediaError
from stitch.models import (
    AudioSettings,
    AudioStream,
    Clip,
    MediaMetadata,
    Project,
    Source,
    VideoStream,
)
from stitch.project_io import ProjectError, load_project, save_project
from stitch.sources import add_video, refresh_source, set_overlay, source_status


@pytest.fixture
def media(tmp_path, monkeypatch):
    path = tmp_path / "video one.mp4"
    path.write_bytes(b"original")
    info = MediaMetadata(
        format_name="mp4",
        duration=10,
        video=VideoStream(index=0, codec="h264", width=1920, height=1080, duration=10),
        audio=(AudioStream(index=1, codec="aac"),),
    )
    calls = []

    def probe(path):
        calls.append(path)
        return info

    monkeypatch.setattr("stitch.sources.probe_media", probe)
    return path, info, calls


def test_repeated_imports_share_source_cache_but_not_clip_ids(media, tmp_path):
    path, _, calls = media
    project = add_video(Project(), path)
    save_project(project, tmp_path / "p.json")
    project = add_video(load_project(tmp_path / "p.json"), path)
    assert len(calls) == 1
    assert len(project.sources) == 1
    assert len({clip.id for clip in project.clips}) == 2
    assert source_status(project.sources[0]).state == "ready"


def test_source_changes_force_reprobe_and_missing_source_keeps_edits(media):
    path, _, calls = media
    assert source_status(Source(path=path)).state == "uncached"
    project = add_video(Project(), path)
    old = project.sources[0]
    path.write_bytes(b"changed source content")
    assert source_status(old).state == "changed"
    new = refresh_source(old)
    assert len(calls) == 2 and new.id == old.id
    assert new.cache.identity != old.cache.identity
    path.unlink()
    assert source_status(new).state == "missing"
    assert project.clips[0].source_id == old.id
    with pytest.raises(MediaError, match="missing"):
        refresh_source(old)


def test_replacement_file_is_detected_even_with_same_size_and_mtime(media, tmp_path):
    import os

    path, _, _ = media
    source = refresh_source(Source(path=path))
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes())
    mtime = path.stat().st_mtime_ns
    os.utime(replacement, ns=(mtime, mtime))
    replacement.replace(path)
    assert source_status(source).state == "changed"


def test_source_changed_during_probe_is_not_cached(media, monkeypatch):
    path, info, _ = media

    def changing_probe(path):
        path.write_bytes(b"changed while probing")
        return info

    monkeypatch.setattr("stitch.sources.probe_media", changing_probe)
    source = Source(path=path)
    with pytest.raises(MediaError, match="changed during inspection"):
        refresh_source(source)
    assert source.cache is None


def test_changed_shorter_media_cannot_silently_invalidate_existing_trims(media, monkeypatch):
    path, info, _ = media
    project = add_video(Project(), path)
    project = Project(
        sources=project.sources,
        clips=(Clip(source_id=project.sources[0].id, in_point=5, out_point=9),),
    )
    path.write_bytes(b"shorter file")
    shorter = MediaMetadata(
        format_name="mp4",
        duration=2,
        video=VideoStream(
            index=0,
            codec="h264",
            width=1920,
            height=1080,
            duration=2,
        ),
    )
    monkeypatch.setattr("stitch.sources.probe_media", lambda _: shorter)
    with pytest.raises(ProjectError, match="conflicts with existing edits"):
        add_video(project, path)
    assert project.clips[0].out_point == 9


def test_overlay_accepts_audio_only_but_clip_import_rejects_it(media, monkeypatch):
    path, _, _ = media
    audio = MediaMetadata(
        format_name="wav",
        duration=1,
        audio=(AudioStream(index=0, codec="pcm_s24le", sample_rate=48000, bit_depth=24),),
    )
    monkeypatch.setattr("stitch.sources.probe_media", lambda _: audio)
    project = set_overlay(Project(), path)
    assert project.overlay.source_id == project.sources[0].id
    with pytest.raises(MediaError, match="no video"):
        add_video(project, path)


def test_directory_is_unavailable_without_discarding_source(tmp_path):
    assert source_status(Source(path=tmp_path)).state == "unavailable"


def test_overlay_replacement_keeps_settings_and_prunes_only_unused_previous_source(media, tmp_path):
    from stitch.document import change_project, remove_overlay

    path, _, _ = media
    project = set_overlay(add_video(Project(), path), path)
    shared_id = project.overlay.source_id
    project = change_project(
        project,
        overlay=project.overlay.model_copy(
            update={
                "audio": AudioSettings(gain_db=-8.5),
                "end_behavior": "loop",
            }
        ),
    )
    replacement = tmp_path / "music.wav"
    replacement.write_bytes(b"replacement")
    project = set_overlay(project, replacement)
    assert project.overlay.audio.gain_db == -8.5 and project.overlay.end_behavior == "loop"
    assert shared_id in {source.id for source in project.sources}
    other = tmp_path / "other.wav"
    other.write_bytes(b"other")
    project = set_overlay(project, other)
    assert {source.path for source in project.sources} == {path, other}
    project = remove_overlay(project)
    assert project.overlay is None and project.sources[0].id == shared_id
    assert len(project.sources) == 1
    assert path.exists() and replacement.exists() and other.exists()
