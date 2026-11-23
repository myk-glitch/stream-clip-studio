"""Clip library: import an existing local video file into the Results page.

Importing always *copies* the file into `<clips folder>/imported/`. The library entry points at
the copy, so deleting it from the Results page can never remove the original file.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..export import ffmpeg as ft
from ..storage.db import Database

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".ts", ".m4v", ".avi", ".flv")
IMPORT_SUBDIR = "imported"
_CHUNK = 4 * 1024 * 1024


@dataclass
class ImportResult:
    clip_id: int
    path: Path          # the copy inside the clips folder
    original: Path
    width: int
    height: int
    duration_s: float
    has_audio: bool


def norm_path(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def same_path(a: Path | str | None, b: Path | str | None) -> bool:
    """True if both name the same file (case-insensitive on Windows; also catches other spellings of an existing file)."""
    if not a or not b:
        return False
    if norm_path(a) == norm_path(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def unique_destination(folder: Path, name: str, reserved: set[str] | None = None) -> Path:
    """First free name: `name`, `stem (2).ext`, … . Names in `reserved` (norm_path) count as taken,
    so jobs queued before their files exist never get the same output path."""
    reserved = reserved if reserved is not None else set()
    dest = folder / name
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while dest.exists() or norm_path(dest) in reserved:
        dest = folder / f"{stem} ({n}){suffix}"
        n += 1
    reserved.add(norm_path(dest))
    return dest


@dataclass
class DeletePlan:
    path: Path
    delete_file: bool   # False: only remove the library entry
    reason: str = ""    # why the file is kept (empty when it will be deleted)


def plan_delete(db: Database, clip_id: int) -> DeletePlan:
    """Decide whether deleting a library entry may also delete its file.

    The file is kept (entry removed only) when it is missing, not a regular file, used by another
    library entry, the original that an import was copied from, or the source video/chat of an analysis.
    """
    row = db.clip(clip_id)
    if row is None:
        raise KeyError(clip_id)
    p = Path(row["path"])
    if not p.exists():
        return DeletePlan(p, False, "the file is already missing from disk")
    if not p.is_file():
        return DeletePlan(p, False, "it isn't a regular file")
    others = db.query("SELECT path, original_path FROM clips WHERE id != ?", (clip_id,))
    if any(same_path(r["path"], p) for r in others):
        return DeletePlan(p, False, "another entry in the list uses the same file")
    if same_path(row["original_path"], p) or any(same_path(r["original_path"], p) for r in others):
        return DeletePlan(p, False, "it's the original file an imported clip was copied from")
    for a in db.query("SELECT video_path, chat_path FROM analyses"):
        if same_path(a["video_path"], p) or same_path(a["chat_path"], p):
            return DeletePlan(p, False, "it's the source video of an analysis")
    return DeletePlan(p, True)


def _copy(src: Path, dest: Path, progress: Callable[[float, str], None], cancelled: Callable[[], bool]) -> None:
    """Chunked copy to a .part file. Doesn't carry over the read-only attribute, so the copy is deletable."""
    total = max(src.stat().st_size, 1)
    part = dest.with_name(dest.name + ".part")
    done = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while chunk := fi.read(_CHUNK):
                if cancelled():
                    raise InterruptedError("import cancelled")
                fo.write(chunk)
                done += len(chunk)
                progress(0.1 + 0.85 * done / total, f"Copying… {done / 1e6:.0f} / {total / 1e6:.0f} MB")
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)


def import_video(ffprobe: Path, src: Path | str, clips_dir: Path, db: Database,
                 progress: Callable[[float, str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> ImportResult:
    progress = progress or (lambda f, m: None)
    cancelled = cancelled or (lambda: False)
    src = Path(src).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")
    progress(0.02, "Checking the video…")
    try:
        data = ft.probe(ffprobe, src)
    except RuntimeError as exc:
        raise ValueError(f"{src.name} isn't a video FFmpeg can read: {exc}") from exc
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"{src.name} has no video stream.")
    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
    width, height = int(video["width"]), int(video["height"])
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)

    folder = clips_dir / IMPORT_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_destination(folder, src.name)
    _copy(src, dest, progress, cancelled)
    clip_id = db.add_clip(path=str(dest), orientation="portrait" if height > width else "landscape",
                          window_start=0.0, window_end=duration, duration_s=duration,
                          encoder=video.get("codec_name"), source="import", original_path=str(src))
    progress(1.0, f"Imported {dest.name}")
    return ImportResult(clip_id, dest, src, width, height, duration, has_audio)
