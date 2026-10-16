"""Opt-in long composition validation; all artifacts stay in a new private directory.

Run with `uv run python scripts/endurance.py --seconds 7200`.
This is a low-resolution duration-scaling check, not a 4K/8K throughput benchmark.
"""

import argparse
import array
import json
import math
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import psutil

from stitch.document import change_project
from stitch.export import export_project
from stitch.media import probe_media
from stitch.models import AudioSettings, Clip, OutputSettings, Project
from stitch.paths import AppPaths
from stitch.process import RenderCancelled
from stitch.project_io import save_project
from stitch.sources import add_video, set_overlay


def ffmpeg(*args):
    return subprocess.run(
        [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=300,
    ).stdout


class MemorySampler:
    def __init__(self):
        self.stop = threading.Event()
        self.python_peak = 0
        self.children_peak = 0
        self.samples = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        process = psutil.Process()
        while not self.stop.is_set():
            try:
                self.python_peak = max(self.python_peak, process.memory_info().rss)
                children = 0
                for child in process.children(recursive=True):
                    try:
                        children += child.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                self.children_peak = max(self.children_peak, children)
                self.samples += 1
            except psutil.NoSuchProcess:
                pass
            self.stop.wait(0.1)

    def result(self):
        self.stop.set()
        self.thread.join()
        return {
            "python_peak_mib": round(self.python_peak / 2**20, 2),
            "children_peak_mib": round(self.children_peak / 2**20, 2),
            "samples": self.samples,
            "interval_seconds": 0.1,
        }


def inspect_markers(path, seconds):
    """Decode bounded windows near the start, middle, and end, including a clip join."""
    results = []
    markers = sorted({5, 300 if seconds > 305 else 5, int(seconds // 10) * 5, int(seconds) - 5})
    for marker in markers:
        start = marker - 0.25  # Exactly three 12-fps frames before the flash.
        audio = array.array(
            "f",
            ffmpeg(
                "-ss",
                start,
                "-i",
                path,
                "-t",
                "0.6",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "f32le",
                "-",
            ),
        )
        onset = next((i / 48000 for i, sample in enumerate(audio) if abs(sample) > 0.025), None)
        assert onset is not None, f"No audio marker at {marker}s"
        pixels = ffmpeg(
            "-ss",
            start,
            "-i",
            path,
            "-t",
            "0.6",
            "-map",
            "0:v:0",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        )
        frame_bytes = 160 * 90
        bright_frame = next(
            (
                index
                for index in range(len(pixels) // frame_bytes)
                if pixels[index * frame_bytes + frame_bytes // 2] > 200
            ),
            None,
        )
        assert bright_frame is not None, f"No video marker at {marker}s"
        audio_error = start + onset - marker
        video_error = start + bright_frame / 12 - marker
        assert abs(audio_error) < 0.01, (marker, audio_error)
        assert abs(video_error) <= 1 / 12, (marker, video_error)
        results.append(
            {
                "source_marker_seconds": marker,
                "audio_error_ms": round(audio_error * 1000, 3),
                "video_error_ms": round(video_error * 1000, 3),
            }
        )
    return results


def run(seconds: int, directory: Path):
    paths = AppPaths(*(directory / name for name in ("projects", "proxies", "jobs", "logs")))
    paths.ensure()
    source = directory / "pulse source.mov"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:size=160x90:rate=12:duration=300",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.12*sin(2*PI*880*t)*lt(mod(t\\,5)\\,1/12):s=44100:d=300",
        "-vf",
        "drawbox=color=white:t=fill:enable='lt(mod(t,5),1/12)'",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "2",
        "-c:a",
        "pcm_s24le",
        source,
    )
    overlay = directory / "overlay.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=1100:duration=7", "-c:a", "pcm_s24le", overlay)
    base = add_video(
        Project(output=OutputSettings(width=160, height=90, frame_rate="12/1", profile="h264")),
        source,
    )
    base = set_overlay(base, overlay)
    base = change_project(
        base,
        overlay=base.overlay.model_copy(
            update={
                "audio": AudioSettings(gain_db=-40),
                "end_behavior": "loop",
            }
        ),
    )

    def composition(duration):
        clips = tuple(
            Clip(source_id=base.sources[0].id, out_point=min(300, duration - i))
            for i in range(0, duration, 300)
        )
        return change_project(base, clips=clips)

    reports = []
    for duration in (60, seconds):
        project = composition(duration)
        save_project(project, paths.projects / f"{duration}.json")
        sampler = MemorySampler()
        sampler.thread.start()
        started = time.monotonic()
        last = [-1]

        def progress(update):
            percent = int(update.fraction * 100)
            if percent // 10 > last[0]:
                print(f"{duration}s composition: {percent}% ({update.stage})", flush=True)
                last[0] = percent // 10

        try:
            result = export_project(
                project, directory / f"{duration}.mp4", paths, progress=progress
            )
        finally:
            memory = sampler.result()
        metadata = probe_media(result.path)
        assert math.isclose(metadata.video.duration, duration, abs_tol=0.001)
        assert math.isclose(metadata.audio[0].duration, duration, abs_tol=0.025)
        reports.append(
            {
                "duration_seconds": duration,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "memory": memory,
                "markers": inspect_markers(result.path, duration),
                "output": str(result.path),
                "job": str(result.job),
            }
        )

    cancel = threading.Event()
    destination = directory / "cancel-guard.mp4"
    sentinel = b"existing output must survive cancellation"
    destination.write_bytes(sentinel)
    cancelled_at = []
    active_encoder = []
    stop_observer = threading.Event()

    def observe_encoder():
        while not stop_observer.wait(0.01):
            for child in psutil.Process().children():
                try:
                    if child.name() == "ffmpeg" and child.is_running():
                        active_encoder.append(child.pid)
                        cancelled_at.append(time.monotonic())
                        cancel.set()
                        return
                except psutil.NoSuchProcess:
                    pass

    observer = threading.Thread(target=observe_encoder, daemon=True)
    observer.start()
    try:
        export_project(
            composition(seconds),
            destination,
            paths,
            overwrite=True,
            cancel=cancel,
        )
    except RenderCancelled:
        assert destination.read_bytes() == sentinel
    else:
        raise AssertionError("Cancellation did not stop the export")
    finally:
        stop_observer.set()
        observer.join()
    assert active_encoder, "Cancellation was not exercised during active FFmpeg work"
    latency = time.monotonic() - cancelled_at[0]
    assert latency < 5, latency
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "ffmpeg": ffmpeg("-version").decode().splitlines()[0],
        "runs": reports,
        "cancel_latency_seconds": round(latency, 3),
        "cancelled_active_encoder": True,
        "scope": "Low-resolution duration scaling; sampled RSS is not a peak-memory guarantee.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=7200)
    args = parser.parse_args()
    if args.seconds < 60 or args.seconds % 5:
        parser.error("--seconds must be at least 60 and a multiple of five")
    directory = Path(tempfile.mkdtemp(prefix="stitch-endurance-"))
    print(f"Artifacts: {directory}", flush=True)
    report = run(args.seconds, directory)
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
