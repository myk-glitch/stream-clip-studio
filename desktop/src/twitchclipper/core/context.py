"""Application-wide state shared by the UI pages."""

from __future__ import annotations

import logging
import shutil
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..config import settings as cfg
from ..export import ffmpeg as ft
from ..export.hwaccel import EncoderReport, cached_or_detect
from ..pipeline.library import norm_path, same_path
from ..storage.db import Database
from ..storage.paths import AppPaths, app_paths

log = logging.getLogger("twitchclipper")


class AppEvents(QObject):
    settings_changed = Signal()
    clips_changed = Signal()
    ffmpeg_changed = Signal()
    account_changed = Signal()


@dataclass
class AppContext:
    paths: AppPaths
    settings: cfg.Settings
    db: Database
    settings_warnings: list[str] = field(default_factory=list)
    ffmpeg: ft.FfmpegInstall | None = None
    encoders: EncoderReport | None = None
    events: AppEvents = field(default_factory=AppEvents)
    account_login: str | None = None
    _busy: Counter = field(default_factory=Counter, repr=False)
    _busy_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- files used by background jobs ----------------------------------------
    @contextmanager
    def using_file(self, path: Path | str):
        """Mark a file as in use by a job (e.g. an export reading it) for the duration of the block."""
        key = norm_path(path)
        with self._busy_lock:
            self._busy[key] += 1
        try:
            yield
        finally:
            with self._busy_lock:
                self._busy[key] -= 1
                if self._busy[key] <= 0:
                    del self._busy[key]

    def file_in_use(self, path: Path | str) -> bool:
        with self._busy_lock:
            keys = list(self._busy)
        return any(same_path(k, path) for k in keys)

    # --- settings ----------------------------------------------------------
    def save_settings(self) -> list[str]:
        warnings = self.settings.validate()
        cfg.save(self.settings, self.paths.settings_file)
        self.events.settings_changed.emit()
        return warnings

    @property
    def clips_dir(self) -> Path:
        return Path(self.settings.export.output_dir) if self.settings.export.output_dir else self.paths.default_clips_dir

    # --- ffmpeg / encoders --------------------------------------------------
    def refresh_ffmpeg(self) -> ft.FfmpegInstall | None:
        self.ffmpeg = ft.best_install(self.settings.ffmpeg.path_override or None)
        self.events.ffmpeg_changed.emit()
        return self.ffmpeg

    def detect_encoders(self, force: bool = False) -> EncoderReport | None:
        """Slow (test encodes): call from a worker thread."""
        if not self.ffmpeg:
            self.encoders = None
            return None
        self.encoders = cached_or_detect(self.db, self.ffmpeg, force=force)
        return self.encoders

    def encoder_for_export(self) -> tuple[str, str]:
        if not self.encoders:
            return ft.CPU_ENCODER, "encoders not detected yet; using CPU"
        return self.encoders.resolve(self.settings.export.encoder)

    def disk_free_gb(self) -> float:
        target = self.clips_dir
        probe = target if target.exists() else target.anchor or Path.home()
        try:
            return shutil.disk_usage(probe).free / 1e9
        except OSError:
            return float("nan")


def setup_logging(paths: AppPaths) -> None:
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(paths.logs_dir / "twitchclipper.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("twitchclipper")
    if not root.handlers:
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def create_context(paths: AppPaths | None = None) -> AppContext:
    paths = (paths or app_paths()).ensure()
    setup_logging(paths)
    settings, warnings = cfg.load(paths.settings_file)
    db = Database(paths.database)
    ctx = AppContext(paths, settings, db, warnings)
    ctx.account_login = db.kv_get("account_login")
    ctx.refresh_ffmpeg()
    log.info("started; ffmpeg=%s", ctx.ffmpeg.ffmpeg if ctx.ffmpeg else None)
    return ctx
