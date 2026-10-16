# Stitch

A desktop video assembler for arranging, trimming, and cropping clips and adding
audio to long-form video. The current build includes the desktop shell, media-tool
checks, a clip queue with trim/crop and source-audio controls, output settings,
atomic save/open, cached media probing, and full-sequence software export with
progress, cancellation, configurable crossfades, and one music/voice-over overlay. Selected clips can be
inspected through cached, low-resolution proxies.

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

## macOS prototype

The package contains Python, Qt, and Stitch. Unzip it and open **Stitch.app** in
Finder; normal editing does not require a terminal or a Python installation.
The external **ffmpeg** and **ffprobe** command-line tools must already be
installed separately, with `libx264`, `libx265`, AAC, and `prores_ks` support.
The app does not download or install them. Finder launches recognize the standard
Homebrew folders `/opt/homebrew/bin` and `/usr/local/bin`. For another installation,
use **Choose FFmpeg folder…** in Media tools; the folder is saved for future launches
and checked in the background. Folders missing either executable are rejected
without replacing the saved folder.

The current build is **Apple Silicon (arm64)**, tested on **macOS 26.6.2**. It
declares macOS 14.4 as the minimum for the locked Qt 6.12 runtime; earlier supported
versions still require testing. See [Qt's platform requirements](https://doc.qt.io/qt-6/supported-platforms.html).
Intel macOS, Windows, and Linux packages have not been built or tested.

This is a local prototype with ad-hoc code signing, **not Developer ID signing or
notarization**. Downloaded copies can be blocked by Gatekeeper. Public distribution
still needs release signing/notarization and a dependency-license review; collected
dependency metadata and available license files are included as notices, not as a
claim that the release compliance work is complete. FFmpeg command-line binaries
are not bundled; Qt's own playback libraries are part of the Qt runtime.

To build on a Mac with the development dependencies installed:

```sh
uv sync --locked
uv run python scripts/package_macos.py
```

The script creates a new `dist/macos-<architecture>-<unique-id>/` directory for each
build, containing `Stitch.app`, a ZIP, and `build-info.json`. Previous builds are
not removed. It verifies the bundle's code signature before creating the ZIP.
The build uses [PyInstaller's macOS bundle support](https://pyinstaller.org/en/stable/usage.html)
and does not publish or upload the package.

An explicit GUI-driven package check generates its own short media, edits it,
tests proxy playback, and exports all three profiles:

```sh
"/path/to/Stitch.app/Contents/MacOS/Stitch" --smoke-test "/path/to/new-check-directory"
```

The destination must not exist. The check leaves fixtures, exports, logs, and
`smoke-report.json` for inspection. It does not use or modify an existing project.

## Build a sequence

1. Choose **Add videos…** and select your clips. Metadata inspection runs in the
   background; it does not load entire videos into memory. **Stop import** keeps
   successful imports and stops after the current inspection finishes (up to
   30 seconds). Failed files are listed without discarding other imported clips.
2. Select a row. Use **Move up**, **Move down**, and **Remove clip** to arrange it.
   Removing a clip never deletes its source file.
3. Set in/out points in seconds, or leave **Use source end** checked. Enable
   **Crop this clip** to enter a rectangle in rotated source pixels. Uncheck
   **Keep source audio** to mute that clip; adjust **Source gain** independently
   for each clip. Apply changes or select another clip
   to validate the edits; invalid drafts stay visible for correction or reset.
4. Choose output dimensions, an exact frame rate, and a profile. A new project
   matches its first successful import's dimensions and reported frame rate
   unless you have already changed the output settings. Odd source dimensions
   round up to even output dimensions. Exports use software encoders; local
   FFmpeg must include the encoder for the selected profile.
   Choose **Hard cut** or **Crossfade** under **Join**; **Overlap** sets the
   transition duration. Apply output settings to validate the whole sequence.
5. **Save** or **Save as…** writes a `.json` project. **Open…** restores the queue
   and settings. An asterisk in the title indicates unsaved changes or pending
   edits. New, Open, and Close prompt to save, discard, or cancel when necessary.
6. Choose **Export…** and select an output filename: `.mp4` for H.264/HEVC or
   `.mov` for ProRes HQ. The save dialog asks before replacing an existing file.
   Per-clip and overall progress appear with elapsed time. **Cancel export** stops
   active rendering; closing the window waits for media work to stop first.

The Source column flags missing or changed files without removing their edits.
Reimporting an existing source refreshes its metadata and adds another clip;
incompatible refreshed metadata is rejected. Relinking is not implemented yet.
Tests cover short 4K/8K render and proxy fixtures, not sustained native-resolution
playback or multi-hour high-resolution export performance.

## Inspect a clip with a proxy

1. Select a clip in **Clips**, then open **Selected clip preview**.
2. Choose **720p** or **1080p**, then **Generate proxy**. Generation runs in a
   background worker with progress and **Cancel proxy**. It processes the entire
   source, not just the trimmed interval, so later trim changes reuse the same
   proxy. Large sources can take significant time to prepare.
3. Use **Play clip** / **Pause** and the seek slider. Playback starts at the
   applied in-point and pauses at the out-point; seeking can inspect the entire
   source. **Set in** / **Set out** copy the current source time into draft trim
   fields. Apply the changes to validate them and update the playback interval.

Preview uses H.264 8-bit SDR, 30 fps, and AAC stereo 48 kHz at 128 kbit/s, regardless
of the export profile. The picture is rotated and fitted into a 1280×720 or
1920×1080 square-pixel canvas, with padding and no upscaling. High-FPS footage is
temporally downsampled for inspection. The preview does **not** simulate crop,
source gain/mute, crossfades, overlay audio, or final export color/quality; **Mute preview**
affects only local playback. HDR sources are rejected, as with the current exporter.
There is no full-sequence playback, frame stepping, or frame-accurate seek yet.

Proxy time zero corresponds to the original video's start, including media with
nonzero container timestamps; relative audio delay is preserved. Trim decisions
remain in original-source seconds and crop decisions remain in original pixels.
Export always resolves the project's original sources, never the preview cache.
Changing proxy quality cannot change output resolution, frame rate, or audio quality.

Valid proxies are reused across clips and project reopen without transcoding.
Choose the same quality to reuse a 1080p proxy; the preview selector defaults to
720p and is not a project setting. Cache keys include source path, file identity,
cached metadata, resolution, and recipe version. Cache entries also record the
generated file's identity. Missing or modified entries are not played.

A changed or missing original stops preview rather than displaying stale media.
Reimport a changed source to refresh its metadata, then generate its new proxy.
Reimport also adds a queue entry; remove the extra entry if it is not wanted.
Existing edits are preserved, and incompatible source metadata is rejected.
Switching sources or proxy quality cancels obsolete generation; import/export
also cancels generation and pauses playback to avoid competing media work.
Closing waits for the worker to stop. Encoding cancellation is prompt; a running
validation probe may take up to 30 seconds.

Generation uses two codec/filter threads and streams media to disk. A bitrate-based
space estimate and a 64 MiB reserve guard the cache volume; this is not a peak-memory
or fixed-file-size guarantee. Incomplete media is removed after cancellation or
failure, while `proxy-*.log` diagnostics remain in the logs directory. Completed
proxies, superseded cache entries, and logs have no automatic eviction yet.

Playback uses [Qt Multimedia's media player](https://doc.qt.io/qtforpython-6/PySide6/QtMultimedia/QMediaPlayer.html).
Codec/backend availability depends on the installed Qt build and OS. Native macOS
playback is tested; Windows/Linux playback and packaging still need validation.

## Add music or voice-over

Open the **Audio overlay** tab beside **Output**, then choose **Add audio…**.
Inspection runs in the background, like video import. The first audio stream is
used, including when the selected file also contains video. Only one overlay
track is supported; it starts with its first decoded audio sample at sequence
time zero, independently of container start timestamps.

- **Overlay gain** controls its level separately from each clip's **Source gain**.
  Both controls range from −60 to +12 dB; 0 dB retains the input level below the
  limiter threshold. Source mute is per clip; **Include overlay** mutes/unmutes
  the overlay without removing its saved settings.
- **Stop when audio ends** leaves silence from the overlay after its end; source
  audio continues. **Loop to sequence end** repeats the audio continuously across
  clip boundaries. Both modes stop at the sequence end. Loops do not crossfade,
  so a file whose ends do not match can click at the repeat point.
- **Replace audio…** preserves gain, mute, and loop/stop settings. Failed or
  cancelled replacement keeps the existing overlay. **Remove audio** removes
  only the project reference, never the source file. Save/reopen retains all
  clip gains and overlay settings.

The exporter normalizes the overlay once into a bounded-duration, 48 kHz stereo
24-bit PCM file on disk, then loops or pads it during final assembly. This avoids
buffering an entire track in RAM and avoids looping unrelated video-container
padding. The temporary audio file is included in disk estimates and removed
after successful export; failed jobs retain it with `overlay.log` for diagnosis.
Muted overlays are not read, so a missing muted overlay does not block export.

Mixing uses the chosen gains without automatic volume normalization. A
latency-compensated sample-peak limiter caps source gain and the final sum at
0.95 full scale, with no automatic makeup gain. It can reduce loud passages;
it is not loudness mastering or a true-peak guarantee after lossy AAC encoding.
There are no overlay fades, ducking, placement offsets, or additional overlay tracks yet.
The [FFmpeg audio-filter documentation](https://ffmpeg.org/ffmpeg-filters.html#amix)
describes the mixing and limiter behavior used here.

## Crossfade joins

**Output → Join → Crossfade** blends adjacent pictures and source audio after
trim, crop, and normalization. One project-level **Overlap** applies to every
boundary. Hard cuts remain the default, including when opening older projects.
Zero overlap uses hard cuts; a one-clip sequence has no transition.

Overlap rounds **up** to a whole output frame: 0.25 seconds at 30 fps becomes
8 frames (about 0.267 seconds). Each end clip must contain one overlap, and
each middle clip must contain two. Invalid trims, settings, or reorderings are
rejected with an explanation; transitions are never silently shortened.
Exactly touching incoming/outgoing overlaps are allowed, with no middle body.
The resulting duration is the sum of normalized clip durations minus one rounded
overlap per boundary. Overlay audio starts at zero and stops/loops against this
shortened duration. Save/reopen retains both join settings.

The blend is linear in the normalized SDR pixel values, with matching linear
audio fades—not optical flow, linear-light compositing, or an equal-power audio
curve. Source gain and mute apply before the fade; missing audio becomes silence.
The selected-clip proxy does not preview transitions. Inspect an exported sample
to evaluate the complete sequence.

Crossfade exports render alternating non-overlapped bodies and overlap chunks
**directly from original media**, using at most two source inputs per process.
Each output picture has one final-profile video encode; the last assembly pass
copies video and encodes delivery audio once. Resolution, frame rate, and the
profile's pixel format are retained. HEVC crossfade chunks disable B-frames
consistently to avoid an observed one-frame 8K reorder-timestamp failure; this
trades compression efficiency for reliable short-chunk assembly, without changing
CRF, resolution, or bit depth. Hard-cut encoding is unchanged.

Chunks run sequentially with bounded media processing, cancellation, disk-space
checks, validation, and atomic publication. To preserve the same normalized VFR
frame grid, a tail chunk can decode the source prefix again. Crossfades therefore
add decoding and process overhead; they are not a faster export mode or a promise
of real-time 4K/8K performance.

## Sequence export

The UI exports the current validated edit snapshot, even if it has not been saved.
Editing is disabled until that export finishes. The terminal equivalent reads a
saved project:

```sh
uv run stitch --export "/path/to/project.stitch.json" --output "/path/to/final.mp4"
# Add --overwrite only when you want to replace an existing output.
```

CLI progress goes to stderr; the completed output/job paths go to stdout as JSON.
Ctrl+C cancels and exits with code 130. Other failures return code 1. Cancellation
normally stops FFmpeg promptly; an active metadata check may take up to 30 seconds.

Clips render sequentially into a job-specific cache directory. The final pass
copies the encoded video and encodes stereo 48 kHz delivery audio once: AAC at
320 kbit/s for MP4, or 24-bit PCM for the ProRes master. Fractional frame boundaries
use cumulative timestamp rounding to avoid per-clip rounding drift. Whole-job
progress allocates 90% to rendering (85% plus 5% overlay preparation when enabled)
and the remainder to assembly/validation;
it measures work stages, not a reliable time-remaining estimate.

Before rendering, Stitch checks available space for intermediates and a staged
output, including a 512 MiB reserve. Estimates scale with resolution, frame rate,
duration, and profile, with a 50% size margin. Constant-quality encoding has no
fixed file size, so these are planning estimates, not disk-space guarantees.
Space is checked again between clips/chunks and before assembly.

The output is staged in a private directory beside the destination. Only a
successfully validated file is published. Source-file paths and aliases cannot be
overwritten. Existing outputs need explicit replacement permission and are checked
again before replacement. A newly appeared destination is never overwritten.
New-file atomic publication requires filesystem hard-link support; unsupported
filesystems fail safely. Publication is the commit point: cancellation after that
point does not undo a completed export.

Successful jobs remove intermediate segment media while retaining small logs and
status reports. Failed/cancelled jobs keep segments and diagnostics for inspection;
their incomplete staged final output is removed. The final file is not replaced
on a render, assembly, validation, or pre-publication cancellation failure.
Failure messages include the job directory. Retained failed jobs consume disk
space; automatic recovery/resume and cache management are not implemented.

Detected HDR and mixed source color tags are rejected pending a deliberate color
conversion pipeline. Unknown range/SAR assumptions are reported as warnings.

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
- Source-audio gain, peak limiting, and mute; stereo 48 kHz, 24-bit PCM for every
  intermediate,
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
output is overwritten. This single-clip diagnostic retains its media; the full
sequence exporter described above cleans successful segment media. Do not call
the synchronous `render_segment()` or `export_project()` API on the GUI thread.

Implementation references: [FFmpeg command options](https://ffmpeg.org/ffmpeg.html)
and [filter documentation](https://ffmpeg.org/ffmpeg-filters.html); sequence assembly
uses the [concat demuxer](https://ffmpeg.org/ffmpeg-formats.html#concat).

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
Sequence tests decode both sides of joins across mixed-resolution/frame-rate clips,
exercise all profiles, and check cancellation, output guards, and retained diagnostics.
Crossfade tests inspect blended pixels and tone gains, middle clips without a body,
fractional output rates, missing/muted audio, shortened overlays, original-only
encoder inputs, and 4K/8K at 120 fps across all three export profiles.
Proxy tests cover both cache qualities, delayed audio and nonzero source clocks,
4K/8K HEVC input, source/cache invalidation, cancellation, and original-only export.
Qt tests exercise background cancellation, source-time trim markers, real proxy
video decoding/playback/seeking, and cache reuse after reopening a project.
A macOS spot check using 160×90, 12 fps generated video at 60 versus 600 seconds
observed sampled RSS of about 35→41 MiB for Python and 29→35 MiB for FFmpeg.
This checks duration scaling at low resolution, not peak-memory guarantees or
multi-hour 4K/8K throughput. Codec buffering still depends on resolution/settings.
The release matrix additionally checks 1080p at 24/30/60/120 fps, mono and 5.1
sources at 44.1/96 kHz, quoted/Unicode/backslash paths, and an actual unavailable
encoder. Full-source duration rounding is tested so ffprobe's microsecond precision
does not accidentally add a frame; explicit trim ends still round up to a frame.
They skip when either binary is missing and require FFmpeg 6 or newer for the
rotation fixture. No existing source footage is needed. To run unit tests only:

```sh
uv run pytest -m "not integration"
```

### Long-composition check

```sh
uv run python scripts/endurance.py --seconds 7200
# Exercise 23 one-second crossfades (7,177-second resulting sequence):
uv run python scripts/endurance.py --seconds 7200 --crossfade 1
```

This opt-in check creates a private temporary directory and keeps its artifacts.
It compares a 60-second export with a two-hour composition of 24 five-minute clips
at **160×90, 12 fps**, including source audio and a looping overlay. It samples
Python and child-process RSS every 100 ms, checks final durations, and decodes
bounded pulse/flash windows near the start, a clip join, midpoint, and end to
detect drift. It also cancels an active FFmpeg process and checks that an existing
destination survives. The printed `report.json` records measurements and paths.
This is not a native 4K/8K performance test, a frame-by-frame two-hour decode, or a
guarantee of peak memory or cancellation latency on another machine.

On 2026-10-17 (IST), macOS 26.6.2 arm64 with FFmpeg 8.1.1 completed the 60-second
and 7,200-second cases in 1.11 s and 110.59 s. Sampled Python RSS peaks were
39.80/75.59 MiB; child-process peaks were 21.17/64.16 MiB. Audio marker error was
0.042 ms at 5, 300, 3,600, and 7,195 seconds, with video markers on the expected
frames. Both final stream durations matched 7,200 seconds. Cancelling an observed
active encoder returned in 0.095 s and preserved the existing destination.

The same day's crossfade run (23 one-second overlaps) produced 7,177 seconds of
video/audio in 121.87 s. The 60-second comparison took 1.12 s. Sampled Python RSS
peaks were 39.77/75.58 MiB; child-process peaks were 21.23/63.47 MiB. Checked markers
at 5, 304, 3,593, and 7,172 seconds had 0.042 ms audio error and zero video-frame
error. Active-encoder cancellation returned in 0.094 s without replacing the
destination. These are local low-resolution measurements, not high-resolution
throughput or absolute memory bounds.

## Storage

`platformdirs` chooses the OS-standard locations. Projects use the application
data directory; proxies and individual export job directories use the cache
directory; rotating logs use the log directory. On macOS these are under
`~/Library/Application Support/Stitch`, `~/Library/Caches/Stitch`, and
`~/Library/Logs/Stitch`. Run `stitch --check` to see exact paths on your system.
Initialization preserves existing files and each job gets a unique directory.
FFmpeg is supplied locally, not bundled with this prototype.
The chosen tool folder is saved as `media-tools.json` beside the projects folder.
Proxy logs and diagnostic/build artifacts are retained until removed manually.

## Code layout

- `src/stitch/app.py`: entry point, logging, diagnostic command.
- `src/stitch/window.py`: clip queue, project actions, and background startup checks.
- `src/stitch/editors.py`: validated trim/crop, source-audio, and output draft controls.
- `src/stitch/document.py`: immutable editing operations and save-point tracking.
- `src/stitch/workers.py`: background import, export, and cancellable proxy generation.
- `src/stitch/preview.py`: Qt proxy playback, source-time inspection, and trim markers.
- `src/stitch/proxy.py`: identity-keyed preview cache and bounded software transcoding.
- `src/stitch/dependencies.py`: executable discovery and bounded version probes.
- `src/stitch/tool_setup.py`: saved external-tool folder and frozen-process environment.
- `src/stitch/smoke.py`: isolated GUI-driven package validation.
- `src/stitch/paths.py`: application storage and isolated job directories.
- `src/stitch/models.py`: validated project and media models; generated JSON Schema.
- `src/stitch/project_io.py`: versioned reads and atomic writes.
- `src/stitch/media.py`: local ffprobe adapter and stream metadata normalization.
- `src/stitch/sources.py`: import services and cache freshness checks.
- `src/stitch/render_plan.py`: pure segment command generation and encoder profiles.
- `src/stitch/render.py`: isolated single-segment execution, diagnostics, and validation.
- `src/stitch/joins.py`: original-only body/overlap plans and bounded crossfade rendering.
- `src/stitch/process.py`: process supervision, bounded progress, and cancellation.
- `src/stitch/export.py`: sequential assembly, disk estimates, and safe publication.
- `src/stitch/audio.py`: bounded overlay preparation and whole-sequence mixing.
- `tests/`: model, storage, source-cache, media integration, CLI, and Qt lifecycle checks.

Plan: [DOC-62 — Stitch weekend plan](https://rezee.app/kash/docs/62).
Release pass: [ISSUE-1149 — Validation and macOS packaging](https://rezee.app/kash/plan/1149).
Deferred: sustained high-resolution performance validation, HDR/color conversion,
hardware encoders, full-sequence effects preview, cache management, relinking,
signed/notarized distribution, and Windows/Linux packaging.
