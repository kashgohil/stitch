"""One-level, worker-thread folder discovery with durable no-duplicate history."""

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from stitch.document import change_project, match_first_clip
from stitch.media import MediaError, MissingSourceError, file_identity
from stitch.models import FolderEntry, Project, Source
from stitch.sources import append_video_source, refresh_source

# Candidates only: ffprobe, not the suffix, decides whether a file contains video.
VIDEO_SUFFIXES = frozenset(
    ".mp4 .mov .mkv .webm .avi .m4v .mts .m2ts .mpeg .mpg .ts .wmv .flv "
    ".3gp .3g2 .mxf .vob .ogv .ogm".split()
)


def natural_key(path: Path) -> tuple:
    # Length/text ties make case and leading-zero ties independent of scandir order.
    parts = tuple(
        (1, int(p)) if p.isascii() and p.isdigit() else (0, p)
        for p in re.split(r"([0-9]+)", path.name.casefold())
    )
    return parts, path.name.casefold(), path.name


@dataclass
class FolderResult:
    project: Project
    imported: int = 0
    skipped: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def scan_folder(
    project: Project,
    folder: Path,
    *,
    excluded_roots: tuple[Path, ...] = (),
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int, str], None] = lambda *_: None,
    discovery: Callable[[str], None] = lambda _: None,
    match_output: bool = False,
) -> FolderResult:
    """Return only complete import decisions. Never modify or delete source files.

    Successful imports survive cancellation. A running ffprobe finishes first;
    scanning and subsequent probes stop at the next cancellation check.
    """
    result = FolderResult(project)
    if cancelled():
        result.cancelled = True
        return result
    candidates = []
    try:
        folder = folder.expanduser().resolve()
        if any(folder.is_relative_to(root.resolve()) for root in excluded_roots):
            raise MediaError("Stitch cache/job folders are excluded from folder import.")
        discovery(f"Scanning {folder} (immediate files only)…")
        last_notice = time.monotonic()
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries, 1):
                if cancelled():
                    result.cancelled = True
                    return result
                if time.monotonic() - last_notice >= 0.1:
                    discovery(f"Scanning folder: {index} entries checked…")
                    last_notice = time.monotonic()
                path = Path(entry.path)
                try:
                    # Do not follow links, recurse, or open FIFOs/devices. Hidden files
                    # also cover Stitch staging names and macOS resource-fork files.
                    if (
                        entry.name.startswith(".")
                        or entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                        or path.suffix.lower() not in VIDEO_SUFFIXES
                    ):
                        result.skipped += 1
                        continue
                    candidates.append(path)
                except OSError as exc:
                    result.errors.append(f"{entry.name}: cannot inspect file: {exc}")
    except (OSError, ValueError, RuntimeError) as exc:
        result.errors.append(f"Cannot scan {folder}: {exc}")
        return result  # An inaccessible folder does not replace the selected folder.
    if cancelled():
        result.cancelled = True
        return result
    candidates.sort(key=natural_key)
    result.project = change_project(project, folder=folder)
    known = {entry.path: entry.identity for entry in project.folder_history}
    existing = {source.path: source for source in project.sources}
    # Current source caches take precedence after an explicit individual re-import.
    known.update({s.path: s.cache.identity for s in project.sources if s.cache})
    physical = {(i.device, i.inode): path for path, i in known.items()}
    present = set(candidates)
    for path in sorted(known, key=str):
        if cancelled():
            result.cancelled = True
            return result
        if path.parent == folder and path not in present:
            # Existing overlay/explicitly imported files may have non-candidate
            # suffixes. Being absent from discovery does not mean they are missing.
            try:
                if file_identity(path) != known[path]:
                    result.errors.append(f"{path.name}: changed; queue/history kept.")
            except MissingSourceError:
                result.errors.append(f"{path.name}: missing; queue/history kept.")
            except MediaError as exc:
                result.errors.append(f"{path.name}: {exc}; queue/history kept.")
    last_notice = 0.0
    for index, path in enumerate(candidates, 1):
        if cancelled():
            result.cancelled = True
            break
        if time.monotonic() - last_notice >= 0.1 or index == len(candidates):
            progress(index, len(candidates), path.name)
            last_notice = time.monotonic()
        try:
            identity = file_identity(path)
            if path in known or path in existing:
                result.skipped += 1
                if known.get(path) != identity:
                    result.errors.append(
                        f"{path.name}: changed or uninspected; use Add videos to explicitly "
                        "re-import. Existing edits kept."
                    )
                elif not any(e.path == path for e in result.project.folder_history):
                    result.project = change_project(
                        result.project,
                        folder_history=(
                            *result.project.folder_history,
                            FolderEntry(path=path, identity=identity),
                        ),
                    )
                continue
            if identity.inode and (identity.device, identity.inode) in physical:
                result.skipped += 1
                result.project = change_project(
                    result.project,
                    folder_history=(
                        *result.project.folder_history,
                        FolderEntry(path=path, identity=identity),
                    ),
                )
                continue  # A renamed file/hard link must not restore an imported clip.
            source = refresh_source(Source(path=path))
            if source.cache.metadata.stitch_generated or source.cache.metadata.video is None:
                result.skipped += 1
                continue
            updated = append_video_source(result.project, source)
            if match_output and result.imported == 0:
                updated = match_first_clip(updated)
            result.project = updated
            result.imported += 1
            known[path] = source.cache.identity
            physical[(source.cache.identity.device, source.cache.identity.inode)] = path
        except (OSError, ValueError, RuntimeError) as exc:
            result.errors.append(f"{path.name}: {exc}")
    result.cancelled |= cancelled()
    return result
