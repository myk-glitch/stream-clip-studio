"""Analyze page: local video + chat log -> ranked highlights -> clips (Mode F, no sign-in)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core.context import AppContext
from ...engine.jobs import Job, JobRunner
from ...pipeline import library
from ...pipeline import local as pipeline
from ..widgets.chat_graph import ChatGraph
from ..widgets.common import StatusLabel, fmt_time, notice, page_header
from ..widgets.crop_editor import CropEditor
from ..widgets.video_preview import VideoPreview

log = logging.getLogger("twitchclipper.analyze")

VIDEO_FILTER = "Video files (*.mp4 *.mkv *.mov *.ts *.flv *.webm);;All files (*)"
CHAT_FILTER = "Chat logs (*.json *.jsonl *.csv);;All files (*)"


class AnalyzePage(QWidget):
    def __init__(self, ctx: AppContext, runner: JobRunner, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx, self.runner = ctx, runner
        self.result: pipeline.AnalysisResult | None = None
        self.analysis_id: int | None = None
        self.export_jobs: list[Job] = []
        self._reserved_outputs: set[str] = set()   # output paths of queued exports
        self._frame_job: Job | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 14)
        root.addWidget(page_header(
            "Analyze", "Find highlights in a local video using its chat log. No Twitch sign-in needed."))
        self.ffmpeg_notice = notice("FFmpeg wasn't found. Install it or set its location in Settings → FFmpeg.")
        root.addWidget(self.ffmpeg_notice)

        # --- inputs ---------------------------------------------------------
        inputs = QGroupBox("Inputs")
        grid = QGridLayout(inputs)
        self.video_edit = QLineEdit()
        self.video_edit.setPlaceholderText("Video file you have the rights to use (e.g. your own recording)")
        self.chat_edit = QLineEdit()
        self.chat_edit.setPlaceholderText("Chat log: TwitchClipper .jsonl, TwitchDownloader .json, or .csv")
        vb = QPushButton("Browse…")
        vb.clicked.connect(self._pick_video)
        cb = QPushButton("Browse…")
        cb.clicked.connect(self._pick_chat)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-86400, 86400)
        self.offset.setDecimals(1)
        self.offset.setSingleStep(0.5)
        self.offset.setSuffix(" s")
        self.offset.setToolTip("Added to every chat timestamp to line it up with the video. 0 if the chat log "
                               "belongs to this exact video; negative if the recording started after the stream.")
        self.limit = QSpinBox()
        self.limit.setRange(1, 500)
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.setObjectName("Primary")
        self.analyze_btn.clicked.connect(self.start_analysis)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()                  # shown once an analysis starts
        self.status = StatusLabel()
        grid.addWidget(QLabel("Video"), 0, 0)
        grid.addWidget(self.video_edit, 0, 1)
        grid.addWidget(vb, 0, 2)
        grid.addWidget(QLabel("Chat log"), 1, 0)
        grid.addWidget(self.chat_edit, 1, 1)
        grid.addWidget(cb, 1, 2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Chat offset"))
        row.addWidget(self.offset)
        row.addSpacing(16)
        row.addWidget(QLabel("Clip limit"))
        row.addWidget(self.limit)
        row.addStretch(1)
        row.addWidget(self.analyze_btn)
        grid.addLayout(row, 2, 0, 1, 3)
        grid.addWidget(self.progress, 3, 0, 1, 3)
        grid.addWidget(self.status, 4, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        root.addWidget(inputs)

        # --- results ----------------------------------------------------------
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.graph = ChatGraph()
        self.graph.setMinimumHeight(130)
        self.graph.clicked.connect(self._graph_clicked)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Export", "#", "Event", "Clip window", "Score", "Chatters"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._row_selected)
        ll.addWidget(self.graph, 2)
        ll.addWidget(self.table, 3)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.preview = VideoPreview(video_min_height=120)
        self.preview.positionChanged.connect(self.graph.show_cursor)
        rl.addWidget(self.preview, 3)
        out = QGroupBox("Output")
        of = QFormLayout(out)
        self.orientation = QComboBox()
        self.orientation.addItem("Landscape (original)", "landscape")
        self.orientation.addItem("Portrait 9:16", "portrait")
        self.orientation.addItem("Both", "both")
        self.orientation.currentIndexChanged.connect(self._orientation_changed)
        self.crop = CropEditor()
        self.crop.setMinimumSize(220, 100)
        self.crop.centerChanged.connect(self._crop_changed)
        of.addRow("Format", self.orientation)
        of.addRow("Crop", self.crop)
        rl.addWidget(out, 2)
        btns = QHBoxLayout()
        self.export_btn = QPushButton("Export checked highlights")
        self.export_btn.setObjectName("Primary")
        self.export_btn.clicked.connect(self.export_checked)
        self.cancel_btn = QPushButton("Cancel exports")
        self.cancel_btn.clicked.connect(self.cancel_exports)
        btns.addWidget(self.export_btn)
        btns.addWidget(self.cancel_btn)
        rl.addLayout(btns)
        self.export_list = QListWidget()
        self.export_list.setMinimumHeight(44)
        self.export_list.setMaximumHeight(120)
        rl.addWidget(self.export_list)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        ctx.events.settings_changed.connect(self.load_settings)
        ctx.events.ffmpeg_changed.connect(self._update_enabled)
        self.load_settings()
        self._update_enabled()

    # --- state ----------------------------------------------------------------
    def load_settings(self) -> None:
        s = self.ctx.settings
        self.limit.setValue(s.limits.clip_limit)
        idx = self.orientation.findData(s.export.orientation)
        self.orientation.setCurrentIndex(max(0, idx))
        self.crop.set_center(s.export.portrait_center)
        self._orientation_changed()

    def _update_enabled(self) -> None:
        has_ff = self.ctx.ffmpeg is not None
        self.ffmpeg_notice.setVisible(not has_ff)
        self.analyze_btn.setEnabled(has_ff)
        self.export_btn.setEnabled(has_ff and self.result is not None and bool(self.result.highlights))
        self.cancel_btn.setEnabled(any(not j.cancelled() for j in self.export_jobs))

    def _pick_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose video", "", VIDEO_FILTER)
        if path:
            self.video_edit.setText(path)

    def _pick_chat(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose chat log", "", CHAT_FILTER)
        if path:
            self.chat_edit.setText(path)

    # --- analysis ---------------------------------------------------------------
    def start_analysis(self) -> None:
        video, chat = Path(self.video_edit.text().strip()), Path(self.chat_edit.text().strip())
        problems = []
        if not video.is_file():
            problems.append("Choose a video file that exists.")
        if not chat.is_file():
            problems.append("Choose a chat log file that exists.")
        if not self.ctx.ffmpeg:
            problems.append("FFmpeg is required.")
        if problems:
            self.status.setText(" ".join(problems))
            return
        s = self.ctx.settings
        dcfg, tcfg = s.detector_config(), s.timing_config()
        offset, limit = self.offset.value(), self.limit.value()
        ffprobe = self.ctx.ffmpeg.ffprobe
        self.analyze_btn.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText("Analyzing…")

        def work(progress, _cancelled):
            return pipeline.analyze(ffprobe, video, chat, offset, limit, dcfg, tcfg, progress)

        job = Job(work)
        job.signals.progress.connect(lambda f, m: (self.progress.setValue(int(f * 100)), self.status.setText(m)))
        job.signals.finished.connect(self._analysis_done)
        job.signals.failed.connect(self._analysis_failed)
        self.runner.submit(job)

    def _analysis_failed(self, msg: str) -> None:
        self.analyze_btn.setEnabled(self.ctx.ffmpeg is not None)
        self.progress.setValue(0)
        self.status.setText("Analysis failed: " + msg.split("\n")[0])
        log.error("analysis failed: %s", msg)

    def _analysis_done(self, result: pipeline.AnalysisResult) -> None:
        self.result = result
        hl = [h.__dict__ | {"window_start": h.window_start, "window_end": h.window_end} for h in result.highlights]
        self.analysis_id = self.ctx.db.save_analysis(
            "local", result.video.name, str(result.video), str(result.chat), result.chat_offset_s,
            result.duration_s, result.message_count, result.params, hl)
        for h, row in zip(result.highlights, hl):
            h.id = row["id"]
        s = self.ctx.settings.detection
        self.graph.set_data(result.times, result.scores, s.high, s.low)
        self.graph.set_highlights([(h.rank, h.window_start, h.window_end) for h in result.highlights])
        self._fill_table()
        self.preview.load(result.video)
        msg = (f"{result.message_count:,} messages over {fmt_time(result.duration_s)} · "
               f"{result.candidates} candidate moments · top {len(result.highlights)} shown")
        if result.warnings:
            msg += " · ⚠ " + " ⚠ ".join(result.warnings)
        self.status.setText(msg)
        self.analyze_btn.setEnabled(True)
        self._update_enabled()
        if result.highlights:
            self.table.selectRow(0)

    def _fill_table(self) -> None:
        hs = self.result.highlights if self.result else []
        self.table.setRowCount(len(hs))
        for r, h in enumerate(hs):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, check)
            window = f"{fmt_time(h.window_start)} – {fmt_time(h.window_end)}" + (" (shortened)" if h.clamped else "")
            for c, text in enumerate([str(h.rank), fmt_time(h.event_t), window, f"{h.score:.1f}", str(h.unique_at_peak)], start=1):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, r)
                self.table.setItem(r, c, item)

    def checked_highlights(self) -> list[pipeline.Highlight]:
        if not self.result:
            return []
        return [h for r, h in enumerate(self.result.highlights)
                if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.CheckState.Checked]

    def _row_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or not self.result:
            return
        h = self.result.highlights[rows[0].row()]
        self.preview.seek(h.window_start)
        self.graph.show_cursor(h.event_t)
        self._load_crop_frame(h.event_t)

    def _graph_clicked(self, t: float) -> None:
        if self.result:
            self.preview.seek(t)

    def _load_crop_frame(self, at: float) -> None:
        if not (self.ctx.ffmpeg and self.result):
            return
        ffmpeg, video = self.ctx.ffmpeg.ffmpeg, self.result.video
        out = self.ctx.paths.work_dir / "crop_preview.png"
        ctx = self.ctx

        def work():
            with ctx.using_file(video):
                return pipeline.extract_frame(ffmpeg, video, at, out)

        job = Job(work, pass_progress=False)
        job.signals.finished.connect(self.crop.set_frame)
        self._frame_job = self.runner.submit(job)

    def _orientation_changed(self) -> None:
        self.crop.setEnabled(self.orientation.currentData() != "landscape")

    def _crop_changed(self, frac: float) -> None:
        self.ctx.settings.export.portrait_center = round(frac, 4)

    # --- export -------------------------------------------------------------------
    def export_checked(self) -> None:
        if not (self.result and self.ctx.ffmpeg):
            return
        chosen = self.checked_highlights()
        if not chosen:
            self.status.setText("Check at least one highlight to export.")
            return
        out_dir = self.ctx.clips_dir
        if self.ctx.disk_free_gb() < 1.0:
            QMessageBox.warning(self, "Low disk space", f"Less than 1 GB free on the drive for {out_dir}.")
            return
        encoder, note = self.ctx.encoder_for_export()
        orient = self.orientation.currentData()
        orientations = ["landscape", "portrait"] if orient == "both" else [orient]
        ff, fp = self.ctx.ffmpeg.ffmpeg, self.ctx.ffmpeg.ffprobe
        center = self.crop.center
        for h in chosen:
            for o in orientations:
                # never overwrite an existing clip or one that is already queued
                out = library.unique_destination(out_dir, pipeline.clip_filename(self.result.video, h, o),
                                                 self._reserved_outputs)
                job_spec = pipeline.ExportJob(self.result.video, h.window, out, o, center, encoder)
                self._queue_export(ff, fp, job_spec, h)
        self.status.setText(f"Queued {len(chosen) * len(orientations)} export(s) with {encoder}"
                            + (f" ({note})" if note not in ("auto", "user choice") else ""))
        self._update_enabled()

    def _queue_export(self, ff, fp, spec: pipeline.ExportJob, h: pipeline.Highlight) -> None:
        item = QListWidgetItem(f"Queued · #{h.rank} {spec.orientation} → {spec.out.name}")
        self.export_list.addItem(item)
        analysis_id = self.analysis_id
        ctx = self.ctx

        def work(progress, cancelled):
            if cancelled():
                raise InterruptedError("cancelled before start")
            with ctx.using_file(spec.video):
                return pipeline.export(ff, fp, spec, progress, cancelled)

        job = Job(work)
        job.signals.progress.connect(lambda f, m: item.setText(f"{int(f * 100):3d}% · #{h.rank} {spec.orientation} → {spec.out.name}"))

        def done(res: pipeline.ExportResult):
            self._reserved_outputs.discard(library.norm_path(spec.out))
            self.ctx.db.add_clip(analysis_id=analysis_id, highlight_id=h.id, path=str(res.out),
                                 orientation=spec.orientation, window_start=spec.window.start,
                                 window_end=spec.window.end, duration_s=res.duration_s, encoder=res.encoder,
                                 score=h.score)
            item.setText(f"Done · #{h.rank} {spec.orientation} ({res.duration_s:.1f} s, {res.seconds:.1f} s to encode) → {res.out.name}")
            self.ctx.events.clips_changed.emit()
            self._update_enabled()

        def failed(msg: str):
            self._reserved_outputs.discard(library.norm_path(spec.out))
            item.setText(f"Failed · #{h.rank} {spec.orientation}: {msg.splitlines()[0]}")
            log.error("export failed: %s", msg)
            self._update_enabled()

        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        self.export_jobs.append(job)
        self.runner.submit(job, export=True)

    def cancel_exports(self) -> None:
        for j in self.export_jobs:
            j.cancel()
        self._update_enabled()
