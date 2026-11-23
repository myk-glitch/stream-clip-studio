"""Generate the synthetic test clip in test_media/ (no Twitch content, no downloads).

    uv run python scripts/make_test_media.py [--force]

The output is a 12 s, 1280x720, 30 fps H.264 + AAC MP4:
  - video: FFmpeg's testsrc2 colour pattern with a burned-in running timestamp and frame number
  - audio: a 440 Hz tone with a short 1760 Hz beep at the start of every second
The file is marked read-only so it isn't deleted by accident; the app imports a *copy* of it.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from twitchclipper.export import ffmpeg as ft  # noqa: E402

OUT_DIR = ROOT / "test_media"
FIXTURE = OUT_DIR / "twitchclipper_test_pattern_12s.mp4"
DURATION = 12
FONT = "C\\:/Windows/Fonts/consola.ttf"


def build_command(ffmpeg: Path, out: Path) -> list[str]:
    text = (
        f"drawtext=fontfile='{FONT}':text='TwitchClipper TEST CLIP  %{{pts\\:hms}}':"
        "x=(w-tw)/2:y=40:fontsize=48:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=12,"
        f"drawtext=fontfile='{FONT}':text='frame %{{n}}   synthetic fixture - safe to play':"
        "x=(w-tw)/2:y=h-90:fontsize=32:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=10"
    )
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:beep_factor=4:sample_rate=48000:duration={DURATION}",
        "-vf", text, "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-shortest", "-movflags", "+faststart",
        "-metadata", "title=TwitchClipper synthetic test clip", str(out),
    ]


def make(force: bool = False) -> Path:
    inst = ft.best_install()
    if not inst:
        raise SystemExit("FFmpeg not found. Install it first (winget install Gyan.FFmpeg).")
    OUT_DIR.mkdir(exist_ok=True)
    if FIXTURE.exists():
        if not force:
            print(f"exists, keeping it: {FIXTURE}  (use --force to regenerate)")
            return FIXTURE
        os.chmod(FIXTURE, stat.S_IWRITE | stat.S_IREAD)
    tmp = FIXTURE.with_suffix(".part.mp4")
    subprocess.run(build_command(inst.ffmpeg, tmp), check=True)
    tmp.replace(FIXTURE)
    os.chmod(FIXTURE, stat.S_IREAD)   # read-only guard
    info = ft.video_info(inst.ffprobe, FIXTURE)
    print(f"wrote {FIXTURE} ({info['width']}x{info['height']}, {info['fps']:.0f} fps, {info['duration']:.2f} s, "
          f"{FIXTURE.stat().st_size / 1e6:.2f} MB)")
    return FIXTURE


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="regenerate even if the fixture exists")
    make(ap.parse_args().force)
