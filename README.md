# Stitch

A desktop video assembler for arranging, trimming, and cropping clips and adding
audio to long-form video. The current build is the application foundation
(ISSUE-1142): a desktop window, background media-tool checks, and local storage.
Import, editing, preview, and export are upcoming issues.

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

## Development checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

For headless environments, set `QT_QPA_PLATFORM=offscreen` before running pytest.
Tests use temporary directories and simulated media checks; they do not require
installed codecs or source footage.

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
- `tests/`: storage, dependency failure, and Qt lifecycle checks.

Plan: [DOC-62 — Stitch weekend plan](https://rezee.app/kash/docs/62).
Next: [ISSUE-1144 — Project model and media probing](https://rezee.app/kash/plan/1144).
