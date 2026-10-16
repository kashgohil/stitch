"""Versioned edit decisions and cached media facts. No Qt or filesystem access."""

import math
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]


def rational(value: str) -> str:
    """Keep NTSC and other fractional rates exact through JSON round trips."""
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("must be a positive rational number, such as 30000/1001") from exc
    if rate <= 0:
        raise ValueError("must be greater than zero")
    return f"{rate.numerator}/{rate.denominator}"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FileIdentity(Model):
    size: Annotated[int, Field(ge=0, strict=True)]
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


class VideoStream(Model):
    index: Annotated[int, Field(ge=0)]
    codec: str
    width: PositiveInt
    height: PositiveInt
    rotation: float = 0
    pixel_format: str | None = None
    bit_depth: PositiveInt | None = None
    average_frame_rate: str | None = None
    real_frame_rate: str | None = None
    time_base: str | None = None
    sample_aspect_ratio: str | None = None
    duration: Seconds | None = None
    start_time: float | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_matrix: str | None = None
    color_range: str | None = None

    @field_validator("average_frame_rate", "real_frame_rate", "time_base", "sample_aspect_ratio")
    @classmethod
    def valid_rate(cls, value: str | None) -> str | None:
        return rational(value) if value is not None else None

    @property
    def display_dimensions(self) -> tuple[int, int]:
        """Oriented pixel dimensions; SAR is kept separately for the renderer."""
        return (self.height, self.width) if self.rotation % 180 == 90 else (self.width, self.height)


class AudioStream(Model):
    index: Annotated[int, Field(ge=0)]
    codec: str
    sample_rate: PositiveInt | None = None
    channels: PositiveInt | None = None
    channel_layout: str | None = None
    sample_format: str | None = None
    bit_depth: PositiveInt | None = None
    duration: Seconds | None = None
    start_time: float | None = None
    time_base: str | None = None

    @field_validator("time_base")
    @classmethod
    def valid_time_base(cls, value: str | None) -> str | None:
        return rational(value) if value is not None else None


class MediaMetadata(Model):
    format_name: str
    duration: Annotated[float, Field(gt=0)]
    video: VideoStream | None = None
    audio: tuple[AudioStream, ...] = ()

    @model_validator(mode="after")
    def has_media(self) -> Self:
        if self.video is None and not self.audio:
            raise ValueError("media must contain video or audio")
        return self


class ProbeCache(Model):
    version: Literal[1] = 1
    identity: FileIdentity
    metadata: MediaMetadata


class Source(Model):
    id: UUID = Field(default_factory=uuid4)
    path: Path
    cache: ProbeCache | None = None

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("source paths must be absolute")
        return value


class Crop(Model):
    """Rectangle in source pixels after rotation, before fit/pad."""

    x: Annotated[int, Field(ge=0, strict=True)] = 0
    y: Annotated[int, Field(ge=0, strict=True)] = 0
    width: PositiveInt
    height: PositiveInt


class AudioSettings(Model):
    enabled: bool = True
    gain_db: Annotated[float, Field(ge=-60, le=12)] = 0


class Clip(Model):
    id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    in_point: Seconds = 0
    out_point: Seconds | None = None
    crop: Crop | None = None
    audio: AudioSettings = Field(default_factory=AudioSettings)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.out_point is not None and self.out_point <= self.in_point:
            raise ValueError("out_point must be after in_point")
        return self


class Overlay(Model):
    source_id: UUID
    audio: AudioSettings = Field(default_factory=AudioSettings)
    end_behavior: Literal["stop", "loop"] = "stop"


class OutputSettings(Model):
    width: PositiveInt = 3840
    height: PositiveInt = 2160
    frame_rate: str = "30000/1001"
    profile: Literal["hevc_main10", "h264", "prores_hq"] = "hevc_main10"
    allow_upscale: bool = False
    join_mode: Literal["hard_cut", "crossfade"] = "hard_cut"
    crossfade_duration: Seconds = 0.5

    @property
    def overlap_frames(self) -> int:
        if self.join_mode == "hard_cut":
            return 0
        return math.ceil(Fraction(str(self.crossfade_duration)) * Fraction(self.frame_rate))

    @property
    def overlap_duration(self) -> Fraction:
        return self.overlap_frames / Fraction(self.frame_rate)

    @field_validator("frame_rate")
    @classmethod
    def valid_rate(cls, value: str) -> str:
        return rational(value)

    @field_validator("width", "height")
    @classmethod
    def even_dimension(cls, value: int) -> int:
        if value % 2:
            raise ValueError("output dimensions must be even")
        return value


class Project(Model):
    schema_version: Literal[1] = 1
    name: str = "Untitled"
    sources: tuple[Source, ...] = ()
    clips: tuple[Clip, ...] = ()
    overlay: Overlay | None = None
    output: OutputSettings = Field(default_factory=OutputSettings)

    @model_validator(mode="after")
    def valid_references_and_edits(self) -> Self:
        sources = {source.id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("source IDs must be unique")
        if len({clip.id for clip in self.clips}) != len(self.clips):
            raise ValueError("clip IDs must be unique")
        for clip in self.clips:
            if clip.source_id not in sources:
                raise ValueError(f"clip {clip.id} references an unknown source")
            cached = sources[clip.source_id].cache
            if cached is None:
                continue
            video = cached.metadata.video
            if video is None:
                raise ValueError("a video clip cannot reference an audio-only source")
            duration = video.duration or cached.metadata.duration
            if clip.in_point >= duration or (
                clip.out_point is not None and clip.out_point > duration + 0.000001
            ):
                raise ValueError("clip trim exceeds source video duration")
            if crop := clip.crop:
                width, height = video.display_dimensions
                if crop.x + crop.width > width or crop.y + crop.height > height:
                    raise ValueError("crop exceeds the oriented source dimensions")
        if self.overlay is not None:
            if self.overlay.source_id not in sources:
                raise ValueError("overlay references an unknown source")
            cached = sources[self.overlay.source_id].cache
            if cached is not None and not cached.metadata.audio:
                raise ValueError("overlay source must contain audio")
        if len(self.clips) > 1 and self.output.overlap_frames:
            for index, clip in enumerate(self.clips):
                cached = sources[clip.source_id].cache
                if not cached:
                    continue  # Export requires current metadata before planning.
                end = clip.out_point
                if end is None:
                    end = cached.metadata.video.duration or cached.metadata.duration
                overlaps = int(index > 0) + int(index < len(self.clips) - 1)
                required = float(self.output.overlap_duration) * overlaps
                if required > end - clip.in_point + 0.000001:
                    raise ValueError(
                        f"Clip {index + 1} is too short for {overlaps} crossfade overlap(s): "
                        f"needs {required:.6f}s after output-frame rounding. "
                        "Reduce the crossfade duration or extend the trim."
                    )
        return self
