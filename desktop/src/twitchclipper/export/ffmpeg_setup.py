"""Optional download of a pinned FFmpeg build (only when the user asks for it).

The SHA256 below is hard-coded in the app and checked before anything is extracted.
It is the value gyan.dev published for this exact file (read on 2026-11-20).
RELEASE TASK: re-confirm it by downloading the file independently before shipping.

License: gyan.dev builds are GPLv3 (they include libx264/libx265). TwitchClipper runs
ffmpeg as a separate program and does not bundle it. Source: https://ffmpeg.org/releases/
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx


@dataclass(frozen=True)
class PinnedBuild:
    version: str
    url: str
    sha256: str
    size_bytes: int
    license: str
    source_url: str
    build_info_url: str


PINNED = PinnedBuild(
    version="9.0.2",
    url="https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip",
    sha256="60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
    size_bytes=114_768_076,
    license="GPLv3 (gyan.dev 'essentials' build; includes libx264/libx265)",
    source_url="https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
    build_info_url="https://www.gyan.dev/ffmpeg/builds/",
)

LICENSE_NOTICE = (
    "FFmpeg is a separate program licensed under the GNU GPL v3 for this build "
    "(it includes libx264 and libx265). TwitchClipper does not bundle FFmpeg; this optional "
    "download places it in your user folder. Source code for this version: {source}. "
    "Build information: {info}."
)


class InstallError(Exception):
    pass


def install(dest_root: Path, build: PinnedBuild = PINNED,
            progress: Callable[[int, int], None] | None = None,
            transport: httpx.BaseTransport | None = None) -> Path:
    """Download, verify SHA256, extract. Returns the path of ffmpeg.exe."""
    target = dest_root / build.version
    exe = target / "bin" / "ffmpeg.exe"
    if exe.is_file():
        return exe
    dest_root.mkdir(parents=True, exist_ok=True)
    archive = dest_root / f"ffmpeg-{build.version}.zip.part"
    h = hashlib.sha256()
    done = 0
    try:
        with httpx.Client(timeout=60, follow_redirects=True, transport=transport) as c:
            with c.stream("GET", build.url) as r:
                if r.status_code != 200:
                    raise InstallError(f"download failed: HTTP {r.status_code}")
                total = int(r.headers.get("content-length", build.size_bytes))
                with open(archive, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        if h.hexdigest().lower() != build.sha256.lower():
            raise InstallError("checksum mismatch: the download does not match the pinned build and was deleted")
        tmp_dir = dest_root / f"{build.version}.extracting"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                resolved = (tmp_dir / member).resolve()
                if not str(resolved).startswith(str(tmp_dir.resolve())):
                    raise InstallError("archive contains unsafe paths")
            z.extractall(tmp_dir)
        found = next(tmp_dir.rglob("ffmpeg.exe"), None)
        if not found:
            raise InstallError("ffmpeg.exe not found in the archive")
        root = found.parent.parent  # .../<build>/bin/ffmpeg.exe -> .../<build>
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(root), str(target))
        shutil.rmtree(tmp_dir, ignore_errors=True)
        (target / "TWITCHCLIPPER_LICENSE_NOTE.txt").write_text(
            LICENSE_NOTICE.format(source=build.source_url, info=build.build_info_url), encoding="utf-8")
        return exe
    finally:
        archive.unlink(missing_ok=True)
