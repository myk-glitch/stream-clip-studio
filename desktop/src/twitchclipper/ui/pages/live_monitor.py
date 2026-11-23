"""Live Monitor page.

Tab 1, "Local capture (Mode D)": choose a capture source, start/stop the rolling buffer, and
"Clip that" to save the moments around a button press (engine/capture.py does the work).
Tab 2, "Channels": the Phase 1 channel list (stored in SQLite), per-channel relationship, mode
overview, current clip-limit and approval policy, and the chat graph. The live chat engine
(EventSub + detection + clip requests) is scheduled for Phase 3; its Start button stays disabled
with an explanation until then.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout, QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ...config import settings as cfg
from ...core.context import AppContext
from ...engine.capture import CaptureController
from ...limits.policy import LimitPolicy
from ...sources import capture as cap
from ..widgets.chat_graph import ChatGraph
from ..widgets.common import Badge, StatusLabel, hbox, notice, page_header

MODES = [
    ("A", "Twitch clip (any channel that allows clipping)", "Sign-in + clips:edit · public clip under your account · "
     "approximate length (ends ~5 s after the request) · manual approval on other channels"),
    ("B", "Twitch clip + download (your channel / editor)", "Sign-in + clips:edit + clip manager scope · "
     "downloads the clip, checks it covers the 27 s window, trims locally"),
    ("C", "OBS Replay Buffer (your own stream, optional)", "OBS 28+ with WebSocket enabled · exact 27 s clips"),
    ("D", "Local capture ring buffer", "A capture source you own · exact 27 s clips · available now on the "
     "“Local capture” tab"),
]

STATE_TEXT = {
    "idle": ("Not capturing", None), "starting": ("Starting…", None), "recording": ("● Recording", True),
    "restarting": ("Reconnecting… (FFmpeg restarted)", None), "stopping": ("Stopping…", None),
    "stopped": ("Stopped", None), "failed": ("Capture failed", False), "ended": ("Source ended", False),
}


class CompactTabs(QTabWidget):
    """QTabWidget reports its pages' *preferred* height for a width, which made the page scroll
    although every tab fits. Report the tallest tab's minimum height for that width instead."""

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        inner = max(1, width - 8)
        need = 0
        for i in range(self.count()):
            w = self.widget(i)
            lay = w.layout()
            h = lay.totalMinimumHeightForWidth(inner) if lay is not None and lay.hasHeightForWidth() else -1
            need = max(need, h if h > 0 else w.minimumSizeHint().height())
        return need + self.tabBar().sizeHint().height() + 8

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setHeight(self.heightForWidth(max(hint.width(), self.width())))
        return hint


def fmt_dur(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


class LiveMonitorPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, confirm=None,
                 controller: CaptureController | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.confirm = confirm or (lambda text: QMessageBox.question(self, "Remove channel", text)
                                   == QMessageBox.StandardButton.Yes)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(page_header("Live Monitor", "Capture a local source and clip moments with a button (Mode D). "
                                   "Watching Twitch chat arrives in Phase 3."))
        self.tabs = CompactTabs()
        root.addWidget(self.tabs, 1)
        self.capture = controller or CaptureController(ctx, self)
        self._build_capture_tab()
        self._build_channels_tab()

        for sig in (ctx.events.settings_changed, ctx.events.account_changed, ctx.events.ffmpeg_changed):
            sig.connect(self.refresh)
        self._listed_once = False
        self.refresh()

    # ===== Local capture (Mode D) ===================================================
    def _build_capture_tab(self) -> None:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.addWidget(notice(
            "Record a camera, capture card, OBS Virtual Camera or your screen into a rolling buffer on this PC. "
            "Press <b>Clip that</b> to save the moments around the press: it keeps the footage from before "
            "and records a few seconds more. No Twitch sign-in needed; nothing is uploaded."))

        src_box = QGroupBox("Capture source")
        sl = QVBoxLayout(src_box)
        self.source_combo = QComboBox()
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(22)
        self.source_combo.currentIndexChanged.connect(self._update_capture_controls)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Look for capture devices again (e.g. after plugging one in)")
        self.refresh_btn.clicked.connect(self.refresh_sources)
        self.audio_combo = QComboBox()
        self.audio_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.audio_combo.setMinimumContentsLength(22)
        self.audio_combo.currentIndexChanged.connect(self._update_capture_controls)
        video_row = QHBoxLayout()
        video_row.addWidget(QLabel("Video"))
        video_row.addWidget(self.source_combo, 1)
        video_row.addWidget(self.refresh_btn)
        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("Audio"))
        audio_row.addWidget(self.audio_combo, 1)
        sl.addLayout(video_row)
        sl.addLayout(audio_row)
        self.sources_info = StatusLabel("Looking for capture sources…")
        sl.addWidget(self.sources_info)

        rec_box = QGroupBox("Recording")
        rl = QVBoxLayout(rec_box)
        self.state_badge = Badge("Not capturing", None)
        self.buffer_lbl = QLabel("")
        self.buffer_lbl.setObjectName("Muted")
        rl.addWidget(hbox(self.state_badge, self.buffer_lbl))
        self.selected_lbl = QLabel("Source: none selected")
        self.selected_lbl.setWordWrap(True)
        rl.addWidget(self.selected_lbl)
        self.start_capture_btn = QPushButton("Start capture")
        self.start_capture_btn.clicked.connect(self.start_capture)
        self.stop_capture_btn = QPushButton("Stop capture")
        self.stop_capture_btn.clicked.connect(self.stop_capture)
        self.clip_btn = QPushButton("Clip that")
        self.clip_btn.setObjectName("Primary")
        self.clip_btn.setMinimumHeight(44)
        self.clip_btn.setMinimumWidth(160)
        f = self.clip_btn.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        self.clip_btn.setFont(f)
        self.clip_btn.clicked.connect(self.clip_that)
        rl.addWidget(hbox(self.start_capture_btn, self.stop_capture_btn, self.clip_btn))
        self.window_lbl = QLabel("")
        self.window_lbl.setObjectName("Muted")
        self.window_lbl.setWordWrap(True)
        rl.addWidget(self.window_lbl)
        self.capture_msg = StatusLabel("")
        rl.addWidget(self.capture_msg)

        # side by side on wide windows, stacked on narrow ones (see resizeEvent)
        self.capture_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.capture_row.addWidget(src_box, 1)
        self.capture_row.addWidget(rec_box, 1)
        lay.addLayout(self.capture_row)

        clips_box = QGroupBox("Clips from this session")
        cl = QVBoxLayout(clips_box)
        self.clip_list = QListWidget()
        self.clip_list.setMinimumHeight(60)
        self.clip_list.itemDoubleClicked.connect(self._open_in_results)
        cl.addWidget(self.clip_list)
        hint = QLabel("Finished clips also appear on the Results page. Double-click one to go there.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        cl.addWidget(hint)
        lay.addWidget(clips_box, 1)
        self.tabs.addTab(tab, "Local capture (Mode D)")

        c = self.capture
        c.sources_changed.connect(self._sources_changed)
        c.state_changed.connect(self._state_changed)
        c.status_changed.connect(self._status_changed)
        c.clip_queued.connect(self._clip_queued)
        c.clip_finished.connect(self._clip_finished)
        c.clip_failed.connect(self._clip_failed)
        c.busy_changed.connect(lambda _: self._update_capture_controls())
        self._clip_items: dict[int, object] = {}
        self._missing_key: str | None = None
        self._state = "idle"
        self._attempted: cap.CaptureSource | None = None

    WIDE_MIN_WIDTH = 900

    def arrange_for_width(self, width: int) -> None:
        """Called by the page's scroll area with the visible width, before it measures the page.
        (Deciding from the page's own width can't work: the page is never narrower than its minimum,
        which depends on this very choice.)"""
        direction = (QBoxLayout.Direction.LeftToRight if width >= self.WIDE_MIN_WIDTH
                     else QBoxLayout.Direction.TopToBottom)
        if self.capture_row.direction() != direction:
            self.capture_row.setDirection(direction)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._listed_once:
            self._listed_once = True
            self.refresh_sources()

    def refresh_sources(self) -> None:
        self.sources_info.setText("Looking for capture sources…")
        self.refresh_btn.setEnabled(False)
        self.capture.refresh_sources()

    def _sources_changed(self, res: cap.SourceList) -> None:
        self.refresh_btn.setEnabled(True)
        want = self.source_combo.currentData() or self.ctx.settings.capture.source
        want_audio = self.audio_combo.currentData() if self.audio_combo.count() else self.ctx.settings.capture.audio
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for s in res.video:
            self.source_combo.addItem(s.label, s.key)
        self._missing_key = None
        if want and res.find(want) is None and want.startswith("dshow:"):
            # The device used last time isn't connected: keep it visible, but it can't be started.
            self._missing_key = want
            self.source_combo.addItem(f"{want.split(':', 1)[1]} (not connected)", want)
        idx = self.source_combo.findData(want) if want else -1
        if idx < 0:
            idx = next((i for i, s in enumerate(res.video) if s.kind == "dshow"), 0 if res.video else -1)
        self.source_combo.setCurrentIndex(idx)
        self.source_combo.blockSignals(False)

        self.audio_combo.clear()
        self.audio_combo.addItem("No audio", "")
        for a in res.audio:
            self.audio_combo.addItem(a.name, a.name)
        ai = self.audio_combo.findData(want_audio) if want_audio else 0
        self.audio_combo.setCurrentIndex(max(0, ai))

        notes = []
        if not res.video:
            notes.append("No supported capture source is available.")
        elif not res.devices or all(s.kind != "dshow" for s in res.video):
            notes.append("No cameras or capture devices were found. You can capture the screen, or try the built-in "
                         "test pattern. Plug in a device (or start OBS Virtual Camera) and press Refresh.")
        if res.dshow_error:
            notes.append(res.dshow_error)
        if self._missing_key:
            notes.append(f"The device you used last time, “{self._missing_key.split(':', 1)[1]}”, isn't connected.")
        if not res.audio:
            notes.append("No audio input devices were found, so clips will be silent (except the test pattern). "
                         "Sound playing on this PC can't be captured unless Windows offers a device for it, "
                         "such as “Stereo Mix”.")
        self.sources_info.setText(" ".join(notes))
        self._update_capture_controls()

    def selected_source(self) -> cap.CaptureSource | None:
        key = self.source_combo.currentData()
        return self.capture.sources.find(key) if key else None

    def selected_audio(self) -> cap.AudioDevice | None:
        name = self.audio_combo.currentData()
        return self.capture.sources.find_audio(name) if name else None

    def _update_capture_controls(self) -> None:
        c = self.capture
        src = self.selected_source()
        running = c.recording
        idle = not running and not c.busy
        self.start_capture_btn.setEnabled(idle and src is not None)
        self.stop_capture_btn.setEnabled(not c.busy and c.rec is not None
                                         and c.rec.state in ("recording", "restarting", "failed", "ended"))
        self.clip_btn.setEnabled(running and not c.busy)
        self.source_combo.setEnabled(idle)
        self.audio_combo.setEnabled(idle)
        self.refresh_btn.setEnabled(idle and not c._listing)
        if running and c.source is not None:
            shown = c.source.label + (f" + {c.audio.name}" if c.audio else "")
        elif src is not None:
            shown = src.label + (f" + {self.selected_audio().name}" if self.selected_audio() else "")
        elif self.source_combo.currentData() == self._missing_key and self._missing_key:
            shown = f"{self.source_combo.currentText()} — connect it and press Refresh"
        else:
            shown = "none selected"
        self.selected_lbl.setText(f"Source: {shown}")
        capt = self.ctx.settings.capture
        self.window_lbl.setText(f"Clip that saves {capt.clip_before_s:g} s before the press + {capt.clip_after_s:g} s "
                                f"after it ({capt.clip_before_s + capt.clip_after_s:g} s). Buffer: "
                                f"{fmt_dur(capt.buffer_s)}. Change these in Settings → Capture.")

    def start_capture(self) -> None:
        src, audio = self.selected_source(), self.selected_audio()
        self._attempted = src
        msg = self.capture.start(src, audio)
        if msg:
            self.capture_msg.setText(msg)
            return
        capt = self.ctx.settings.capture
        capt.source, capt.audio = src.key, audio.name if audio else ""
        try:
            cfg.save(self.ctx.settings, self.ctx.paths.settings_file)   # remember the choice (no page reloads)
        except OSError:
            pass
        self._update_capture_controls()

    def stop_capture(self) -> None:
        msg = self.capture.stop()
        if msg:
            self.capture_msg.setText(msg)
        self._update_capture_controls()

    def clip_that(self) -> None:
        ok, msg = self.capture.clip_that()
        if not ok:
            self.capture_msg.setText(msg)

    def _state_changed(self, state: str, msg: str) -> None:
        self._state = state
        text, ok = STATE_TEXT.get(state, (state, None))
        self.state_badge.set(text, ok)
        if state == "failed":
            extra = ""
            src = self.capture.source or self._attempted
            if src is not None and src.kind == "dshow":
                extra = " If the device was unplugged, plug it back in and press Refresh."
                self.capture.refresh_sources()
            self.capture_msg.setText((msg or "Capture stopped unexpectedly.") + extra)
        elif state == "restarting":
            self.capture_msg.setText(f"Recording hit a problem and is restarting: {msg}" if msg else
                                     "Recording hit a problem and is restarting.")
        elif state == "ended":
            self.capture_msg.setText("The source stopped sending video.")
        elif state in ("recording", "starting", "stopping"):
            self.capture_msg.setText(msg if state != "recording" else "")
        elif state == "stopped":
            self.capture_msg.setText("")
        self._update_capture_controls()

    def _status_changed(self, st: dict) -> None:
        if st["state"] in ("recording", "restarting"):
            buffered = st["buffered_s"]
            need = self.ctx.settings.capture.clip_before_s
            ready = "" if buffered >= need else f" · Clip that needs {need:g} s: ready in {fmt_dur(need - buffered)}"
            self.buffer_lbl.setText(f"Buffer {fmt_dur(buffered)} of {fmt_dur(st['buffer_s'])}{ready}")
        elif st["state"] == "idle":
            self.buffer_lbl.setText("")
        if st.get("warning"):
            self.capture_msg.setText(st["warning"])

    def _clip_queued(self, token: int, text: str) -> None:
        self.capture_msg.setText("")
        self.clip_list.insertItem(0, text)
        self._clip_items[token] = self.clip_list.item(0)

    def _clip_finished(self, token: int, clip_id: int, path: str) -> None:
        item = self._clip_items.get(token)
        text = f"Clip #{token}: saved {Path(path).name}"
        if item is not None:
            item.setText(text)
            item.setData(Qt.ItemDataRole.UserRole, clip_id)

    def _clip_failed(self, token: int, msg: str) -> None:
        item = self._clip_items.get(token)
        if item is not None:
            item.setText(f"Clip #{token}: not saved. {msg}")
        self.capture_msg.setText(msg)

    def _open_in_results(self, item) -> None:
        clip_id = item.data(Qt.ItemDataRole.UserRole)
        win = self.window()
        if clip_id and hasattr(win, "navigate"):
            win.navigate("results")
            res = getattr(win, "pages", {}).get("results")
            if res is not None:
                res.select_clip(clip_id)

    def shutdown(self) -> None:
        self.capture.shutdown()

    # ===== Channels (Phase 1 setup for Phase 3) ======================================
    def _build_channels_tab(self) -> None:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.addWidget(notice(
            "<b>Phase 1:</b> set up your channel list here. Live monitoring itself (reading chat through Twitch's "
            "official EventSub API and creating clips) arrives in Phase 3. "
            "It needs a Twitch sign-in on the Account page; local analysis on the Analyze page does not."))

        req = QHBoxLayout()
        self.badge_signin = Badge()
        self.badge_chat = Badge()
        self.badge_ffmpeg = Badge()
        for b in (self.badge_signin, self.badge_chat, self.badge_ffmpeg):
            req.addWidget(b)
        req.addStretch(1)
        root.addLayout(req)

        chans = QGroupBox("Channels")
        cl = QVBoxLayout(chans)
        self.login_edit = QLineEdit()
        self.login_edit.setPlaceholderText("Twitch login, e.g. yourchannel")
        self.login_edit.returnPressed.connect(self.add_channel)
        self.relation = QComboBox()
        self.relation.addItem("Someone else's channel", "other")
        self.relation.addItem("My channel", "own")
        self.relation.addItem("I'm an editor", "editor")
        add = QPushButton("Add")
        add.clicked.connect(self.add_channel)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        cl.addWidget(hbox(self.login_edit, self.relation, add, self.remove_btn, stretch_end=False))
        self.error = QLabel("")
        self.error.setObjectName("Muted")
        cl.addWidget(self.error)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Channel", "Relationship", "Clips allowed"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        cl.addWidget(self.table)
        self.limits_lbl = QLabel("")
        self.limits_lbl.setObjectName("Muted")
        self.limits_lbl.setWordWrap(True)
        cl.addWidget(self.limits_lbl)

        modes = QGroupBox("Clip modes")
        ml = QVBoxLayout(modes)
        for key, title, needs in MODES:
            lab = QLabel(f"<b>Mode {key}: {title}</b><br><span style='color:gray'>{needs}</span>")
            lab.setWordWrap(True)
            ml.addWidget(lab)
        ml.addStretch(1)

        row = QHBoxLayout()
        row.addWidget(chans, 3)
        row.addWidget(modes, 2)
        root.addLayout(row, 2)

        self.graph = ChatGraph()
        self.graph.setMinimumHeight(110)
        root.addWidget(self.graph, 1)
        self.start_btn = QPushButton("Start monitoring")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setEnabled(False)
        self.start_btn.setToolTip("Live monitoring arrives in Phase 3.")
        root.addWidget(hbox(self.start_btn, QLabel("Available in Phase 3")))
        self.tabs.addTab(tab, "Channels (Phase 3)")

    def refresh(self) -> None:
        ctx = self.ctx
        signed_in = bool(ctx.account_login)
        self.badge_signin.set("Signed in" if signed_in else "Not signed in", signed_in)
        self.badge_chat.set("Chat reading (user:read:chat)", signed_in or None)
        self.badge_ffmpeg.set("FFmpeg", ctx.ffmpeg is not None)
        pub = ctx.settings.publish
        rows = ctx.db.channels()
        self.table.setRowCount(len(rows))
        labels = {"own": "My channel", "editor": "Editor", "other": "Someone else's"}
        for r, ch in enumerate(rows):
            rel = ch["relation"]
            auto = {"own": pub.auto_create_own, "editor": pub.auto_create_editor, "other": pub.auto_create_other}[rel]
            for c, text in enumerate([ch["login"], labels.get(rel, rel),
                                      "Automatic" if auto else "After your approval"]):
                self.table.setItem(r, c, QTableWidgetItem(text))
        lim = ctx.settings.limits
        policy = ("stop creating clips (monitoring continues)" if lim.policy == LimitPolicy.STOP.value
                  else "replace the weakest clip when a stronger moment appears")
        self.limits_lbl.setText(f"Clip limit per stream: {lim.clip_limit}; when reached: {policy}. "
                                "Change these in Settings → Clips & limits.")
        self._update_capture_controls()

    def add_channel(self) -> None:
        login = self.login_edit.text().strip()
        try:
            self.ctx.db.add_channel(login, self.relation.currentData())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.error.setText("")
        self.login_edit.clear()
        self.refresh()

    def remove_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        login = self.table.item(rows[0].row(), 0).text()
        if self.confirm(f"Remove {login} from your channel list?"):
            self.ctx.db.remove_channel(login)
            self.refresh()
