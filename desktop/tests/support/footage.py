"""Generate local test footage with a burned-in clock strip, and decode it back.

The clock value in each frame is `base_ms + frame_index * 1000 / fps`, so any
clip cut from this footage can be checked against the exact time range requested.
Keyframes every 2 s match Twitch's ingest requirement.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .timecode import decode_strip, draw_strip


def generate(
    ffmpeg: Path,
    out: Path,
    duration_s: float,
    fps: int = 30,
    width: int = 640,
    height: int = 360,
    base_ms: int = 0,
    keyframe_s: float = 2.0,
) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    gop = int(round(fps * keyframe_s))
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-f", "lavfi", "-t", str(duration_s), "-i", "sine=frequency=440:sample_rate=48000",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "96k", "-shortest", str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    total = int(round(duration_s * fps))
    yy, xx = np.mgrid[0:height, 0:width]
    background = np.stack(
        [(xx * 255 // width), (yy * 255 // height), np.full_like(xx, 96)], axis=2
    ).astype(np.uint8)
    box = max(16, height // 8)
    try:
        for i in range(total):
            frame = background.copy()
            x = int((i * 4) % max(1, width - box))
            y = height // 2
            frame[y : y + box, x : x + box] = (255, 255, 255)
            draw_strip(frame, base_ms + int(round(i * 1000 / fps)))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed generating footage: {err.strip()}")
    return out


@dataclass
class DecodeResult:
    frames: int
    decoded: int
    first_ms: int | None
    last_ms: int | None
    monotonic: bool

    @property
    def decode_rate(self) -> float:
        return self.decoded / self.frames if self.frames else 0.0


def decode_file(ffmpeg: Path, path: Path, scale_width: int = 640) -> DecodeResult:
    """Decode the clock strip from every frame of a video file."""
    # Probe output size after scaling by asking ffmpeg for one frame first is costly;
    # instead force an exact even size from the source aspect via scale=-2.
    probe_cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={scale_width}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    first = subprocess.run(probe_cmd, capture_output=True, timeout=60)
    if first.returncode != 0 or not first.stdout:
        raise RuntimeError(f"cannot decode {path}: {first.stderr.decode(errors='replace').strip()}")
    height = len(first.stdout) // (scale_width * 3)
    frame_bytes = scale_width * height * 3

    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
        "-vf", f"scale={scale_width}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    frames = decoded = 0
    first_ms = last_ms = prev = None
    monotonic = True
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        frames += 1
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(height, scale_width, 3)
        ms = decode_strip(frame)
        if ms is None:
            continue
        decoded += 1
        if first_ms is None:
            first_ms = ms
        if prev is not None and ms < prev:
            monotonic = False
        prev = last_ms = ms
    proc.wait()
    return DecodeResult(frames, decoded, first_ms, last_ms, monotonic)
