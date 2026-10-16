"""Editing operations and save-point tracking, independent of the desktop UI."""

from pathlib import Path
from uuid import UUID

from stitch.models import Clip, OutputSettings, Project
from stitch.project_io import ProjectError, load_project, save_project


def change_project(project: Project, **changes) -> Project:
    return Project.model_validate({**project.model_dump(), **changes})


def replace_clip(project: Project, replacement: Clip) -> Project:
    if not any(clip.id == replacement.id for clip in project.clips):
        raise ProjectError("The selected clip is no longer in this project.")
    return change_project(
        project,
        clips=tuple(replacement if clip.id == replacement.id else clip for clip in project.clips),
    )


def move_clip(project: Project, clip_id: UUID, offset: int) -> Project:
    clips = list(project.clips)
    index = next((i for i, clip in enumerate(clips) if clip.id == clip_id), None)
    if index is None:
        raise ProjectError("The selected clip is no longer in this project.")
    target = index + offset
    if not 0 <= target < len(clips):
        return project
    clips.insert(target, clips.pop(index))
    return change_project(project, clips=tuple(clips))


def remove_clip(project: Project, clip_id: UUID) -> Project:
    clips = tuple(clip for clip in project.clips if clip.id != clip_id)
    used = {clip.source_id for clip in clips}
    if project.overlay:
        used.add(project.overlay.source_id)
    return change_project(
        project,
        clips=clips,
        sources=tuple(source for source in project.sources if source.id in used),
    )


def match_first_clip(project: Project) -> Project:
    """Set a new project's output from its first import, preserving fractional FPS."""
    if not project.clips:
        return project
    source = next(s for s in project.sources if s.id == project.clips[0].source_id)
    if not source.cache or not source.cache.metadata.video:
        return project
    video = source.cache.metadata.video
    width, height = video.display_dimensions
    output = OutputSettings(
        width=width + width % 2,
        height=height + height % 2,
        frame_rate=video.average_frame_rate or video.real_frame_rate or project.output.frame_rate,
        profile=project.output.profile,
        allow_upscale=project.output.allow_upscale,
    )
    return change_project(project, output=output)


class Document:
    def __init__(self) -> None:
        self.project = Project()
        self.saved = self.project
        self.path: Path | None = None
        self.output_configured = False

    @property
    def dirty(self) -> bool:
        return self.project != self.saved

    def open(self, path: Path) -> None:
        loaded = load_project(path)  # Fail before replacing any current state.
        self.project = self.saved = loaded
        self.path = path
        self.output_configured = True

    def save(self, path: Path) -> None:
        save_project(self.project, path)
        self.path = path
        self.saved = self.project
