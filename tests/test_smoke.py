import json
import shutil
import subprocess
import sys

import pytest
from test_render_integration import require_encoder


def test_smoke_refuses_existing_directory_without_touching_files(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("keep")
    result = subprocess.run(
        [sys.executable, "-m", "stitch", "--smoke-test", str(tmp_path)],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 1 and marker.read_text() == "keep"


@pytest.mark.integration
@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_gui_smoke_runner_exports_all_profiles(tmp_path):
    for encoder in ("libx264", "libx265", "prores_ks"):
        require_encoder(encoder)
    directory = tmp_path / "smoke"
    result = subprocess.run(
        [sys.executable, "-m", "stitch", "--smoke-test", str(directory)],
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    report = json.loads((directory / "smoke-report.json").read_text())
    assert report["status"] == "passed" and report["preview_frame_decoded"]
    assert [p["profile"] for p in report["profiles"]] == ["h264", "hevc_main10", "prores_hq"]
    saved = json.loads((directory / "projects" / "smoke.json").read_text())
    assert saved["output"]["join_mode"] == "crossfade"
    assert saved["output"]["crossfade_duration"] == 0.2
    assert saved["folder"] == str(directory / "footage")
    assert len(saved["folder_history"]) == 2 and len(saved["clips"]) == 2
    for profile in report["profiles"]:
        assert profile["metadata"]["video"]["duration"] == pytest.approx(2.6, abs=0.000001)
        assert profile["metadata"]["stitch_generated"]
