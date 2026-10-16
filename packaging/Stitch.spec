# Build with scripts/package_macos.py. Output directories are unique per build.
from importlib.metadata import distribution
from pathlib import Path

root = Path(SPECPATH).parent
data = [(str(root / "README.md"), ".")]
for name in (
    "pyside6", "pyside6-essentials", "pyside6-addons", "shiboken6", "pydantic",
    "pydantic-core", "platformdirs", "typing-extensions", "typing-inspection", "annotated-types",
):
    package = distribution(name)
    for file in package.files or ():
        if file.name == "METADATA" or "license" in str(file).lower():
            data.append((str(package.locate_file(file)), f"dependency-notices/{name}/{file.parent}"))

a = Analysis(
    [str(root / "packaging/launcher.py")],
    pathex=[str(root / "src")],
    binaries=[], datas=data, hiddenimports=[], hookspath=[], runtime_hooks=[],
    excludes=["pytest", "psutil", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Stitch", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Stitch")
app = BUNDLE(
    coll, name="Stitch.app", bundle_identifier="app.rezee.stitch.prototype",
    version="0.1.0",
    info_plist={
        "CFBundleDisplayName": "Stitch",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "14.4",
        "LSApplicationCategoryType": "public.app-category.video",
    },
)
