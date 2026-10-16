# Stitch

A desktop video assembler for arranging, trimming, and cropping clips and adding
audio to long-form video. The current build includes the desktop shell, media-tool
checks, a clip queue with trim/crop and source-audio controls, output settings,
atomic save/open, cached media probing, and a single-clip software render core.
Preview, overlay-audio controls, and full-sequence export are upcoming issues.

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

## Build a sequence

1. Choose **Add videos…** and select your clips. Metadata inspection runs in the
   background; it does not load entire videos into memory. **Stop import** keeps
   successful imports and stops after the current inspection finishes (up to
   30 seconds). Failed files are listed without discarding other imported clips.
2. Select a row. Use **Move up**, **Move down**, and **Remove clip** to arrange it.
   Removing a clip never deletes its source file.
3. Set in/out points in seconds, or leave **Use source end** checked. Enable
   **Crop this clip** to enter a rectangle in rotated source pixels. Uncheck
   **Keep source audio** to mute that clip. Apply changes or select another clip
   to validate the edits; invalid drafts stay visible for correction or reset.
4. Choose output dimensions, an exact frame rate, and a profile. A new project
   matches its first successful import's dimensions and reported frame rate
   unless you have already changed the output settings. Odd source dimensions
   round up to even output dimensions. Profiles are saved choices, not yet
   executable exports or guarantees of hardware support.
5. **Save** or **Save as…** writes a `.json` project. **Open…** restores the queue
   and settings. An asterisk in the title indicates unsaved changes or pending
   edits. New, Open, and Close prompt to save, discard, or cancel when necessary.

The Source column flags missing or changed files without removing their edits.
Reimporting an existing source refreshes its metadata and adds another clip;
incompatible refreshed metadata is rejected. Relinking is not implemented yet.
There is no playback or final sequence export in the UI yet. A terminal diagnostic
can render one clip as described below. Tests cover short 4K/8K render fixtures,
not sustained high-resolution playback or multi-hour export performance.

## Render one clip (developer diagnostic)

Save a project in the UI, then run:

```sh
uv run stitch --render-clip "/path/to/project.stitch.json" --clip-index 1
```

Clip numbers are one-based. This reads the saved project, uses its original
source media and edit/output settings, and renders one normalized MOV segment
in a new job directory. It does not assemble the sequence, mix overlay audio,
or publish a final delivery file. The command prints the segment path, job path,
validated output metadata, and any assumptions/warnings as JSON. Failure returns
exit code 1 with a diagnostic path. No Qt window is opened.

The renderer supports:

- Timestamp-aware trim, including nonzero source clocks and delayed source audio.
- Automatic orthogonal rotation, then static crop, aspect-preserving Lanczos
  fit, and black padding. Upscaling stays disabled unless selected in the project.
  Pixel aspect ratio is accounted for; fitted dimensions round down to even pixels.
- Constant output frame rate. Segment duration rounds up to the next output frame;
  the last video frame and silence cover the rounding interval. Trims must contain
  at least one decoded video frame. PCM duration rounds to the nearest audio sample.
- Software `libx265` Main10 (`yuv420p10le`), `libx264` High (`yuv420p`), and
  `prores_ks` 422 HQ (`yuv422p10le`). Pixel format fallback is disabled and output
  metadata is checked. H.264 deliberately reduces higher-bit-depth input to 8-bit.
- Source-audio gain and mute; stereo 48 kHz, 24-bit PCM for every intermediate,
  including silence for clips without usable audio in the selected interval.

All intermediate profiles use PCM, not AAC, to avoid repeated lossy audio encoding
and per-clip AAC encoder delay. Final delivery AAC encoding belongs to assembly
and audio mixing. Intermediate files can be large. Defaults are CRF 18 and the
medium preset for H.264/HEVC, with two encoder/filter threads; Python callers can
configure `RenderOptions(crf=..., preset=..., threads=...)`. ProRes uses HQ rather
than CRF. Performance depends on resolution, codec, storage, and machine.

Detected PQ/HLG HDR input is rejected: this is not an HDR conversion pipeline.
Known SDR color tags are retained and range is normalized to limited range.
Unknown range assumes limited; unknown pixel aspect ratio assumes square pixels,
both with warnings. Mixed-color-space sequence handling is not implemented yet.

Rendering checks source identity before and after execution, rejects stale caches,
and verifies output dimensions, codec/pixel format, rate, timestamps, and duration.
Each job keeps `status.json` (including arguments and state) and `ffmpeg.log`.
Successful and failed diagnostic jobs are retained; no source or existing final
output is overwritten. Progress, cancellable jobs, disk estimates, final-output
publication, and automatic cleanup are part of the next assembly issue. Do not
call the synchronous `render_segment()` API on the GUI thread.

Implementation references: [FFmpeg command options](https://ffmpeg.org/ffmpeg.html)
and [filter documentation](https://ffmpeg.org/ffmpeg-filters.html).

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
settings must be reviewed before export; empty-project model defaults are
3840×2160, 30000/1001 fps, HEVC Main10. The UI matches the first import as described
above; standalone probing does not change settings or validate export support.
Missing metadata stays unknown, and 10-bit metadata
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
Render checks exercise all three software profiles, 4K H.264 and 8K HEVC at 120 fps,
and decoded-pixel/audio-sample checks for trim, crop/padding, rotation, and A/V offset.
These are correctness fixtures, not throughput benchmarks. Encoder-specific tests
skip when the local FFmpeg build lacks that encoder.
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
- `src/stitch/window.py`: clip queue, project actions, and background startup checks.
- `src/stitch/editors.py`: validated trim/crop, source-audio, and output draft controls.
- `src/stitch/document.py`: immutable editing operations and save-point tracking.
- `src/stitch/workers.py`: cancellable, sequential background import.
- `src/stitch/dependencies.py`: executable discovery and bounded version probes.
- `src/stitch/paths.py`: application storage and isolated job directories.
- `src/stitch/models.py`: validated project and media models; generated JSON Schema.
- `src/stitch/project_io.py`: versioned reads and atomic writes.
- `src/stitch/media.py`: local ffprobe adapter and stream metadata normalization.
- `src/stitch/sources.py`: import services and cache freshness checks.
- `src/stitch/render_plan.py`: pure segment command generation and encoder profiles.
- `src/stitch/render.py`: isolated single-segment execution, diagnostics, and validation.
- `tests/`: model, storage, source-cache, media integration, CLI, and Qt lifecycle checks.

Plan: [DOC-62 — Stitch weekend plan](https://rezee.app/kash/docs/62).
Next: [ISSUE-1146 — Safe long-form export](https://rezee.app/kash/plan/1146).
