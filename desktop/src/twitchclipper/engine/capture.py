"""Mode D in the app: one capture session (LocalRingRecorder) plus the manual "Clip that" action.

Everything slow runs off the UI thread:
  * listing sources (runs FFmpeg) and start/stop (stop can wait for FFmpeg to finish) run on a
    one-thread "control" pool, so they are serialised;
  * each clip waits for its post-roll and encodes on a one-thread "clips" pool, so clips never
    queue behind the Analyze page's exports and never pause recording.
Results come back through Qt signals. The ring buffer does the pinning, cutting and checking;
this class only decides the window and records the finished clip in the database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from ..core.context import AppContext
from ..export import ffmpeg as ft
from ..pipeline import library
from ..sources import capture as cap
from ..sources.local_ring import FootageMissing, LocalRingRecorder, Pin, PreRollLost, RingError
from ..timing.window import Window
from .jobs import Job

log = logging.getLogger(__name__)

DOUBLE_PRESS_S = 2.0       # presses closer together than this are one press
MAX_PENDING_CLIPS = 5
STALL_TIMEOUT_S = 15.0     # no new segment for this long = source lost (see LocalRingRecorder)


@dataclass
class PendingClip:
    token: int
    window: Window
    pressed_at: float
    pre_pin: Pin | None


RecorderFactory = Callable[[cap.CaptureSource, cap.AudioDevice | None], LocalRingRecorder]


class CaptureController(QObject):
    sources_changed = Signal(object)          # SourceList
    state_changed = Signal(str, str)          # recorder state, message ("" if none)
    status_changed = Signal(dict)             # periodic snapshot for the UI
    clip_queued = Signal(int, str)            # token, description
    clip_finished = Signal(int, int, str)     # token, clip id, path
    clip_failed = Signal(int, str)            # token, readable reason
    busy_changed = Signal(bool)               # a start/stop is in progress

    def __init__(self, ctx: AppContext, parent: QObject | None = None, *,
                 enumerate_fn: Callable[[], cap.SourceList] | None = None,
                 recorder_factory: RecorderFactory | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.enumerate_fn = enumerate_fn or (lambda: cap.enumerate_sources(ctx.ffmpeg.ffmpeg if ctx.ffmpeg else None))
        self.recorder_factory = recorder_factory or self._default_recorder
        self.sources = cap.SourceList()
        self.rec: LocalRingRecorder | None = None
        self.source: cap.CaptureSource | None = None
        self.audio: cap.AudioDevice | None = None
        self.pending: dict[int, PendingClip] = {}
        self._next_token = 1
        self._last_press = -1e9
        self._reserved: set[str] = set()
        self._busy = False
        self._listing = False
        self._jobs: set[Job] = set()
        self.control = QThreadPool(self)
        self.control.setMaxThreadCount(1)
        self.clips = QThreadPool(self)
        self.clips.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self._shut = False

    # --- helpers -----------------------------------------------------------------
    def _submit(self, pool: QThreadPool, fn, done, failed) -> Job:
        job = Job(fn, pass_progress=False)
        self._jobs.add(job)
        job.signals.finished.connect(done)
        job.signals.failed.connect(failed)
        job.signals.finished.connect(lambda *_: self._jobs.discard(job))
        job.signals.failed.connect(lambda *_: self._jobs.discard(job))
        pool.start(job)
        return job

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def recording(self) -> bool:
        return self.rec is not None and self.rec.state in ("recording", "restarting")

    @property
    def before_s(self) -> float:
        return self.ctx.settings.capture.clip_before_s

    @property
    def after_s(self) -> float:
        return self.ctx.settings.capture.clip_after_s

    def _default_recorder(self, src: cap.CaptureSource, audio: cap.AudioDevice | None) -> LocalRingRecorder:
        inst = self.ctx.ffmpeg
        enc, _ = self.ctx.encoder_for_export()
        return LocalRingRecorder(
            inst.ffmpeg, cap.input_args(src, audio), self.ctx.paths.buffer_dir,
            buffer_s=self.ctx.settings.capture.buffer_s, segment_s=cap.SEGMENT_S, ffprobe=inst.ffprobe,
            output_args=cap.output_args(src, audio, ft.encoder_args(enc)), stall_timeout_s=STALL_TIMEOUT_S)

    @staticmethod
    def _first_line(msg: str) -> str:
        return msg.split("\n\n", 1)[0].strip()

    # --- sources -----------------------------------------------------------------
    def refresh_sources(self) -> None:
        if self._listing or self._shut:
            return
        self._listing = True

        def done(res):
            self._listing = False
            self.sources = res
            self.sources_changed.emit(res)

        def failed(msg):
            self._listing = False
            self.sources = cap.SourceList(dshow_error=self._first_line(msg))
            self.sources_changed.emit(self.sources)

        self._submit(self.control, self.enumerate_fn, done, failed)

    # --- start / stop ---------------------------------------------------------------
    def start(self, source: cap.CaptureSource | None, audio: cap.AudioDevice | None = None) -> str | None:
        """Begin capturing. Returns a message if it can't even be attempted (shown straight away)."""
        if self._busy:
            return "Capture is already starting or stopping."
        if self.recording:
            return "Capture is already running."
        if source is None:
            return "Choose a capture source first."
        if self.ctx.ffmpeg is None:
            return "FFmpeg isn't installed. Install it or set its location in Settings → FFmpeg."
        if self.pending:
            return "A clip from the last capture is still being saved. Start again when it has finished."
        self._set_busy(True)
        self.state_changed.emit("starting", f"Starting {source.label}…")

        def work():
            if source.kind == "dshow" or audio is not None:
                # The list may be stale: check the device (and audio device) are still connected.
                fresh = self.enumerate_fn()
                self.sources = fresh
                if fresh.dshow_error:
                    raise RingError(fresh.dshow_error)
                if source.kind == "dshow" and not fresh.find(source.key):
                    raise RingError(f"“{source.label.split(': ', 1)[-1]}” isn't connected any more. "
                                    "Plug it in, press Refresh, and try again.")
                if audio is not None and fresh.find_audio(audio.name) is None:
                    raise RingError(f"The audio device “{audio.name}” isn't connected any more. "
                                    "Press Refresh and choose another, or “No audio”.")
            rec = self.recorder_factory(source, audio)
            rec.on_state = lambda state, err: self.state_changed.emit(state, err or "")
            rec.start()
            return rec

        def done(rec):
            self.rec, self.source, self.audio = rec, source, audio
            self._set_busy(False)
            self.timer.start()
            self.state_changed.emit(rec.state, "")
            self._poll()

        def failed(msg):
            self._set_busy(False)
            self.state_changed.emit("failed", self._first_line(msg))

        self._submit(self.control, work, done, failed)
        return None

    def stop(self) -> str | None:
        if self._busy:
            return "Capture is already starting or stopping."
        rec = self.rec
        if rec is None or rec.state not in ("recording", "restarting", "failed", "ended"):
            return "Capture isn't running."
        self._set_busy(True)
        wait_until = max((p.window.end for p in self.pending.values()), default=None)
        self.state_changed.emit("stopping", "Stopping after the pending clip's last seconds are recorded…"
                                if wait_until is not None else "Stopping…")

        def work():
            if wait_until is not None and rec.state in ("recording", "restarting"):
                rec.wait_until(wait_until, timeout=self.after_s + 15)
            rec.stop()
            return rec.state

        def done(_):
            self._set_busy(False)
            self.timer.stop()
            self.state_changed.emit("stopped", "")
            self._poll()

        def failed(msg):
            self._set_busy(False)
            self.state_changed.emit("failed", self._first_line(msg))

        self._submit(self.control, work, done, failed)
        return None

    # --- status ---------------------------------------------------------------------
    def status(self) -> dict:
        rec = self.rec
        if rec is None:
            return {"state": "idle", "buffered_s": 0.0, "segments": 0, "error": None, "warning": None,
                    "pending": len(self.pending), "restarts": 0, "buffer_s": self.ctx.settings.capture.buffer_s}
        st = rec.status()
        st["pending"] = len(self.pending)
        st["buffer_s"] = rec.buffer_s
        return st

    def _poll(self) -> None:
        self.status_changed.emit(self.status())

    # --- Clip that ------------------------------------------------------------------
    def clip_that(self) -> tuple[bool, str]:
        """Clip [press − before, press + after]. Returns (accepted, message) straight away."""
        rec = self.rec
        if rec is None or not self.recording:
            return False, "Start capture first: Clip that saves footage from the rolling buffer."
        wall = time.monotonic()
        if wall - self._last_press < DOUBLE_PRESS_S:
            return False, "Already clipping that moment."
        if len(self.pending) >= MAX_PENDING_CLIPS:
            return False, f"{MAX_PENDING_CLIPS} clips are still being made. Wait for one to finish."
        now = rec.stream_now()
        if now is None:
            return False, "Capture has only just started; there's no footage in the buffer yet."
        before, after = self.before_s, self.after_s
        window = Window(now - before, now + after)
        oldest = rec.oldest_start()
        if oldest is None or oldest > window.start + 0.1:
            have = max(0.0, now - oldest) if oldest is not None else 0.0
            return False, (f"Only {have:.0f} s of footage is buffered, but this clip needs {before:g} s from before "
                           f"the moment. Try again in {max(1.0, before - have):.0f} s, or lower “Before the press” "
                           "in Settings → Capture. No clip was made.")
        try:
            newest = rec.newest_end()
            pre = rec.pin(Window(window.start, min(window.end, newest))) if newest > window.start + 0.2 else None
        except PreRollLost:
            return False, "The footage from before the moment has already left the buffer. No clip was made."
        except FootageMissing as exc:
            return False, (f"Recording restarted within the last {before:g} s ({exc}), so this moment can't be "
                           "clipped exactly. No clip was made.")
        self._last_press = wall
        token = self._next_token
        self._next_token += 1
        clip = PendingClip(token, window, time.time(), pre)
        self.pending[token] = clip
        msg = f"Clip #{token}: {before:g} s before + {after:g} s after — waiting for the last {after:g} s…"
        self.clip_queued.emit(token, msg)       # before _export: it can fail straight away (clips folder unusable)
        self._export(clip, rec)
        return True, msg

    def _export(self, clip: PendingClip, rec: LocalRingRecorder) -> None:
        ctx = self.ctx
        name = f"capture_{datetime.fromtimestamp(clip.pressed_at).strftime('%Y%m%d_%H%M%S')}.mp4"
        try:
            out = library.unique_destination(ctx.clips_dir, name, self._reserved)
        except OSError as exc:
            self._clip_failed(clip, f"Couldn't use the clips folder {ctx.clips_dir}: {exc.strerror or exc}")
            return
        enc, _ = ctx.encoder_for_export()

        def work():
            try:
                rec.export(clip.window, out, encoder=ft.encoder_args(enc) + ["-pix_fmt", "yuv420p"],
                           wait_s=self.after_s + 20.0)
            except FootageMissing as exc:
                if "in time" in str(exc):
                    raise RingError("Capture stopped (or the source was lost) before the seconds after the "
                                    "moment were recorded, so no clip was made.") from exc
                raise
            finally:
                if clip.pre_pin:
                    clip.pre_pin.release()
            info = ft.video_info(ctx.ffmpeg.ffprobe, out) if ctx.ffmpeg and ctx.ffmpeg.ffprobe else {"duration": None}
            return out, info

        def done(res):
            path, info = res
            self._reserved.discard(path.name)
            self.pending.pop(clip.token, None)
            clip_id = ctx.db.add_clip(path=str(path), orientation="landscape", window_start=clip.window.start,
                                      window_end=clip.window.end, duration_s=info.get("duration"), encoder=enc,
                                      source="capture")
            ctx.events.clips_changed.emit()
            self.clip_finished.emit(clip.token, clip_id, str(path))
            self._poll()

        def failed(msg):
            self._reserved.discard(out.name)
            self._clip_failed(clip, self._first_line(msg))

        self._submit(self.clips, work, done, failed)

    def _clip_failed(self, clip: PendingClip, msg: str) -> None:
        self.pending.pop(clip.token, None)
        if clip.pre_pin:
            clip.pre_pin.release()
        log.warning("capture clip #%s failed: %s", clip.token, msg)
        self.clip_failed.emit(clip.token, msg)
        self._poll()

    # --- shutdown --------------------------------------------------------------------
    def shutdown(self, timeout_ms: int = 15000) -> None:
        """App closing: stop recording, let running clips finish (bounded), remove buffer files."""
        self._shut = True
        self.timer.stop()
        rec = self.rec
        if rec is not None:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping capture on shutdown")
        self.clips.waitForDone(timeout_ms)
        self.control.waitForDone(timeout_ms)
        if rec is not None:
            rec.close()
