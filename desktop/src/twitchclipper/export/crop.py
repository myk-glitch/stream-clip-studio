"""Portrait (9:16) output from a landscape clip, used when Twitch returns no portrait URL."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .ffmpeg import BELOW_NORMAL_PRIORITY


def portrait_filter(center_x_frac: float = 0.5, out_w: int = 1080, out_h: int = 1920) -> str:
    """Full-height 9:16 crop, horizontally centred on center_x_frac (0 = left edge, 1 = right)."""
    c = min(1.0, max(0.0, center_x_frac))
    return (
        f"crop=w=trunc(ih*9/16/2)*2:h=ih:"
        f"x='max(0,min(iw-ow,iw*{c:.4f}-ow/2))':y=0,"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    )


def crop_to_portrait(ffmpeg: Path, src: Path, out: Path, center_x_frac: float = 0.5,
                     out_w: int = 1080, out_h: int = 1920, encoder: list[str] | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
        "-vf", portrait_filter(center_x_frac, out_w, out_h),
        *(encoder or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]),
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=BELOW_NORMAL_PRIORITY)
    if r.returncode != 0:
        raise RuntimeError(f"portrait crop failed: {r.stderr.strip()}")
