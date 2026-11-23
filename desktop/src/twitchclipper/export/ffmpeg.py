"""Locate ffmpeg, check its version and encoders, probe files, verify downloads."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MIN_VERSION = (7, 0)
HW_ENCODERS = ["h264_nvenc", "h264_qsv", "h264_amf"]
CPU_ENCODER = "libx264"
BELOW_NORMAL_PRIORITY = 0x00004000 if sys.platform == "win32" else 0


@dataclass
class FfmpegInstall:
    ffmpeg: Path
    ffprobe: Path | None
    source: str
    version_string: str = ""
    version: tuple[int, ...] = ()
    encoders: set[str] = field(default_factory=set)


def real_executable(path: Path) -> Path:
    """The ffmpeg behind a Chocolatey or Scoop shim. A shim runs ffmpeg as a child process, so
    stopping the shim can leave ffmpeg running and still holding the files it was writing."""
    root = path.parent.parent
    location = (root.name.lower(), path.parent.name.lower())
    if location == ("chocolatey", "bin"):
        pattern = root / "lib" / "ffmpeg*" / "tools" / "**" / path.name
    elif location == ("scoop", "shims"):
        pattern = root / "apps" / "ffmpeg*" / "current" / "bin" / path.name
    else:
        return path
    matches = sorted(glob.glob(str(pattern), recursive=True))
    return Path(matches[0]) if matches else path


def _candidates() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    env = os.environ.get("TWITCHCLIPPER_FFMPEG")
    if env:
        found.append((Path(env), "env:TWITCHCLIPPER_FFMPEG"))
    which = shutil.which("ffmpeg")
    if which:
        found.append((Path(which), "PATH"))
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        (os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"), "winget"),
        (os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"), "winget-link"),
        (r"C:\ProgramData\chocolatey\bin\ffmpeg.exe", "chocolatey"),
        (os.path.join(os.path.expanduser("~"), "scoop", "shims", "ffmpeg.exe"), "scoop"),
        (os.path.join(local, "TwitchClipper", "ffmpeg", "*", "bin", "ffmpeg.exe"), "app-download"),
    ]
    for pattern, source in patterns:
        for match in sorted(glob.glob(pattern)):
            found.append((Path(match), source))
    return found


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"ffmpeg version (?:n)?(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return ()
    return tuple(int(x) for x in m.groups() if x is not None)


def locate() -> list[FfmpegInstall]:
    """Return every usable ffmpeg found, in priority order (deduplicated)."""
    seen: set[str] = set()
    installs: list[FfmpegInstall] = []
    for path, source in _candidates():
        path = real_executable(path)
        if not path.is_file():
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            out = subprocess.run([str(path), "-hide_banner", "-version"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        first = out.stdout.splitlines()[0] if out.stdout else ""
        probe = path.with_name("ffprobe.exe" if path.suffix.lower() == ".exe" else "ffprobe")
        installs.append(
            FfmpegInstall(
                ffmpeg=path,
                ffprobe=probe if probe.is_file() else None,
                source=source,
                version_string=first,
                version=parse_version(first),
            )
        )
    return installs


def list_encoders(ffmpeg: Path) -> set[str]:
    out = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    names = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


@dataclass
class EncoderTest:
    name: str
    listed: bool
    works: bool
    seconds: float
    error: str = ""


def test_encoder(ffmpeg: Path, name: str, listed: bool) -> EncoderTest:
    """Really encode 1 s of a generated test pattern; being listed is not enough."""
    if not listed:
        return EncoderTest(name, False, False, 0.0, "not compiled into this ffmpeg build")
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "1",
        "-c:v", name, "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    t0 = time.perf_counter()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return EncoderTest(name, True, False, 60.0, "timed out")
    dt = time.perf_counter() - t0
    err = (out.stderr or "").strip().splitlines()
    return EncoderTest(name, True, out.returncode == 0, dt, err[-1] if err and out.returncode else "")


def choose_encoder(results: list[EncoderTest]) -> str:
    for name in HW_ENCODERS:
        for r in results:
            if r.name == name and r.works:
                return name
    return CPU_ENCODER


def encoder_args(name: str) -> list[str]:
    """Reasonable quality/speed settings per encoder."""
    if name == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    if name == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
    if name == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", "23"]
    if name == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"]
    return ["-c:v", name]


def probe(ffprobe: Path, path: Path) -> dict:
    out = subprocess.run(
        [str(ffprobe), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


def video_info(ffprobe: Path, path: Path) -> dict:
    data = probe(ffprobe, path)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    num, den = (int(x) for x in v.get("avg_frame_rate", "0/1").split("/"))
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": num / den if den else 0.0,
        "duration": float(data["format"].get("duration", 0.0)),
        "codec": v.get("codec_name"),
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    return sha256_file(path).lower() == expected.strip().lower()


def install_from(path: Path, source: str = "settings") -> FfmpegInstall | None:
    """Inspect one ffmpeg executable (e.g. the path chosen in Settings)."""
    path = Path(path)
    if path.is_dir():
        path = path / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    path = real_executable(path)
    if not path.is_file():
        return None
    try:
        out = subprocess.run([str(path), "-hide_banner", "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = out.stdout.splitlines()[0] if out.stdout else ""
    probe = path.with_name("ffprobe" + path.suffix)
    return FfmpegInstall(path, probe if probe.is_file() else None, source, first, parse_version(first))


def usable(inst: FfmpegInstall | None) -> bool:
    return bool(inst and inst.version and inst.version >= MIN_VERSION and inst.ffprobe)


def best_install(override: str | Path | None = None) -> FfmpegInstall | None:
    if override:
        inst = install_from(Path(override))
        if usable(inst):
            return inst
    for inst in locate():
        if usable(inst):
            return inst
    return None
