"""Import services and cache freshness checks for the upcoming desktop controls."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from stitch.media import MediaError, MissingSourceError, file_identity, probe_media
from stitch.models import Clip, FolderEntry, Overlay, ProbeCache, Project, Source
from stitch.project_io import ProjectError


@dataclass(frozen=True)
class SourceStatus:
    source_id: str
    path: str
    state: Literal["ready", "uncached", "changed", "missing", "unavailable"]
    detail: str | None = None


def source_status(source: Source) -> SourceStatus:
    """Inspect file identity only; never trust stale metadata or change edits."""
    try:
        identity = file_identity(source.path)
    except MissingSourceError as exc:
        return SourceStatus(str(source.id), str(source.path), "missing", str(exc))
    except MediaError as exc:
        return SourceStatus(str(source.id), str(source.path), "unavailable", str(exc))
    state = (
        "uncached"
        if source.cache is None
        else ("ready" if source.cache.identity == identity else "changed")
    )
    return SourceStatus(str(source.id), str(source.path), state)


def refresh_source(source: Source) -> Source:
    """Re-probe changed sources, keeping identity and metadata from the same file."""
    before = file_identity(source.path)
    if source.cache is not None and source.cache.identity == before:
        return source
    metadata = probe_media(source.path)
    after = file_identity(source.path)
    if before != after:
        raise MediaError(
            f"Source changed during inspection; retry when it stops changing: {source.path}"
        )
    return Source(
        id=source.id, path=source.path, cache=ProbeCache(identity=after, metadata=metadata)
    )


def _import_source(project: Project, path: Path) -> Source:
    try:
        path = path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise MediaError(f"Cannot resolve source path: {exc}") from exc
    existing = next((source for source in project.sources if source.path == path), None)
    return refresh_source(existing or Source(path=path))


def _updated_sources(project: Project, source: Source) -> tuple[Source, ...]:
    if any(item.id == source.id for item in project.sources):
        return tuple(source if item.id == source.id else item for item in project.sources)
    return (*project.sources, source)


def add_video(project: Project, path: Path) -> Project:
    """Return a new validated project. A failed import leaves the old one intact."""
    source = _import_source(project, path)
    return append_video_source(project, source)


def append_video_source(project: Project, source: Source) -> Project:
    """Commit a successfully inspected video and its import history together."""
    if source.cache.metadata.video is None:
        raise MediaError(
            f"{source.path.name} contains audio but no video. Use it as overlay audio."
        )
    history = project.folder_history
    if source.path.parent == project.folder or any(e.path == source.path for e in history):
        entry = FolderEntry(path=source.path, identity=source.cache.identity)
        history = tuple(e for e in history if e.path != source.path) + (entry,)
    try:
        return Project(
            name=project.name,
            sources=_updated_sources(project, source),
            clips=(*project.clips, Clip(source_id=source.id)),
            overlay=project.overlay,
            output=project.output,
            folder=project.folder,
            folder_history=history,
        )
    except ValidationError as exc:
        raise ProjectError(f"Updated source conflicts with existing edits: {exc}") from exc


def set_overlay(project: Project, path: Path) -> Project:
    source = _import_source(project, path)
    if not source.cache.metadata.audio:
        raise MediaError(f"{path.name} contains no audio stream.")
    sources = _updated_sources(project, source)
    previous = project.overlay
    if previous and previous.source_id != source.id:
        used_clips = {clip.source_id for clip in project.clips}
        sources = tuple(s for s in sources if s.id != previous.source_id or s.id in used_clips)
    try:
        return Project(
            name=project.name,
            sources=sources,
            clips=project.clips,
            overlay=Overlay(
                source_id=source.id, audio=previous.audio, end_behavior=previous.end_behavior
            )
            if previous
            else Overlay(source_id=source.id),
            output=project.output,
            folder=project.folder,
            folder_history=project.folder_history,
        )
    except ValidationError as exc:
        raise ProjectError(f"Updated source conflicts with existing edits: {exc}") from exc
