from fractions import Fraction

import pytest

from stitch.audio import active_overlay, normalize_overlay_command, overlay_mix_graph
from stitch.export import concat_command
from stitch.models import (
    AudioSettings,
    AudioStream,
    FileIdentity,
    MediaMetadata,
    Overlay,
    ProbeCache,
    Project,
    Source,
)


@pytest.fixture
def project(tmp_path):
    source = Source(
        path=tmp_path / "music's ગુજરાતી.wav",
        cache=ProbeCache(
            identity=FileIdentity(size=1, mtime_ns=1, ctime_ns=1, device=1, inode=1),
            metadata=MediaMetadata(
                format_name="wav",
                duration=3,
                audio=(
                    AudioStream(
                        index=2,
                        codec="pcm_s24le",
                        duration=3,
                        start_time=5,
                        sample_rate=96000,
                        channels=1,
                    ),
                ),
            ),
        ),
    )
    return Project(
        sources=(source,),
        overlay=Overlay(source_id=source.id, audio=AudioSettings(gain_db=-9), end_behavior="loop"),
    )


def test_normalize_bounded_audio_and_explicit_stream(project, tmp_path):
    source = active_overlay(project)
    command = normalize_overlay_command("ffmpeg", source, tmp_path / "overlay.wav", Fraction(2))
    assert command[command.index("-map") + 1] == "0:2"
    assert command[command.index("-i") + 1] == str(source.path)
    assert command[command.index("-t") + 1] == "2.000000000"
    assert "pcm_s24le" in command and "auto" in command and "-n" in command
    assert "asetpts=PTS-STARTPTS" in command[command.index("-af") + 1]


def test_mix_is_duration_bound_and_does_not_auto_normalize(project, tmp_path):
    graph = overlay_mix_graph(project, Fraction(10))
    assert "volume=-9dB" in graph
    assert "normalize=0" in graph and "dropout_transition=0" in graph
    assert "limit=0.95:level=0:latency=1" in graph
    assert "atrim=end_sample=480000" in graph
    command = concat_command(
        "ffmpeg",
        tmp_path / "list",
        tmp_path / "final.mp4",
        project,
        Fraction(10),
        tmp_path / "overlay.wav",
    )
    assert "-stream_loop" in command and "[mix]" in command
    assert "-af" not in command and "copy" in command
    assert command.index("-stream_loop") < command.index(str(tmp_path / "overlay.wav"))
    stopped = project.model_copy(
        update={"overlay": project.overlay.model_copy(update={"end_behavior": "stop"})}
    )
    assert "-stream_loop" not in concat_command(
        "ffmpeg",
        tmp_path / "list",
        tmp_path / "out.mp4",
        stopped,
        Fraction(10),
        tmp_path / "overlay.wav",
    )


def test_muted_overlay_requires_no_media_access(project):
    muted = project.model_copy(
        update={
            "overlay": project.overlay.model_copy(update={"audio": AudioSettings(enabled=False)})
        }
    )
    assert active_overlay(muted) is None
