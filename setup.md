# Stitch setup for agents

Use this guide to install, run, test, and package the current repository.
Run repository commands from its root unless a section specifies another location.
Replace example paths with paths on the current machine.
See [README.md](README.md) for editing workflows and export behavior.

## 1. Identify the environment

Stitch is a local desktop application, not a web application.
It uses Python 3.12, PySide6, Pydantic, and external FFmpeg processes.
There is no Node.js build, backend server, database, Docker service, or migration step.
Running Stitch requires no API key, `.env` file, or Rezee connection.
Cloning the private repository requires repository access.

The repository controls dependencies through these files:

- `.python-version` selects Python 3.12.
- `pyproject.toml` requires Python `>=3.12,<3.13` and defines runtime/development dependencies.
- `uv.lock` records the resolved dependency versions.
- `packaging/Stitch.spec` defines the macOS application bundle.

Use `uv sync --locked` to reproduce the locked environment.
Do not regenerate `uv.lock` or upgrade dependencies during ordinary setup.
The default sync includes the `dev` dependency group.
It installs pytest, pytest-qt, Ruff, PyInstaller, and psutil alongside the application.

### Tested baseline

The local validation on 2026-10-17 used:

- macOS 26.6.2 on Apple Silicon.
- uv 0.11.12 and Python 3.12.13.
- PySide6 6.12.0 and Pydantic 2.14.1.
- FFmpeg and ffprobe 8.1.1 from Homebrew.
- PyInstaller 6.22.3.

The previous full regression run passed 255 tests at commit `05ddab9`.
Treat this as a recorded result, not the expected count for future revisions.

The macOS bundle declares macOS 14.4 as its minimum version.
Earlier supported macOS versions still need testing.
Intel macOS, Windows, and Linux packages remain untested.
The source installation steps below do not establish equivalent platform support.

## 2. Install system tools

Install Git, uv, FFmpeg, and ffprobe before running integration tests.
Use an installation method permitted by the host environment.
Do not place credentials in commands, documentation, or committed files.

### macOS: tested route

Use an existing Homebrew installation:

```sh
brew install git uv ffmpeg
git --version
uv --version
ffmpeg -version
ffprobe -version
```

If Homebrew is unavailable, obtain it through the machine owner's approved installation process.
Do not install a replacement package manager without permission.

### Ubuntu/Debian: source installation, not tested here

Install the distribution tools:

```sh
sudo apt update
sudo apt install git curl ffmpeg
```

Install uv through an approved package manager or its official installer.
The following command executes a downloaded script.
Review the installer before execution when the host policy requires review.
See the [official uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/).

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal after installation.
Check that `uv`, `ffmpeg`, and `ffprobe` resolve through PATH.
Distribution FFmpeg versions and available encoders can differ.
The rotation integration fixture requires FFmpeg 6 or newer.
The desktop requires a graphical session and the system libraries required by Qt.
An offscreen test run does not establish native desktop playback support.

### Windows: source installation, not tested here

Install Git using the machine's approved installation method.
Install uv through WinGet, as documented by the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/#winget):

```powershell
winget install --id=astral-sh.uv -e
```

Install a trusted FFmpeg distribution containing both `ffmpeg.exe` and `ffprobe.exe`.
Add its executable directory to PATH.
Open a new PowerShell session.
Check the commands:

```powershell
Get-Command git, uv, ffmpeg, ffprobe
ffmpeg -version
ffprobe -version
```

The current packaging script does not build Windows installers.

## 3. Prepare the repository

For a new checkout, run:

```sh
git clone https://git.rezee.app/kash/stitch.git
cd stitch
```

For an existing checkout, enter that directory instead.
Inspect its instructions and worktree before changing files:

```sh
git status --short
git branch --show-current
```

Read any applicable `AGENTS.md` files.
Preserve existing changes that belong to the user or another agent.
Do not reset, clean, or replace the checkout to solve a setup problem.
If repository authentication fails, request access through the existing credential mechanism.

Install the environment:

```sh
uv python install 3.12
uv sync --locked
uv run python --version
uv run stitch --check
```

uv creates `.venv` in the repository.
Activation is unnecessary when commands use `uv run`.
The initial installation needs network access unless the required downloads already exist in local caches.
Do not copy another machine's `.venv` into this checkout.

`stitch --check` creates application storage directories and checks both media tools.
It prints their resolved paths, versions, and storage paths as JSON.
Exit code `0` means these checks passed.
Exit code `1` means a tool or storage directory needs attention.
This command does not check encoder availability or GUI playback.

## 4. Check FFmpeg capabilities

List encoders and filters:

```sh
ffmpeg -hide_banner -encoders
ffmpeg -hide_banner -filters
```

The delivery profiles require these encoders:

| Profile | Video encoder | Delivery audio |
| --- | --- | --- |
| H.264 MP4 | `libx264` | `aac` |
| HEVC Main10 MP4 | `libx265` | `aac` |
| ProRes HQ MOV | `prores_ks` | `pcm_s24le` |

Integration fixtures also use `mpeg4` and `ffv1`.
Crossfades require the `blend` and `acrossfade` filters.
The pipeline also uses scaling, trimming, resampling, mixing, and limiting filters.
Use the integration tests to check the complete pipeline.
Do not silently substitute a different codec or pixel format when an encoder is missing.

Stitch uses external `ffmpeg` and `ffprobe` executables for export and inspection.
Qt can report a different FFmpeg version for preview playback.
Qt's playback libraries do not replace the external tools.

## 5. Run the application

Start the desktop:

```sh
uv run stitch
```

The equivalent module command is `uv run python -m stitch`.
There is no web port or development server.

For a custom FFmpeg location, select **Choose FFmpeg folder…** under Media tools.
Select a directory containing both executables.
Then use **Check again** if a tool still needs attention.
Stitch saves the selected directory in `media-tools.json` beside the projects directory.
This preference takes precedence over other PATH entries during application startup.
Direct shell commands and tests may use a different executable if their PATH differs.

The packaged macOS app also searches `/opt/homebrew/bin` and `/usr/local/bin`.
Finder does not need to inherit the terminal's PATH for these locations.

## 6. Run checks

Run static checks first:

```sh
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
git diff --check
```

Run tests without the integration marker:

```sh
uv run pytest -m "not integration"
```

Run the complete suite when both media tools are available:

```sh
uv run pytest -q
```

Tests generate their own media in temporary directories.
They do not need the user's footage.
Tests can skip when a tool or encoder is missing.
Check the skip summary before reporting that media support passed:

```sh
uv run pytest -ra
```

The complete suite includes short 4K/8K fixtures at high frame rates.
These fixtures can consume substantial memory, CPU, and temporary disk space.
Run the suite serially unless available resources justify parallel media jobs.
Short fixtures do not establish sustained 4K/8K performance.

For a host without a display, use an offscreen Qt platform for tests.
In a POSIX shell:

```sh
QT_QPA_PLATFORM=offscreen uv run pytest -m "not integration"
```

In PowerShell:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
uv run pytest -m "not integration"
Remove-Item Env:QT_QPA_PLATFORM
```

Use a separate test shell if that environment variable already has an important value.
Do not treat an offscreen run as proof of native video playback.
Run desktop playback checks in a graphical session.

### GUI smoke check

Choose a new destination under a scratch directory.
Replace the example path before execution:

```sh
uv run stitch --smoke-test "/absolute/path/to/new-smoke-directory"
```

The destination must not exist.
The check refuses an existing directory instead of replacing its contents.
It generates footage and exercises these operations:

1. Import a folder in natural filename order.
2. Reorder, trim, and crop clips.
3. Configure overlay audio and crossfades.
4. Save and reopen the project.
5. Generate and play a proxy.
6. Export all three delivery profiles into the source folder.
7. Refresh the folder without importing those outputs.

Check `smoke-report.json` for `"status": "passed"`.
The directory retains the project, outputs, preview image, and diagnostic logs.

### Optional long-sequence checks

Run these only when the task needs duration, timing, or memory validation:

```sh
uv run python scripts/endurance.py --seconds 7200
uv run python scripts/endurance.py --seconds 7200 --crossfade 1
```

Each command creates a new temporary directory and prints its location.
The script retains generated media and `report.json`.
It checks timing markers, sampled process memory, and cancellation of an active encoder.
The fixtures use 160×90 video at 12 fps, not 4K/8K footage.

## 7. Use command-line diagnostics

Folder import and refresh use up to four concurrent metadata inspections.
Each ffprobe process uses one decoder thread; export settings are unchanged.
Results stay in natural filename order, and unchanged imported files reuse cached metadata.
Cancellation stops new scheduling and waits for active probes to finish.
The worker limit is `PROBE_WORKERS` in `src/stitch/folders.py`.
Run the concurrency regression tests with `uv run pytest tests/test_folders.py`.

Replace every example path before execution:

```sh
uv run stitch --help
uv run stitch --project-schema
uv run stitch --probe "/absolute/path/to/video.mp4"
uv run stitch --inspect-project "/absolute/path/to/project.json"
uv run stitch --render-clip "/absolute/path/to/project.json" --clip-index 1
uv run stitch --export "/absolute/path/to/project.json" --output "/absolute/path/to/new-output.mp4"
```

These diagnostic commands do not open the GUI.
Clip indexes start at `1`.
The clip renderer retains its diagnostic segment instead of publishing a complete sequence.
Use `.mp4` for H.264/HEVC output and `.mov` for ProRes output.
Add `--overwrite` only when the user authorizes replacement of that exact output.
Ctrl+C cancels a command-line export with exit code `130`.

## 8. Build and check the macOS package

Run packaging on macOS with the development dependencies installed.
The script needs Git, `codesign`, and `ditto` on PATH.

```sh
uv sync --locked
uv run python scripts/package_macos.py
```

The script creates new directories under `build/` and `dist/`.
It does not replace earlier builds.
The resulting `dist/macos-<architecture>-<unique-id>/` directory contains:

- `Stitch.app`.
- `Stitch-macos-<architecture>.zip`.
- `build-info.json` with the source commit, dirty-worktree flag, architecture, and dependency notes.

The script checks the bundle signature before creating the ZIP.
The bundle includes Python, Qt, and Stitch dependencies.
It does not bundle the external FFmpeg command-line executables.
Its signature is ad hoc, not Developer ID signing or notarization.
Do not describe this package as approved for public distribution.

Use the actual bundle path printed by the build:

```sh
"/absolute/path/to/Stitch.app/Contents/MacOS/Stitch" --check
"/absolute/path/to/Stitch.app/Contents/MacOS/Stitch" --smoke-test "/absolute/path/to/new-bundle-check"
```

To check startup through Finder, use another new destination:

```sh
open -n -W "/absolute/path/to/Stitch.app" --args --smoke-test "/absolute/path/to/new-finder-check"
```

Read the resulting `smoke-report.json`.
The exit status from `open` alone does not establish that the application check passed.

## 9. Storage and cleanup

Run `uv run stitch --check` to obtain the actual storage paths.
On macOS, the defaults are:

- Projects: `~/Library/Application Support/Stitch/projects`.
- Tool preference: `~/Library/Application Support/Stitch/media-tools.json`.
- Proxies: `~/Library/Caches/Stitch/proxies`.
- Export jobs: `~/Library/Caches/Stitch/jobs`.
- Application logs: `~/Library/Logs/Stitch`.

Other operating systems use paths selected by `platformdirs`.
Project files reference original media through absolute paths.
Copying a project JSON file does not copy its source footage.
Automatic source relinking is not implemented.

Exports need space for intermediate segments and a staged final file.
The disk check includes a 512 MiB reserve plus an estimate based on the composition.
Proxy generation uses a separate estimate and a 64 MiB reserve.
These reserves are not complete storage requirements for a long export.

Successful sequence exports remove intermediate media but retain diagnostic logs.
Failed exports retain their job artifacts for inspection.
Proxies, package builds, smoke artifacts, and endurance artifacts have no automatic cleanup.
Inspect exact paths before removing artifacts.
Never remove original footage, user projects, or an entire cache/workspace as a setup shortcut.

## 10. Troubleshooting

- **Clone fails:** Check repository access and the configured credential helper. Do not embed a token in the clone URL.
- **Python version is incompatible:** Run `uv python install 3.12`, then `uv sync --locked`.
- **Locked sync fails:** Check network access and dependency consistency. Do not regenerate the lockfile merely to suppress the failure.
- **FFmpeg is missing:** Check PATH and the saved Media tools directory. Check both executables, not just `ffmpeg`.
- **An encoder is missing:** Install an approved FFmpeg build with that encoder. Repeat the profile integration tests.
- **Qt cannot open a display:** Use a graphical session for the desktop. Use offscreen mode only for suitable automated tests.
- **Qt reports a missing system library:** Diagnose the named library for that operating system. Do not assume every Linux distribution uses identical packages.
- **A smoke check fails:** Read `failed_stage` and `error` in `smoke-report.json`. Inspect the retained logs before retrying with a new destination.
- **An export fails:** Read the reported job's `status.json`, `ffmpeg.log`, and `assembly.log` when present.
- **Disk space is insufficient:** Choose a volume with sufficient space. Do not disable the reserve or validation checks.
- **Sources changed or disappeared:** Preserve the project. Use explicit re-import where appropriate, or request the original media location.
- **Folder refresh omits a file:** Check its suffix, import history, and generated-media marker. See the folder rules in `README.md`.
- **Gatekeeper blocks the bundle:** The prototype is not notarized. Use an approved local startup procedure rather than disabling system security.

## 11. Agent handoff checklist

1. Record the OS, architecture, source commit, and worktree state.
2. Check that locked dependency installation succeeds.
3. Check that `stitch --check` resolves both media tools.
4. Record encoder availability and any skipped tests.
5. Run checks appropriate to the changed behavior.
6. Record artifact paths and unresolved platform limitations.
7. Preserve unrelated user changes.
8. Commit implementation changes in small logical chunks when authorized.

Use the established commit format: `[ISSUE-number] One or two sentences describing the change.`
Use the actual issue number for the work.
Do not push or publish a package without authorization.
The project plan is [DOC-62](https://rezee.app/kash/docs/62).
The repository itself is sufficient for local setup without access to that document.
