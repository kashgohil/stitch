"""Build an ad-hoc-signed local prototype without replacing any previous build."""

import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    if sys.platform != "darwin":
        raise SystemExit("Build the macOS app on macOS. Other OS packages are not implemented yet.")
    root = Path(__file__).resolve().parents[1]
    for name in ("build", "dist"):
        (root / name).mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f"macos-{platform.machine()}-", dir=root / "dist"))
    work = Path(tempfile.mkdtemp(prefix="pyinstaller-", dir=root / "build"))
    print(f"Package destination: {output}", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(root / "packaging/Stitch.spec"),
            "--distpath",
            str(output),
            "--workpath",
            str(work),
        ],
        cwd=root,
        check=True,
    )
    app = output / "Stitch.app"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    archive = output / f"Stitch-macos-{platform.machine()}.zip"
    subprocess.run(
        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], check=True
    )
    manifest = {
        "app": str(app),
        "archive": str(archive),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "signing": "ad hoc; not notarized",
        "external_dependency": "FFmpeg and ffprobe; not included",
    }
    manifest["source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
    ).strip()
    manifest["source_dirty"] = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
        ).strip()
    )
    (output / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
