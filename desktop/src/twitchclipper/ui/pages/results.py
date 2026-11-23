"""Results page: the clip library with import, preview, open, show in folder and delete."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core.context import AppContext
from ...engine.jobs import Job, JobRunner
from ...pipeline import library
from ..widgets.common import fmt_time, page_header
from ..widgets.video_preview import VideoPreview

COLUMNS = ["Clip", "Source", "Format", "Length", "Encoder", "Created"]


class SystemLauncher:
    """Hands a file to Windows: open it in the default app, or select it in Explorer."""

    def open(self, path: Path) -> None:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - the user's default player
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def reveal(self, path: Path) -> None:
        if sys.platform == "win32":
            if not shell_select(path):
                # Fallback. Explorer opens the folder but, on Windows 10, often only focuses the file
                # instead of selecting it, which is why the Shell API above is preferred.
                subprocess.Popen(f'explorer /select,"{path}"')
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])


def shell_select(path: Path) -> bool:
    """Open the folder in Explorer with the file selected (SHOpenFolderAndSelectItems). True on success."""
    try:
        import ctypes
        from ctypes import wintypes

        ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        init = ole32.CoInitializeEx(None, 0x2)          # apartment-threaded; S_FALSE if already initialised
        try:
            pidl = shell32.ILCreateFromPathW(os.path.normpath(str(path)))
            if not pidl:
                return False
            try:
                return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
            finally:
                shell32.ILFree(pidl)
        finally:
            if init in (0, 1):
                ole32.CoUninitialize()
    except (OSError, AttributeError):
        return False


class ResultsPage(QWidget):
    def __init__(self, ctx: AppContext, runner: JobRunner | None = None, parent: QWidget | None = None,
                 confirm=None, launcher=None):
        super().__init__(parent)
        self.ctx = ctx
        self.runner = runner or JobRunner(self)
        self.confirm = confirm or self._confirm_dialog
        self.launcher = launcher or SystemLauncher()
        self.choose_file = self._choose_file_dialog
        self._import_job: Job | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(page_header("Results", "Your clips. Select one to preview it, or import a video you already have."))
        bar = QHBoxLayout()
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.import_btn = QPushButton("Import video…")
        self.import_btn.setToolTip("Copy a local video into your clips folder and add it to this list.\n"
                                   "The original file is never changed or deleted.")
        self.open_btn = QPushButton("Open")
        self.open_btn.setToolTip("Open in your default video player")
        self.folder_btn = QPushButton("Show in folder")
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setObjectName("Danger")
        refresh = QPushButton("Refresh")
        for b, fn in [(self.import_btn, lambda: self.import_video()), (self.open_btn, self.open_selected),
                      (self.folder_btn, self.show_in_folder), (self.delete_btn, self.delete_selected),
                      (refresh, self.refresh)]:
            b.clicked.connect(fn)
        bar.addWidget(self.count)
        bar.addSpacing(12)
        bar.addWidget(self.status)
        bar.addStretch(1)
        for b in (self.import_btn, self.open_btn, self.folder_btn, self.delete_btn, refresh):
            bar.addWidget(b)
        root.addLayout(bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        self.preview = VideoPreview(closable=True)
        split.addWidget(self.table)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)
        self.rows: list = []
        ctx.events.clips_changed.connect(self.refresh)
        self.refresh()

    # --- list -------------------------------------------------------------
    def refresh(self) -> None:
        keep = self.selected()
        keep_id = keep["id"] if keep else None
        self.rows = self.ctx.db.clips()
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.rows))
        missing = 0
        for r, c in enumerate(self.rows):
            exists = Path(c["path"]).exists()
            missing += not exists
            name = Path(c["path"]).name + ("" if exists else "  (file missing)")
            imported = c["source"] == "import"
            source = c["analysis_title"] or ("Imported file" if imported else
                                             "Live capture" if c["source"] == "capture" else "-")
            vals = [name, source, c["orientation"], fmt_time(c["duration_s"] or 0),
                    "-" if imported else (c["encoder"] or "-"),
                    datetime.fromtimestamp(c["created_at"]).strftime("%Y-%m-%d %H:%M")]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.ItemDataRole.UserRole, c["id"])
                if col == 0:
                    item.setToolTip(c["path"])
                self.table.setItem(r, col, item)
        self.table.blockSignals(False)
        self.count.setText(f"{len(self.rows)} clip(s)" + (f" · {missing} missing on disk" if missing else ""))
        if keep_id is None or not self.select_clip(keep_id):
            self.preview.release()   # nothing selected, or the selected clip is gone
        self._update_buttons()

    def select_clip(self, clip_id: int) -> bool:
        for r, c in enumerate(self.rows):
            if c["id"] == clip_id:
                self.table.selectRow(r)
                return True
        return False

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.rows[rows[0].row()] if rows and rows[0].row() < len(self.rows) else None

    def _update_buttons(self) -> None:
        c = self.selected()
        exists = bool(c and Path(c["path"]).is_file())
        self.open_btn.setEnabled(exists)
        # still useful when the file is gone: opens the folder it was in
        self.folder_btn.setEnabled(bool(c and (exists or Path(c["path"]).parent.is_dir())))
        self.delete_btn.setEnabled(c is not None)
        self.import_btn.setEnabled(self._import_job is None)

    def _selected(self) -> None:
        c = self.selected()
        path = c["path"] if c and Path(c["path"]).exists() else None
        current = self.preview.source_path
        if not (path and current and Path(path) == Path(current)):
            self.preview.load(path)
        self._update_buttons()

    # --- import -----------------------------------------------------------
    def _choose_file_dialog(self) -> str | None:
        patterns = " ".join(f"*{e}" for e in library.VIDEO_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(self, "Import a video", str(Path.home() / "Videos"),
                                              f"Video files ({patterns});;All files (*)")
        return path or None

    def import_video(self, path: str | Path | None = None) -> Job | None:
        if not self.ctx.ffmpeg:
            self.warn("FFmpeg needed", "Importing checks the video with FFmpeg. Set it up in Settings → FFmpeg first.")
            return None
        path = path or self.choose_file()
        if not path:
            return None
        ffprobe, clips_dir, db = self.ctx.ffmpeg.ffprobe, self.ctx.clips_dir, self.ctx.db
        job = Job(lambda progress, cancelled: library.import_video(ffprobe, path, clips_dir, db, progress, cancelled))
        job.signals.progress.connect(lambda f, m: self.status.setText(m))
        job.signals.finished.connect(self._import_done)
        job.signals.failed.connect(self._import_failed)
        self._import_job = job
        self.status.setText(f"Importing {Path(path).name}…")
        self._update_buttons()
        self.runner.submit(job)
        return job

    def _import_done(self, res: library.ImportResult) -> None:
        self._import_job = None
        self.ctx.events.clips_changed.emit()
        self.select_clip(res.clip_id)
        self.status.setText(f"Imported {res.path.name}" + ("" if res.has_audio else " (no audio track)"))
        self._update_buttons()

    def _import_failed(self, msg: str) -> None:
        self._import_job = None
        self.status.setText("Import failed")
        self._update_buttons()
        self.warn("Import failed", msg.split("\n\n")[0])

    # --- file actions -----------------------------------------------------
    def open_selected(self) -> None:
        c = self.selected()
        if not c:
            return
        p = Path(c["path"])
        if not p.is_file():
            self._file_gone(p)
            return
        try:
            self.launcher.open(p)
        except OSError as exc:
            self.warn("Couldn't open the clip", describe_launch_error(exc, p))

    def show_in_folder(self) -> None:
        c = self.selected()
        if not c:
            return
        p = Path(c["path"])
        try:
            if p.is_file():
                self.launcher.reveal(p)
            elif p.parent.is_dir():           # file gone, folder still there: open the folder
                self.launcher.open(p.parent)
                self._file_gone(p, folder_opened=True)
            else:
                self._file_gone(p)
        except OSError as exc:
            self.warn("Couldn't show the clip in its folder", describe_launch_error(exc, p))

    def _file_gone(self, p: Path, folder_opened: bool = False) -> None:
        self.refresh()
        if folder_opened:
            self.status.setText(f"{p.name} is no longer in this folder")
            return
        self.warn("Clip not found", f"{p.name} no longer exists at\n{p.parent}\n\n"
                  "It may have been moved, renamed or deleted outside TwitchClipper. "
                  "Use Delete… to remove it from the list.")

    def _confirm_dialog(self, text: str) -> bool:
        title = "Delete clip" if text.startswith("Delete") else "Remove from list"
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def delete_selected(self) -> None:
        c = self.selected()
        if not c:
            return
        plan = library.plan_delete(self.ctx.db, c["id"])
        p = plan.path
        if plan.delete_file and self.ctx.file_in_use(p):
            self.warn("Clip is in use", f"{p.name} is being read by an export or another job in TwitchClipper.\n\n"
                      "Wait for it to finish (or cancel it on the Analyze page), then try again.")
            return
        if plan.delete_file:
            note = ("\n\nOnly this imported copy is deleted. The original file stays where it is."
                    if c["source"] == "import" else "")
            text = f"Delete {p.name}?\n\nThe file will be removed from disk and from the list.{note}"
        else:
            text = f"Remove {p.name} from the list?\n\nThe file stays on disk because {plan.reason}."
        if not self.confirm(text):
            return
        if plan.delete_file:
            VideoPreview.release_everywhere(p)    # Results, Analyze and any other preview holding this file
            self.preview.release()
            error = self._unlink_with_retry(p)
            if error:
                self.warn("Couldn't delete file", error)
                return
        self.ctx.db.delete_clip(c["id"])
        self.status.setText(f"Deleted {p.name}" if plan.delete_file else f"Removed {p.name} from the list")
        self.ctx.events.clips_changed.emit()

    def warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    @staticmethod
    def _unlink_with_retry(p: Path, attempts: int = 5, delay_ms: int = 100) -> str | None:
        """Delete `p`. Returns None on success or a user-facing explanation.

        Every preview in the app releases the file before this runs (measured: the first attempt
        succeeds), so the short retry only covers a slow external program letting go.
        """
        for i in range(attempts):
            try:
                p.unlink(missing_ok=True)
                return None
            except PermissionError as exc:
                if getattr(exc, "winerror", None) == ERROR_SHARING_VIOLATION:
                    if i < attempts - 1:
                        QApplication.processEvents()
                        QThread.msleep(delay_ms)
                        continue
                    return (f"{p.name} is open in another program (for example a video player). "
                            "Close it and try again.")
                return (f"Windows denied access to {p.name}"
                        + (" because it's marked read-only" if is_read_only(p) else "")
                        + ". The file was not changed and stays in the list.")
            except OSError as exc:
                return f"{p.name} couldn't be deleted: {exc.strerror or exc}"
        return None


ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_NO_ASSOCIATION = 1155


def is_read_only(p: Path) -> bool:
    try:
        return not os.stat(p).st_mode & stat.S_IWRITE
    except OSError:
        return False


def describe_launch_error(exc: OSError, p: Path) -> str:
    if getattr(exc, "winerror", None) == ERROR_NO_ASSOCIATION:
        return (f"No app is set to open {p.suffix or 'these'} files. "
                "Choose one in Windows Settings → Apps → Default apps, then try again.")
    if isinstance(exc, FileNotFoundError):
        return f"{p.name} no longer exists."
    return f"Windows couldn't open {p.name}: {exc.strerror or exc}"
