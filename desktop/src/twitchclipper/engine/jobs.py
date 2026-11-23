"""Background jobs for the desktop UI.

Heavy work (encoder detection, chat analysis, ffmpeg exports) runs on worker threads;
results come back to the UI thread through Qt signals (queued connections).
Exports have their own single-thread pool so only one ffmpeg export runs at a time.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class JobSignals(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    """Runs fn(progress, cancelled) on a worker thread."""

    def __init__(self, fn: Callable[..., Any], *, pass_progress: bool = True):
        super().__init__()
        self.fn = fn
        self.pass_progress = pass_progress
        self.signals = JobSignals()
        self._cancel = False
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel

    def run(self) -> None:
        try:
            if self.pass_progress:
                result = self.fn(lambda f, msg="": self.signals.progress.emit(float(f), msg), self.cancelled)
            else:
                result = self.fn()
        except InterruptedError as exc:
            self._emit(self.signals.failed, f"Cancelled: {exc}")
        except Exception as exc:  # noqa: BLE001 - report every failure to the UI
            self._emit(self.signals.failed, f"{exc}\n\n{traceback.format_exc(limit=4)}")
        else:
            self._emit(self.signals.finished, result)

    @staticmethod
    def _emit(signal, value) -> None:
        try:
            signal.emit(value)
        except RuntimeError:     # the receiving side was torn down (app closing) while the job ran
            pass


class JobRunner(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.general = QThreadPool(self)
        self.general.setMaxThreadCount(2)
        self.exports = QThreadPool(self)
        self.exports.setMaxThreadCount(1)
        self._alive: set[Job] = set()

    def submit(self, job: Job, export: bool = False) -> Job:
        self._alive.add(job)
        job.signals.finished.connect(lambda *_: self._alive.discard(job))
        job.signals.failed.connect(lambda *_: self._alive.discard(job))
        (self.exports if export else self.general).start(job)
        return job

    def active(self) -> int:
        return len(self._alive)

    def wait(self, ms: int = 30000) -> bool:
        return self.general.waitForDone(ms) and self.exports.waitForDone(ms)
