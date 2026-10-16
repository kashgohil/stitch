import shutil

import pytest
from test_render_integration import ffmpeg, require_encoder

from stitch.export import export_project
from stitch.folders import scan_folder
from stitch.media import probe_media
from stitch.models import OutputSettings, Project
from stitch.paths import AppPaths
from stitch.project_io import load_project, save_project
from stitch.proxy import ProxySettings, generate_proxy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
    ),
]


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "ગુજરાતી footage"
    folder.mkdir()
    for name, color, rate in (("clip10.mov", "blue", 60), ("clip2.mov", "red", 24)):
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size=160x90:rate={rate}:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=0.5",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s24le",
            folder / name,
        )
    return folder


@pytest.fixture
def paths(tmp_path):
    return AppPaths(*(tmp_path / n for n in ("projects", "proxies", "jobs", "logs")))


@pytest.mark.parametrize("profile", ["h264", "hevc_main10", "prores_hq"])
@pytest.mark.parametrize("join_mode", ["hard_cut", "crossfade"])
def test_folder_export_in_source_directory_is_not_reimported(
    folder, paths, tmp_path, profile, join_mode
):
    require_encoder(
        {"h264": "libx264", "hevc_main10": "libx265", "prores_hq": "prores_ks"}[profile]
    )
    initial = Project(
        output=OutputSettings(
            width=160,
            height=90,
            frame_rate="30/1",
            profile=profile,
            join_mode=join_mode,
            crossfade_duration=0.1,
        )
    )
    result = scan_folder(initial, folder)
    assert result.imported == 2 and not result.errors
    project = result.project
    assert [s.path.name for s in project.sources] == ["clip2.mov", "clip10.mov"]
    saved = tmp_path / "folder.json"
    save_project(project, saved)
    target = folder / ("assembled.mov" if profile == "prores_hq" else "assembled.mp4")
    exported = export_project(project, target, paths)
    assert exported.metadata.stitch_generated
    assert exported.metadata.video.duration == pytest.approx(0.9 if join_mode == "crossfade" else 1)
    refreshed = scan_folder(load_project(saved), folder)
    assert refreshed.project == project and refreshed.imported == 0 and not refreshed.errors
    # The marker travels with the output, even in a different/new project.
    fresh = scan_folder(initial, folder)
    assert fresh.imported == 2 and len(fresh.project.clips) == 2


def test_real_audio_corruption_and_copied_proxy_are_skipped(folder, paths):
    require_encoder("libx264")
    initial = scan_folder(Project(), folder).project
    source = initial.sources[0]
    preview = generate_proxy(source, ProxySettings(), paths)
    assert probe_media(preview).stitch_generated
    shutil.copy2(preview, folder / "copied-preview.mp4")
    ffmpeg("-f", "lavfi", "-i", "sine=duration=0.1", "-c:a", "aac", folder / "audio-only.mp4")
    (folder / "corrupt.mp4").write_bytes(b"not media")
    result = scan_folder(initial, folder)
    assert result.imported == 0 and result.project == initial
    assert len(result.errors) == 1 and "corrupt.mp4" in result.errors[0]
    assert result.skipped == 4
