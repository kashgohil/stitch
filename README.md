# Stitch

A desktop video assembler for arranging, trimming, and cropping clips and adding
audio to long-form video. The current build includes the desktop shell, media-tool
checks, a versioned project model, atomic save/open, and cached media probing.
Import services are available in Python; their desktop controls are the next step.
Preview and export are upcoming issues.

## Setup and launch

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and FFmpeg.
On macOS with Homebrew:

```sh
brew install uv ffmpeg
git clone https://git.rezee.app/kash/stitch.git
cd stitch
uv sync --locked
uv run stitch
```

uv selects Python 3.12 using `.python-version` and installs the versions in
`uv.lock` into `.venv`. You can also run `uv run python -m stitch`.

On Windows, install FFmpeg including both `ffmpeg.exe` and `ffprobe.exe`, add the
binary directory to PATH, and restart your terminal. On Ubuntu/Debian use
`sudo apt install ffmpeg`. Then use the same uv commands. macOS is the first
tested platform; Windows/Linux packaging is deferred.

The app checks both tools on a background thread. If either is missing, fails,
or exceeds its five-second timeout, Setup shows the problem and installation
instructions. Use **Check again** after fixing it. Installed tools do not imply
that any particular codec, resolution, or hardware encoder is supported.

For diagnostics without launching the GUI:

```sh
uv run stitch --check
```

This prints the resolved tools and storage paths. Exit code 0 means setup passed;
1 means a tool or storage directory needs attention.

## Media and project diagnostics

```sh
uv run stitch --probe "/path/to/video.mp4"
uv run stitch --inspect-project "/path/to/project.stitch.json"
uv run stitch --project-schema
```

These commands do not launch Qt. Probe prints normalized metadata; inspection
validates a saved project and reports each source as ready, uncached, changed,
missing, or unavailable. Inspection exits with code 1 if any source needs attention.
Schema prints JSON Schema generated from the same models used to validate files.

The Python API supports importing video and overlay audio, for example:

```python
from pathlib import Path
from stitch.models import Project
from stitch.project_io import load_project, save_project
from stitch.sources import add_video, set_overlay

project = add_video(Project(name="Weekend sequence"), Path("/media/clip.mp4"))
project = set_overlay(project, Path("/media/music.wav"))
save_project(project, Path("weekend.stitch.json"))
reopened = load_project(Path("weekend.stitch.json"))
```

Project schema version 1 stores absolute source paths, UUID references, ordered
clips, trim/crop decisions, audio settings, export settings, and cached metadata.
Unknown versions are refused. Source files are not embedded or required to open
a project. Relinking projects across machines is future work.

Frame rates use exact rational strings (`30000/1001`, `120/1`). Trim values are
seconds; out-points are exclusive. Crop rectangles use source pixels after
rotation, before scaling; sample aspect ratio is stored separately. Output
settings must be selected deliberately before export; current model defaults
are 3840×2160, 30000/1001 fps, HEVC Main10. Probing does not change output settings
or validate export support. Missing metadata stays unknown, and 10-bit metadata
does not imply HDR processing support.

Probing invokes ffprobe on a local regular file with a 30-second timeout and
limited analysis. It reads stream/container metadata, not a full frame scan.
Video component depth comes from FFmpeg pixel-format descriptors when the stream
does not declare it. Both reported frame rates are retained; their difference
alone does not prove variable frame rate. Media without a known positive
duration is rejected by this first version.

Probe caches persist in the project and compare size, modification/change time,
device, and inode before reuse. A change triggers re-probing; a file changing
during probing is rejected for retry. This is an identity check, not a content
hash. `source_status()` reports freshness without modifying edits. Import
services return a new validated project and preserve the old one on failure.

## Development checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

For headless environments, set `QT_QPA_PLATFORM=offscreen` before running pytest.
Unit tests use temporary directories and simulated media checks. Integration
tests generate short files using locally installed FFmpeg (mpeg4/AAC, FFV1, PCM)
and inspect them with ffprobe, including one-frame 4K/8K 10-bit 120 fps fixtures.
They skip when either binary is missing and require FFmpeg 6 or newer for the
rotation fixture. No existing source footage is needed. To run unit tests only:

```sh
uv run pytest -m "not integration"
```

## Storage

`platformdirs` chooses the OS-standard locations. Projects use the application
data directory; proxies and individual export job directories use the cache
directory; rotating logs use the log directory. On macOS these are under
`~/Library/Application Support/Stitch`, `~/Library/Caches/Stitch`, and
`~/Library/Logs/Stitch`. Run `stitch --check` to see exact paths on your system.
Initialization preserves existing files and each job gets a unique directory.
FFmpeg is supplied locally, not bundled with this prototype.

## Code layout

- `src/stitch/app.py`: entry point, logging, diagnostic command.
- `src/stitch/window.py`: desktop shell and background startup checks.
- `src/stitch/dependencies.py`: executable discovery and bounded version probes.
- `src/stitch/paths.py`: application storage and isolated job directories.
- `src/stitch/models.py`: validated project and media models; generated JSON Schema.
- `src/stitch/project_io.py`: versioned reads and atomic writes.
- `src/stitch/media.py`: local ffprobe adapter and stream metadata normalization.
- `src/stitch/sources.py`: import services and cache freshness checks.
- `tests/`: model, storage, source-cache, media integration, CLI, and Qt lifecycle checks.

Plan: [DOC-62 — Stitch weekend plan](https://rezee.app/kash/docs/62).
Next: [ISSUE-1143 — Clip queue and project controls](https://rezee.app/kash/plan/1143).
