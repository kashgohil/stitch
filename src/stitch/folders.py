"""One-level, worker-thread folder discovery with durable no-duplicate history."""

import os
import re
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from stitch.document import change_project, match_first_clip
from stitch.media import MediaError, MissingSourceError, file_identity
from stitch.models import FileIdentity, FolderEntry, Project, Source
from stitch.sources import append_video_source, refresh_source

# Candidates only: ffprobe, not the suffix, decides whether a file contains video.
VIDEO_SUFFIXES = frozenset(
    ".mp4 .mov .mkv .webm .avi .m4v .mts .m2ts .mpeg .mpg .ts .wmv .flv "
    ".3gp .3g2 .mxf .vob .ogv .ogm".split()
)
PROBE_WORKERS = 4


@dataclass(frozen=True)
class Inspection:
    identity: FileIdentity
    source: Source | None = None


def inspect_candidate(
    path: Path,
    known_paths: frozenset[Path],
    known_files: frozenset[tuple[int, int]],
    cancelled: Callable[[], bool],
) -> Inspection | None:
    """Inspect independently. Only the coordinating thread changes the project."""
    if cancelled():
        return None
    identity = file_identity(path)
    if path in known_paths or (identity.inode and (identity.device, identity.inode) in known_files):
        return Inspection(identity)
    source = refresh_source(Source(path=path))
    return Inspection(source.cache.identity, source)


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
    probe_workers: int = PROBE_WORKERS,
) -> FolderResult:
    """Return only complete import decisions. Never modify or delete source files.

    At most four inspections run concurrently. Results enter the project in natural
    order. Cancellation stops scheduling; already running probes finish before return.
    """
    if type(probe_workers) is not int or not 1 <= probe_workers <= PROBE_WORKERS:
        raise ValueError(f"probe_workers must be an integer between 1 and {PROBE_WORKERS}.")
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

    def accept(path: Path, inspection: Inspection) -> None:
        identity = inspection.identity
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
            return
        if identity.inode and (identity.device, identity.inode) in physical:
            result.skipped += 1
            result.project = change_project(
                result.project,
                folder_history=(
                    *result.project.folder_history,
                    FolderEntry(path=path, identity=identity),
                ),
            )
            return  # A renamed file/hard link must not restore an imported clip.
        source = inspection.source
        if source is None:
            raise MediaError("Source identity changed during discovery; refresh the folder.")
        # A prefetched result can wait behind a slower file. Do not accept it
        # if the file changed during that wait.
        if file_identity(path) != identity:
            raise MediaError("Source changed after inspection; refresh the folder.")
        if source.cache.metadata.stitch_generated or source.cache.metadata.video is None:
            result.skipped += 1
            return
        updated = append_video_source(result.project, source)
        if match_output and result.imported == 0:
            updated = match_first_clip(updated)
        result.project = updated
        result.imported += 1
        known[path] = source.cache.identity
        physical[(source.cache.identity.device, source.cache.identity.inode)] = path

    # Limit pending futures as well as active threads. An early slow file must
    # not accumulate results for the entire folder in memory.
    pending = deque()
    remaining = iter(enumerate(candidates, 1))
    known_paths = frozenset(known.keys() | existing.keys())
    known_files = frozenset(physical)
    last_notice = 0.0
    with ThreadPoolExecutor(max_workers=probe_workers, thread_name_prefix="stitch-inspect") as pool:

        def schedule() -> None:
            while len(pending) < probe_workers and not cancelled():
                item = next(remaining, None)
                if item is None:
                    break
                index, path = item
                pending.append(
                    (
                        index,
                        path,
                        pool.submit(
                            inspect_candidate,
                            path,
                            known_paths,
                            known_files,
                            cancelled,
                        ),
                    )
                )

        schedule()
        while pending:
            index, path, future = pending.popleft()
            if time.monotonic() - last_notice >= 0.1 or index == len(candidates):
                progress(index, len(candidates), path.name)
                last_notice = time.monotonic()
            try:
                inspection = future.result()
                if inspection is not None:
                    accept(path, inspection)
            except (OSError, ValueError, RuntimeError) as exc:
                result.errors.append(f"{path.name}: {exc}")
            if cancelled():
                for _, _, future in pending:
                    future.cancel()
                break
            schedule()
    result.cancelled |= cancelled()
    return result
