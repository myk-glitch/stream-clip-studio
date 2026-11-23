"""Settings page: detection, timing, clips & limits, export & encoding, FFmpeg."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ...config import settings as cfg
from ...core.context import AppContext
from ...engine.jobs import Job, JobRunner
from ...export import ffmpeg_setup
from ...limits.policy import LimitPolicy
from ..widgets.common import breakable, hbox, notice, page_header

ENCODER_LABELS = {"auto": "Automatic (best that works)", "h264_nvenc": "NVIDIA NVENC", "h264_qsv": "Intel Quick Sync",
                  "h264_amf": "AMD AMF", "libx264": "CPU (libx264)"}


def _dspin(lo, hi, step=0.5, dec=1, suffix=" s") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(dec)
    s.setSuffix(suffix)
    return s


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, runner: JobRunner, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx, self.runner = ctx, runner
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(page_header("Settings", "Changes apply when you press Save."))
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # Detection
        det = QWidget()
        f = QFormLayout(det)
        self.window_s = QSpinBox()
        self.window_s.setRange(1, 30)
        self.window_s.setSuffix(" s")
        self.high = _dspin(0.5, 50, 0.5, 1, "")
        self.low = _dspin(0.1, 50, 0.1, 1, "")
        self.min_unique = QSpinBox()
        self.min_unique.setRange(1, 1000)
        self.min_sep = _dspin(0, 3600, 5, 0)
        self.keywords = QPlainTextEdit()
        self.keywords.setPlaceholderText("one per line: keyword = weight, e.g.  kekw = 1.0")
        f.addRow("Activity window", self.window_s)
        f.addRow("Trigger threshold (high)", self.high)
        f.addRow("Burst start threshold (low)", self.low)
        f.addRow("Minimum distinct chatters", self.min_unique)
        f.addRow("Minimum gap between highlights", self.min_sep)
        f.addRow("Keywords and emotes", self.keywords)
        self.tabs.addTab(det, "Detection")

        # Timing
        tim = QWidget()
        f = QFormLayout(tim)
        self.pre = _dspin(0, 120)
        self.post = _dspin(0, 120)
        self.delay = _dspin(0, 60)
        self.reaction = _dspin(0, 60)
        f.addRow(notice("Estimated event = start of the chat burst − viewer delay − reaction time. "
                        "The clip covers [event − before, event + after]. Defaults: 22 s before, 5 s after (27 s)."))
        f.addRow("Before the event", self.pre)
        f.addRow("After the event", self.post)
        f.addRow("Viewer delay", self.delay)
        f.addRow("Reaction time", self.reaction)
        self.total_lbl = QLabel("")
        self.total_lbl.setObjectName("Muted")
        f.addRow("Clip length", self.total_lbl)
        for w in (self.pre, self.post):
            w.valueChanged.connect(lambda _: self.total_lbl.setText(f"{self.pre.value() + self.post.value():.1f} s"))
        self.tabs.addTab(tim, "Timing")

        # Clips & limits
        lim = QWidget()
        f = QFormLayout(lim)
        self.clip_limit = QSpinBox()
        self.clip_limit.setRange(1, 500)
        self.policy = QComboBox()
        self.policy.addItem("Stop creating clips (monitoring keeps running)", LimitPolicy.STOP.value)
        self.policy.addItem("Replace the weakest clip with a stronger one", LimitPolicy.REPLACE_WEAKEST.value)
        self.auto_own = QCheckBox("Create clips automatically on my own channel")
        self.auto_editor = QCheckBox("Create clips automatically on channels I edit")
        self.auto_other = QCheckBox("Create clips automatically on other people's channels")
        self.auto_other.setToolTip("Off by default: clips on other channels are public and posted under your account, "
                                   "so each one waits for your approval.")
        f.addRow("Clip limit", self.clip_limit)
        f.addRow("When the limit is reached", self.policy)
        f.addRow(self.auto_own)
        f.addRow(self.auto_editor)
        f.addRow(self.auto_other)
        f.addRow(notice("With automatic creation off, each detected moment waits for your approval. Twitch only keeps "
                        "about 90 seconds around the request, so approvals have a short deadline."))
        self.tabs.addTab(lim, "Clips && limits")   # "&&" = literal ampersand in Qt labels

        # Export & encoding
        exp = QWidget()
        f = QFormLayout(exp)
        self.out_dir = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_out_dir)
        self.encoder = QComboBox()
        self.orientation = QComboBox()
        for label, val in [("Landscape (original)", "landscape"), ("Portrait 9:16", "portrait"), ("Both", "both")]:
            self.orientation.addItem(label, val)
        self.encoder_report = QLabel("")
        self.encoder_report.setObjectName("Muted")
        self.encoder_report.setWordWrap(True)
        self.retest_btn = QPushButton("Re-test encoders")
        self.retest_btn.clicked.connect(lambda: self.retest_encoders(force=True))
        f.addRow("Clips folder", hbox(self.out_dir, browse, stretch_end=False))
        f.addRow("Default format", self.orientation)
        f.addRow("Encoder", self.encoder)
        f.addRow("", self.encoder_report)
        f.addRow("", hbox(self.retest_btn))
        self.tabs.addTab(exp, "Export && encoding")

        # FFmpeg
        ff = QWidget()
        f = QFormLayout(ff)
        self.ff_path = QLineEdit()
        self.ff_path.setPlaceholderText("Leave empty to find FFmpeg automatically")
        ffb = QPushButton("Browse…")
        ffb.clicked.connect(self._pick_ffmpeg)
        self.ff_status = QLabel("")
        self.ff_status.setWordWrap(True)
        self.dl_btn = QPushButton(f"Download FFmpeg {ffmpeg_setup.PINNED.version} (optional, ~{ffmpeg_setup.PINNED.size_bytes // 1_000_000} MB)")
        self.dl_btn.clicked.connect(self.download_ffmpeg)
        self.dl_progress = QProgressBar()
        self.dl_progress.hide()
        lic = ffmpeg_setup.LICENSE_NOTICE.format(source=breakable(ffmpeg_setup.PINNED.source_url),
                                                 info=breakable(ffmpeg_setup.PINNED.build_info_url))
        f.addRow("FFmpeg location", hbox(self.ff_path, ffb, stretch_end=False))
        f.addRow("Status", self.ff_status)
        f.addRow("", hbox(self.dl_btn))
        f.addRow("", self.dl_progress)
        f.addRow(notice(f"<b>License:</b> {lic}"))
        self.tabs.addTab(ff, "FFmpeg")

        # Capture (Mode D)
        capw = QWidget()
        f = QFormLayout(capw)
        lim_c = cfg.CAPTURE_LIMITS
        self.cap_before = _dspin(*lim_c["clip_before_s"], step=1, dec=0)
        self.cap_after = _dspin(*lim_c["clip_after_s"], step=1, dec=0)
        self.cap_buffer = _dspin(*lim_c["buffer_s"], step=30, dec=0)
        f.addRow(notice("For <b>Clip that</b> on the Live Monitor page (local capture, Mode D). The clip keeps "
                        "footage from before the press and records a little more after it. "
                        "Defaults: 22 s before, 5 s after. The rolling buffer must be at least "
                        f"{cfg.BUFFER_MARGIN_S:g} s longer than the clip; it's raised automatically if not. "
                        "Changes apply the next time capture starts (before/after: at the next press)."))
        f.addRow("Before the press", self.cap_before)
        f.addRow("After the press", self.cap_after)
        f.addRow("Rolling buffer length", self.cap_buffer)
        self.tabs.addTab(capw, "Capture")

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self.reset_defaults)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("Primary")
        self.save_btn.clicked.connect(self.save)
        buttons.addWidget(reset)
        buttons.addWidget(self.save_btn)
        root.addLayout(buttons)
        self.message = QLabel("")
        self.message.setObjectName("Muted")
        root.addWidget(self.message)
        for w in (self.window_s, self.high, self.low, self.min_unique, self.min_sep, self.pre, self.post,
                  self.delay, self.reaction, self.clip_limit, self.cap_before, self.cap_after, self.cap_buffer):
            w.setMaximumWidth(160)
        for w in (self.policy, self.orientation, self.encoder):
            w.setMaximumWidth(420)
        self.keywords.setMaximumWidth(420)
        ctx.events.ffmpeg_changed.connect(self.refresh_ffmpeg_status)
        self.load()

    # --- load / save ---------------------------------------------------------------
    def load(self, s: cfg.Settings | None = None) -> None:
        s = s or self.ctx.settings
        d, t, l, p, e = s.detection, s.timing, s.limits, s.publish, s.export
        self.window_s.setValue(d.window_s)
        self.high.setValue(d.high)
        self.low.setValue(d.low)
        self.min_unique.setValue(d.min_unique)
        self.min_sep.setValue(d.min_separation_s)
        self.keywords.setPlainText("\n".join(f"{k} = {v:g}" for k, v in d.keywords.items()))
        self.pre.setValue(t.pre_roll_s)
        self.post.setValue(t.post_roll_s)
        self.delay.setValue(t.viewer_delay_s)
        self.reaction.setValue(t.reaction_s)
        self.total_lbl.setText(f"{t.pre_roll_s + t.post_roll_s:.1f} s")
        self.clip_limit.setValue(l.clip_limit)
        self.policy.setCurrentIndex(max(0, self.policy.findData(l.policy)))
        self.auto_own.setChecked(p.auto_create_own)
        self.auto_editor.setChecked(p.auto_create_editor)
        self.auto_other.setChecked(p.auto_create_other)
        self.out_dir.setText(e.output_dir or str(self.ctx.paths.default_clips_dir))
        self.orientation.setCurrentIndex(max(0, self.orientation.findData(e.orientation)))
        self._fill_encoders(e.encoder)
        self.ff_path.setText(s.ffmpeg.path_override)
        self.cap_before.setValue(s.capture.clip_before_s)
        self.cap_after.setValue(s.capture.clip_after_s)
        self.cap_buffer.setValue(s.capture.buffer_s)
        self.refresh_ffmpeg_status()

    def _fill_encoders(self, current: str) -> None:
        self.encoder.clear()
        rep = self.ctx.encoders
        for name in cfg.ENCODER_CHOICES:
            label = ENCODER_LABELS[name]
            if rep and name != "auto":
                label += "  ✓ works" if rep.works(name) else "  ✗ not available"
            self.encoder.addItem(label, name)
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(current)))
        if rep:
            lines = [f"{t['name']}: {'works' if t['works'] else 'not available'}" for t in rep.tests]
            self.encoder_report.setText(f"Automatic choice: {rep.auto_choice}. " + " · ".join(lines))
        else:
            self.encoder_report.setText("Encoders not tested yet.")

    def parse_keywords(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for line in self.keywords.toPlainText().splitlines():
            if "=" not in line:
                continue
            k, v = line.rsplit("=", 1)
            try:
                if k.strip():
                    out[k.strip().lower()] = float(v)
            except ValueError:
                continue
        return out

    def save(self) -> None:
        s = self.ctx.settings
        s.detection.window_s = self.window_s.value()
        s.detection.high = self.high.value()
        s.detection.low = self.low.value()
        s.detection.min_unique = self.min_unique.value()
        s.detection.min_separation_s = self.min_sep.value()
        s.detection.keywords = self.parse_keywords() or dict(cfg.DetectionSettings().keywords)
        s.timing.pre_roll_s = self.pre.value()
        s.timing.post_roll_s = self.post.value()
        s.timing.viewer_delay_s = self.delay.value()
        s.timing.reaction_s = self.reaction.value()
        s.limits.clip_limit = self.clip_limit.value()
        s.limits.policy = self.policy.currentData()
        s.publish.auto_create_own = self.auto_own.isChecked()
        s.publish.auto_create_editor = self.auto_editor.isChecked()
        s.publish.auto_create_other = self.auto_other.isChecked()
        out = self.out_dir.text().strip()
        s.export.output_dir = "" if out == str(self.ctx.paths.default_clips_dir) else out
        s.export.orientation = self.orientation.currentData()
        s.export.encoder = self.encoder.currentData()
        ff_changed = s.ffmpeg.path_override != self.ff_path.text().strip()
        s.ffmpeg.path_override = self.ff_path.text().strip()
        s.capture.clip_before_s = self.cap_before.value()
        s.capture.clip_after_s = self.cap_after.value()
        s.capture.buffer_s = self.cap_buffer.value()
        warnings = self.ctx.save_settings()
        self.load()
        self.message.setText("Saved." + (" Adjusted: " + "; ".join(warnings) if warnings else ""))
        if ff_changed:
            self.ctx.refresh_ffmpeg()
            self.retest_encoders()

    def reset_defaults(self) -> None:
        self.load(cfg.Settings())
        self.message.setText("Defaults loaded. Press Save to keep them.")

    # --- encoders / ffmpeg ---------------------------------------------------------
    def retest_encoders(self, force: bool = False) -> None:
        if not self.ctx.ffmpeg:
            self._fill_encoders(self.ctx.settings.export.encoder)
            return
        self.retest_btn.setEnabled(False)
        self.encoder_report.setText("Testing encoders…")
        job = Job(lambda: self.ctx.detect_encoders(force=force), pass_progress=False)

        def done(_):
            self.retest_btn.setEnabled(True)
            self._fill_encoders(self.ctx.settings.export.encoder)
            self.ctx.events.ffmpeg_changed.emit()

        job.signals.finished.connect(done)
        job.signals.failed.connect(lambda m: (self.retest_btn.setEnabled(True), self.encoder_report.setText(m.splitlines()[0])))
        self.runner.submit(job)

    def refresh_ffmpeg_status(self) -> None:
        inst = self.ctx.ffmpeg
        if inst:
            self.ff_status.setText(f"✓ {inst.version_string}<br><span style='color:gray'>{breakable(str(inst.ffmpeg))} "
                                   f"({inst.source})</span>")
        else:
            self.ff_status.setText("✗ FFmpeg not found. Install it (e.g. <code>winget install Gyan.FFmpeg</code>), "
                                   "choose its location, or use the optional download below.")
        self.dl_btn.setVisible(inst is None)
        if hasattr(self, "encoder"):
            self._fill_encoders(self.ctx.settings.export.encoder)

    def download_ffmpeg(self) -> None:
        b = ffmpeg_setup.PINNED
        ok = QMessageBox.question(
            self, "Download FFmpeg",
            f"Download FFmpeg {b.version} (about {b.size_bytes // 1_000_000} MB) from gyan.dev?\n\n"
            f"License: {b.license}. The file's SHA256 is checked before it's used.") == QMessageBox.StandardButton.Yes
        if not ok:
            return
        dest = self.ctx.paths.data_dir / "ffmpeg"
        self.dl_btn.setEnabled(False)
        self.dl_progress.show()

        def work(progress, _c):
            return ffmpeg_setup.install(dest, progress=lambda d, t: progress(d / max(t, 1), "Downloading"))

        job = Job(work)
        job.signals.progress.connect(lambda f, _m: self.dl_progress.setValue(int(f * 100)))

        def done(exe):
            self.dl_progress.hide()
            self.ctx.settings.ffmpeg.path_override = str(exe)
            self.ctx.save_settings()
            self.ff_path.setText(str(exe))
            self.ctx.refresh_ffmpeg()
            self.retest_encoders(force=True)

        def failed(msg):
            self.dl_progress.hide()
            self.dl_btn.setEnabled(True)
            self.ff_status.setText("Download failed: " + msg.splitlines()[0])

        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        self.runner.submit(job)

    def _pick_out_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose clips folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _pick_ffmpeg(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Choose ffmpeg.exe", "", "ffmpeg (ffmpeg.exe);;All files (*)")
        if p:
            self.ff_path.setText(p)
