"""Dashboard: status at a glance, recent clips, quick actions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ...core.context import AppContext
from ..widgets.common import StatCard, notice, page_header
from .results import SystemLauncher


class DashboardPage(QWidget):
    def __init__(self, ctx: AppContext, navigate: Callable[[str], None], parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.launcher = SystemLauncher()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(page_header("Dashboard", "Everything runs locally on this PC. Twitch sign-in is optional."))
        self.warn = notice("")
        self.warn.hide()
        root.addWidget(self.warn)

        self.card_grid = grid = QGridLayout()
        grid.setSpacing(12)
        self.card_ffmpeg = StatCard("FFmpeg")
        self.card_encoder = StatCard("Encoder")
        self.card_account = StatCard("Twitch account")
        self.card_clips = StatCard("Clips")
        self.card_disk = StatCard("Free space")
        self.cards = [self.card_ffmpeg, self.card_encoder, self.card_account, self.card_clips, self.card_disk]
        self.card_columns = 0
        self._arrange_cards(len(self.cards))
        root.addLayout(grid)

        actions = QHBoxLayout()
        b1 = QPushButton("Analyze a local video")
        b1.setObjectName("Primary")
        b1.clicked.connect(lambda: navigate("analyze"))
        b2 = QPushButton("Open clips folder")
        b2.clicked.connect(self.open_clips_folder)
        b3 = QPushButton("Live Monitor")
        b3.clicked.connect(lambda: navigate("live"))
        for b in (b1, b2, b3):
            actions.addWidget(b)
        actions.addStretch(1)
        root.addLayout(actions)

        box = QGroupBox("Recent clips")
        bl = QVBoxLayout(box)
        self.recent = QListWidget()
        self.recent.itemDoubleClicked.connect(lambda _: navigate("results"))
        bl.addWidget(self.recent)
        root.addWidget(box, 1)

        for sig in (ctx.events.clips_changed, ctx.events.ffmpeg_changed, ctx.events.settings_changed,
                    ctx.events.account_changed):
            sig.connect(self.refresh)
        self.refresh()

    # five cards in one row need ~700 px; on narrow windows use two rows (3 + 2) instead of scrolling sideways
    WIDE_MIN_WIDTH = 740

    def _arrange_cards(self, columns: int) -> None:
        if columns == self.card_columns:
            return
        for c in self.cards:
            self.card_grid.removeWidget(c)
        for i in range(max(self.card_columns, columns)):
            self.card_grid.setColumnStretch(i, 1 if i < columns else 0)
        for i, c in enumerate(self.cards):
            self.card_grid.addWidget(c, i // columns, i % columns)
        self.card_columns = columns

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange_cards(len(self.cards) if self.width() >= self.WIDE_MIN_WIDTH else 3)

    def refresh(self) -> None:
        ctx = self.ctx
        if ctx.ffmpeg:
            v = ".".join(map(str, ctx.ffmpeg.version)) or "?"
            src = {"PATH": "Found on PATH", "winget": "Installed with winget", "settings": "Location from Settings",
                   "app-download": "Downloaded by TwitchClipper"}.get(ctx.ffmpeg.source, ctx.ffmpeg.source)
            self.card_ffmpeg.set(f"Ready ({v})", src, "ok")
            self.card_ffmpeg.setToolTip(str(ctx.ffmpeg.ffmpeg))
        else:
            self.card_ffmpeg.set("Not found", "Install FFmpeg or set its path in Settings", "bad")
        if ctx.encoders:
            enc, note = ctx.encoder_for_export()
            hw = enc != "libx264"
            self.card_encoder.set(enc, ("Hardware encoding" if hw else "CPU encoding") + (f" · {note}" if note not in ("auto", "user choice") else ""),
                                  "ok" if hw else None)
        elif ctx.ffmpeg:
            self.card_encoder.set("Testing…", "Checking which encoders work on this PC")
        else:
            self.card_encoder.set("-", "Needs FFmpeg")
        if ctx.account_login:
            self.card_account.set(ctx.account_login, "Signed in", "ok")
        else:
            self.card_account.set("Not signed in", "Only needed for live chat and Twitch clips")
        stats = ctx.db.stats()
        self.card_clips.set(str(stats["clips"]), f"{stats['analyses']} analyses · {stats['channels']} channels")
        free = ctx.disk_free_gb()
        drive = ctx.clips_dir.drive or str(ctx.clips_dir.anchor)
        self.card_disk.set(f"{free:.1f} GB", f"on {drive} (clips folder)", "bad" if free < 2 else ("warn" if free < 10 else None))
        self.card_disk.setToolTip(str(ctx.clips_dir))
        self.recent.clear()
        for c in ctx.db.clips(limit=12):
            when = datetime.fromtimestamp(c["created_at"]).strftime("%b %d %H:%M")
            self.recent.addItem(f"{when}  ·  {Path(c['path']).name}  ·  {c['orientation']}  ·  {c['duration_s'] or 0:.0f} s")
        if not self.recent.count():
            self.recent.addItem("No clips yet. Use Analyze to make your first ones.")
        if ctx.settings_warnings:
            self.warn.setText("Settings adjusted: " + "; ".join(ctx.settings_warnings))
            self.warn.show()

    def open_clips_folder(self) -> None:
        d = self.ctx.clips_dir
        try:
            d.mkdir(parents=True, exist_ok=True)
            self.launcher.open(d)
        except OSError as exc:   # e.g. the clips folder is on a drive that isn't connected
            self.warn("Couldn't open the clips folder",
                      f"{d}\n\n{exc.strerror or exc}\n\nYou can choose another folder in Settings → Export & encoding.")

    def warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)
