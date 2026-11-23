"""Mode D: local rolling buffer recorder with segment pinning.

ffmpeg's segment muxer writes short MPEG-TS segments (stream copy) plus a CSV list
of finished segments. A watcher thread indexes segments and deletes ones older
than the buffer window, unless a pending clip has pinned them. Exports read only
finished, pinned files, in a separate ffmpeg process at below-normal priority,
so they never block or slow the recorder.

Hardening:
  * one session per recorder and per buffer folder (a lock file the OS releases if
    the app dies); start/stop are idempotent and can be repeated
  * the watcher never dies on bad input: unreadable list lines, missing, empty,
    truncated or duplicate segments are rejected and counted, and a pinned window
    that would span a rejected segment or a restart is refused (gap), never exported
  * if ffmpeg exits unexpectedly (crash, disk full, capture device lost) the
    recorder restarts it with continued segment numbering, a few times with back-off;
    after that it reports `failed` with a readable error and can be started again
  * ffmpeg's stderr is drained (a full pipe would stall the recorder) and kept for
    error messages; stop asks ffmpeg to finish cleanly ("q") before terminating it
  * on Windows ffmpeg is placed in a kill-on-close job, so it dies with the app
    instead of recording on as an orphan
  * pins are handles: releasing one twice can't remove another clip's pin
  * exports write to temporary files, are checked with ffprobe (video present,
    expected length) and only then renamed into place; failures remove all
    temporary files and raise ExportError with a readable message
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..export.ffmpeg import BELOW_NORMAL_PRIORITY
from ..timing.window import Window

log = logging.getLogger(__name__)

TS_PACKET = 188
SEG_RE = re.compile(r"^seg_(\d+)\.ts$")
LOCK_NAME = "ring.lock"
TMP_DIR = "export_tmp"
GAP_TOLERANCE_S = 0.25
DISK_FULL_WINERRORS = {39, 112}          # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
ACCESS_WINERRORS = {5, 32, 33}           # access denied, sharing / lock violation
MAX_WALLS = 3600


# --- errors -------------------------------------------------------------------
class RingError(Exception):
    """A problem to show the user; str() is a readable message."""


class AlreadyRecording(RingError):
    pass


class RingBusy(RingError):
    """The buffer folder is in use (another session, or a clip still being saved)."""


class StorageError(RingError):
    pass


class PreRollLost(RingError):
    """The window starts before the oldest segment still in the buffer."""


class FootageMissing(RingError):
    """The window isn't (fully, continuously) in the buffer: not recorded yet, or a gap."""


class ExportError(RingError):
    pass


def is_disk_full(err: BaseException | str) -> bool:
    if isinstance(err, OSError):
        return err.errno == errno.ENOSPC or getattr(err, "winerror", None) in DISK_FULL_WINERRORS
    text = str(err).lower()
    return "no space left on device" in text or "disk is full" in text or "not enough space" in text


def _drive(folder: Path) -> str:
    return folder.drive or folder.anchor or str(folder)


def describe_storage_error(err: BaseException | str, folder: Path) -> str:
    if is_disk_full(err):
        return (f"The drive {_drive(folder)} is full, so TwitchClipper couldn't write to {folder}. "
                "Free up some space or choose another folder in Settings.")
    if isinstance(err, PermissionError) or getattr(err, "winerror", None) in ACCESS_WINERRORS \
            or "permission denied" in str(err).lower():
        return (f"Windows denied access to {folder}. Another program may be using a file there, "
                "or the folder is read-only.")
    detail = err.strerror if isinstance(err, OSError) and err.strerror else str(err)
    return f"Couldn't write to {folder}: {detail}"


def describe_ffmpeg_failure(stderr: str, returncode: int | None, folder: Path) -> str:
    text = stderr.strip()
    if is_disk_full(text) or "permission denied" in text.lower():
        return describe_storage_error(text, folder)
    last = text.splitlines()[-1] if text else "no error output"
    return f"FFmpeg stopped unexpectedly (exit code {returncode}): {last[-300:]}"


# --- data ---------------------------------------------------------------------
@dataclass
class Segment:
    seq: int
    path: Path
    start: float              # session timeline (seconds); restarts are offset to follow on
    end: float
    completed_wall: float
    pins: int = 0
    run: int = 0              # which ffmpeg process wrote it

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class RingStats:
    segments_seen: int = 0
    evicted: int = 0
    evict_blocked_by_pin: int = 0
    max_bytes: int = 0
    rejected: int = 0
    restarts: int = 0
    orphans_removed: int = 0
    delete_retries: int = 0
    watcher_errors: int = 0
    max_tick_s: float = 0.0
    completion_walls: list[float] = field(default_factory=list)


@dataclass
class _Run:
    index: int
    proc: subprocess.Popen | None      # dropped once the process has ended (frees its OS handle)
    list_path: Path
    offset: float                 # added to listed times to put them on the session timeline
    origin: float | None = None   # pts origin: listed time t is at pts (t + origin)
    read_pos: int = 0
    last_start: float = -math.inf
    stderr: deque = field(default_factory=lambda: deque(maxlen=40))
    reader: threading.Thread | None = None
    kill_on_exit: bool = False
    ended: bool = False
    returncode: int | None = None
    started_wall: float = field(default_factory=time.time)
    stalled: bool = False

    def stderr_text(self) -> str:
        return "\n".join(self.stderr)


class Pin:
    """Segments held for one clip. Release exactly once (extra releases do nothing)."""

    def __init__(self, rec: "LocalRingRecorder", window: Window, segments: list[Segment], origin: float | None):
        self.recorder = rec
        self.window = window
        self.segments = segments
        self.origin = origin
        self.released = False

    def release(self) -> None:
        self.recorder._release(self)

    def __enter__(self) -> "Pin":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


ACTIVE_STATES = ("recording", "restarting")


class LocalRingRecorder:
    """States: idle -> recording <-> restarting -> stopped | ended (input finished) | failed."""

    def __init__(self, ffmpeg: Path, input_args: list[str], workdir: Path,
                 buffer_s: float = 120.0, segment_s: float = 2.0, *,
                 ffprobe: Path | None = None, min_free_bytes: int = 500 * 1024 * 1024,
                 max_restarts: int = 3, restart_window_s: float = 60.0, restart_delay_s: float = 0.5,
                 poll_s: float = 0.1, on_state: Callable[[str, str | None], None] | None = None,
                 output_args: list[str] | None = None, stall_timeout_s: float | None = None):
        self.ffmpeg = Path(ffmpeg)
        self.ffprobe = Path(ffprobe) if ffprobe else self.ffmpeg.with_name("ffprobe" + self.ffmpeg.suffix)
        self.input_args = input_args
        self.dir = Path(workdir)
        self.buffer_s = buffer_s
        self.segment_s = segment_s
        self.min_free_bytes = min_free_bytes
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.restart_delay_s = restart_delay_s
        self.poll_s = poll_s
        self.on_state = on_state
        # Output side of the ffmpeg command. Default: stream copy of every input stream (files, or
        # sources that are already H.264/AAC). Live capture devices pass encoder options here.
        self.output_args = output_args
        # A live source that stops delivering (device unplugged, driver hang) can leave ffmpeg running
        # without writing anything. With a timeout set, no new segment for that long counts as a crash.
        self.stall_timeout_s = stall_timeout_s

        self.segments: list[Segment] = []
        self.lock = threading.Condition()        # segments, pins, state, runs
        self._life = threading.Lock()            # serialises start / stop / clear
        self._dir_lock_guard = threading.Lock()
        self._lock_file = None
        self.stats = RingStats()
        self.state = "idle"
        self.last_error: str | None = None
        self.warning: str | None = None
        self._runs: list[_Run] = []
        self._pins: set[Pin] = set()
        self._known: set[str] = set()
        self._pending_delete: set[Path] = set()
        self._next_seq = 0                       # next file number for ffmpeg
        self._accepted = 0                       # Segment.seq counter
        self._restart_times: deque[float] = deque()
        self._stop = threading.Event()
        self._stopping = False
        self._watcher: threading.Thread | None = None
        self._last_space_check = 0.0

    # --- public state -------------------------------------------------------
    @property
    def proc(self) -> subprocess.Popen | None:
        return self._runs[-1].proc if self._runs else None

    @property
    def origin(self) -> float | None:
        """pts origin of the first run (Phase 0 compatibility)."""
        return self._runs[0].origin if self._runs else None

    @property
    def list_path(self) -> Path:
        return self._runs[-1].list_path if self._runs else self.dir / "segments_000.csv"

    def running(self) -> bool:
        p = self.proc
        return self.state in ACTIVE_STATES and p is not None and p.poll() is None

    def pinned_count(self) -> int:
        with self.lock:
            return len(self._pins)

    def status(self) -> dict:
        with self.lock:
            segs = list(self.segments)
            return {
                "state": self.state, "error": self.last_error, "warning": self.warning,
                "segments": len(segs), "buffered_s": (segs[-1].end - segs[0].start) if segs else 0.0,
                "pins": len(self._pins), "restarts": self.stats.restarts, "rejected": self.stats.rejected,
            }

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        with self._life:
            if self.state in ACTIVE_STATES:
                raise AlreadyRecording("The rolling buffer is already recording.")
            self._join_watcher()
            with self.lock:
                if self._pins:
                    raise RingBusy("A clip from the last recording is still being saved. "
                                   "Start again when it has finished.")
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(describe_storage_error(exc, self.dir)) from exc
            self._acquire_dir_lock()
            try:
                self._check_free_space(raise_error=True)
                with self.lock:
                    self.segments = []
                    self.stats = RingStats()
                    self._runs = []
                    self._known = set()
                    self._accepted = 0
                    self._restart_times.clear()
                    self._stopping = False
                    self.last_error = None
                    self.warning = None
                self._stop = threading.Event()
                self._clean_dir()
                with self.lock:
                    self._spawn_run()
                self._set_state("recording")
                self._watcher = threading.Thread(target=self._watch, name="ring-watcher", daemon=True)
                self._watcher.start()
            except BaseException:
                with self.lock:
                    run = self._runs[-1] if self._runs else None
                if run:
                    self._end_process(run, graceful=False)
                    self._close_run(run)
                self._release_dir_lock()
                if self.state in ACTIVE_STATES:
                    self._set_state("failed")
                raise

    def stop(self, timeout: float = 10.0) -> None:
        """Stop recording; buffered segments stay available for export until clear() or start()."""
        with self._life:
            with self.lock:
                self._stopping = True
                run = self._runs[-1] if self._runs else None
                was = self.state
            if run:
                self._end_process(run, graceful=True, timeout=timeout)
            self._stop.set()
            self._join_watcher()
            with self.lock:
                run = self._runs[-1] if self._runs else None
            if run and not run.ended:
                self._finish_run(run)
            self._release_dir_lock()
            if was in ACTIVE_STATES:
                self._set_state("stopped")

    def close(self) -> None:
        """App shutdown: stop and remove buffer files (kept if a clip is still being saved)."""
        self.stop()
        try:
            self.clear()
        except RingBusy:
            log.info("buffer files kept: a clip is still being saved")

    def clear(self) -> None:
        with self._life:
            if self.state in ACTIVE_STATES:
                raise AlreadyRecording("Stop recording before clearing the buffer.")
            with self.lock:
                if self._pins:
                    raise RingBusy("A clip is still being saved from the buffer.")
                self.segments = []
            self._clean_dir()

    # --- internals: processes -------------------------------------------------
    def _command(self, list_path: Path, start_number: int) -> list[str]:
        return [
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error",
            *self.input_args,
            *(self.output_args or ["-map", "0", "-c", "copy"]),
            "-f", "segment", "-segment_time", str(self.segment_s), "-segment_format", "mpegts",
            "-segment_list", str(list_path), "-segment_list_type", "csv",
            "-segment_list_flags", "+live", "-segment_start_number", str(start_number),
            str(self.dir / "seg_%06d.ts"),
        ]

    def _spawn_run(self) -> _Run:
        """Start one ffmpeg process. Caller holds self.lock."""
        index = len(self._runs)
        list_path = self.dir / f"segments_{index:03d}.csv"
        list_path.unlink(missing_ok=True)
        if self._runs:
            prev = self._runs[-1]
            prev_end = self.segments[-1].end if self.segments else prev.offset
            last_wall = self.stats.completion_walls[-1] if self.stats.completion_walls else time.time()
            # Approximate: footage lost between the last finished segment and the restart.
            offset = prev_end + max(0.0, time.time() - last_wall)
        else:
            offset = 0.0
        cmd = self._command(list_path, self._next_seq)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except OSError as exc:
            raise RingError(f"Couldn't start FFmpeg ({self.ffmpeg}): {exc.strerror or exc}") from exc
        run = _Run(index, proc, list_path, offset)
        run.kill_on_exit = kill_with_parent(proc)
        run.reader = threading.Thread(target=_drain, args=(proc.stderr, run.stderr), daemon=True,
                                      name=f"ring-stderr-{index}")
        run.reader.start()
        self._runs.append(run)
        return run

    def _end_process(self, run: _Run, graceful: bool, timeout: float = 10.0) -> None:
        p = run.proc
        if p is None:
            return
        if p.poll() is None and graceful:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                p.wait(timeout=min(timeout, 5.0))
            except subprocess.TimeoutExpired:
                pass
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def _close_run(self, run: _Run) -> None:
        """Release a finished run's pipes, reader thread and process handle (keeps its exit code)."""
        p = run.proc
        if p is not None:
            for stream in (p.stdin, p.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        if run.reader and run.reader is not threading.current_thread():
            run.reader.join(timeout=2.0)
            if not run.reader.is_alive():
                run.reader = None
        if p is not None and p.poll() is not None:
            run.returncode = p.returncode
            run.proc = None       # a long session with many restarts mustn't pile up process handles
        run.ended = True

    def _finish_run(self, run: _Run) -> None:
        """Process has exited: index what it finished, drop unfinished files, release handles."""
        if run.reader:
            run.reader.join(timeout=2.0)     # collect the last error lines
        self._index(run)
        self._remove_orphans()
        self._close_run(run)

    def _join_watcher(self) -> None:
        w = self._watcher
        if w and w.is_alive() and w is not threading.current_thread():
            w.join(timeout=10.0)
        self._watcher = None

    # --- internals: folder ------------------------------------------------------
    def _acquire_dir_lock(self) -> None:
        with self._dir_lock_guard:
            if self._lock_file:
                return
            path = self.dir / LOCK_NAME
            try:
                f = open(path, "a+b")
            except OSError as exc:
                raise StorageError(describe_storage_error(exc, self.dir)) from exc
            try:
                f.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                raise RingBusy(f"Another recording is already using the buffer folder {self.dir}.") from None
            self._lock_file = f

    def _release_dir_lock(self) -> None:
        with self._dir_lock_guard:
            f, self._lock_file = self._lock_file, None
            if not f:
                return
            try:
                f.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            f.close()

    def _clean_dir(self) -> None:
        """Remove segments, lists and export temp files left by an earlier (possibly crashed) session."""
        leftovers: list[Path] = []
        targets = list(self.dir.glob("seg_*.ts")) + list(self.dir.glob("segments*.csv"))
        tmp = self.dir / TMP_DIR
        if tmp.is_dir():
            targets += [p for p in tmp.iterdir() if p.is_file()]
        for p in targets:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                leftovers.append(p)
        numbers = [int(m.group(1)) for p in leftovers if (m := SEG_RE.match(p.name))]
        with self.lock:
            self._next_seq = max(numbers) + 1 if numbers else 0
            self._pending_delete.update(leftovers)

    def _check_free_space(self, raise_error: bool) -> bool:
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            return True
        if free >= self.min_free_bytes:
            if not raise_error:
                self.warning = None
            return True
        msg = (f"Only {free / 1e6:.0f} MB free on {_drive(self.dir)}; the rolling buffer needs at least "
               f"{self.min_free_bytes / 1e6:.0f} MB. Free up some space or choose another buffer folder.")
        if raise_error:
            raise StorageError(msg)
        self.warning = msg
        return False

    def _delete(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:      # held by another program (antivirus, a player): try again later
            self._pending_delete.add(path)
            self.stats.delete_retries += 1

    def _retry_deletes(self) -> None:
        with self.lock:
            pending, self._pending_delete = self._pending_delete, set()
            for p in pending:
                self._delete(p)

    def _remove_orphans(self) -> None:
        """Delete segment files ffmpeg started but never finished (not in any list)."""
        with self.lock:
            live = {s.path.name for s in self.segments}
            for p in self.dir.glob("seg_*.ts"):
                m = SEG_RE.match(p.name)
                if m:
                    self._next_seq = max(self._next_seq, int(m.group(1)) + 1)
                if p.name not in self._known and p.name not in live:
                    self._delete(p)
                    self.stats.orphans_removed += 1

    # --- indexing / eviction ------------------------------------------------------
    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                with self.lock:
                    run = self._runs[-1] if self._runs else None
                if run is None or run.ended:
                    break
                tick = time.monotonic()
                self._index(run)
                self._retry_deletes()
                if tick - self._last_space_check > 2.0:
                    self._last_space_check = tick
                    self._check_free_space(raise_error=False)
                # Slowest indexing pass: tells a blocked watcher apart from ffmpeg not writing.
                self.stats.max_tick_s = max(self.stats.max_tick_s, time.monotonic() - tick)
                self._check_stall(run)
                if run.proc.poll() is not None and not self._stopping:
                    if not self._handle_exit(run):
                        break
            except Exception:  # noqa: BLE001 - the watcher must survive anything
                self.stats.watcher_errors += 1
                log.exception("ring watcher error")
            self._stop.wait(self.poll_s)

    def _handle_exit(self, run: _Run) -> bool:
        """ffmpeg exited on its own. Returns True to keep watching (restarted)."""
        self._finish_run(run)
        rc = run.returncode
        if rc == 0:
            self._release_dir_lock()
            self._set_state("ended")
            return False
        if run.stalled:
            self.last_error = (f"No new video for {self.stall_timeout_s:.0f} s, so recording was restarted. "
                               "The capture device may have been disconnected or taken by another program.")
        else:
            self.last_error = describe_ffmpeg_failure(run.stderr_text(), rc, self.dir)
        log.warning("ring recorder ffmpeg exited (%s): %s", rc, self.last_error)
        attempt = 0
        while not self._stop.is_set():
            now = time.monotonic()
            while self._restart_times and now - self._restart_times[0] > self.restart_window_s:
                self._restart_times.popleft()
            if len(self._restart_times) >= self.max_restarts:
                self._release_dir_lock()
                self._set_state("failed", self.last_error)
                return False
            self._set_state("restarting", self.last_error)
            self._stop.wait(min(5.0, self.restart_delay_s * (2 ** attempt)))
            attempt += 1
            self._restart_times.append(time.monotonic())
            if self._stop.is_set():
                return False
            if not self._check_free_space(raise_error=False):
                continue        # keep the original error (e.g. "drive is full"); self.warning has the numbers
            with self.lock:
                if self._stopping:
                    return False
                try:
                    new = self._spawn_run()
                except RingError as exc:
                    self.last_error = str(exc)
                    continue
                self.stats.restarts += 1
            # A process that dies straight away counts as another failed attempt.
            time.sleep(min(0.3, self.poll_s * 3))
            if new.proc.poll() is not None and new.proc.returncode != 0:
                self._finish_run(new)
                self.last_error = describe_ffmpeg_failure(new.stderr_text(), new.returncode, self.dir)
                continue
            self._set_state("recording")
            return True
        return False

    def _index(self, run: _Run) -> None:
        """Read new complete lines of this run's segment list."""
        try:
            with open(run.list_path, "rb") as f:
                f.seek(run.read_pos)
                chunk = f.read()
        except FileNotFoundError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        run.read_pos += end + 1
        for raw in chunk[: end + 1].splitlines():
            if raw.strip():
                self._add_entry(run, raw)

    def _reject(self, name: str, reason: str, delete: Path | None = None) -> None:
        self.stats.rejected += 1
        log.warning("ring: rejected segment %s (%s)", name, reason)
        if delete is not None:
            with self.lock:
                self._delete(delete)

    def _add_entry(self, run: _Run, raw: bytes) -> None:
        try:
            name, a, b = raw.decode("utf-8").strip().rsplit(",", 2)
            name = name.strip().strip('"')
            start, end = float(a), float(b)
        except ValueError:
            self._reject(raw[:60].decode("utf-8", "replace"), "unreadable list entry")
            return
        path = self.dir / name
        if not SEG_RE.match(name) or Path(name).name != name:
            self._reject(name, "unexpected file name")
            return
        if name in self._known:
            self._reject(name, "listed twice")
            return
        self._known.add(name)
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            self._reject(name, f"bad times {start}..{end}", delete=path)
            return
        if start < run.last_start - GAP_TOLERANCE_S:
            self._reject(name, "out of order", delete=path)
            return
        problem = _ts_problem(path)
        if problem:
            self._reject(name, problem, delete=path)
            return
        run.last_start = start
        if run.origin is None and not any(s.run == run.index for s in self.segments):
            try:
                # pts origin, measured from the run's first segment, whose listed start is exact
                # (later list entries run ~2 frames late).
                _, v0 = _starts(self.ffprobe, path, timeout=5.0)   # ~0.1 s normally; never stall indexing long
                run.origin = v0 - start
            except Exception as exc:  # noqa: BLE001
                log.warning("ring: couldn't probe %s for the pts origin: %s", name, exc)
        with self.lock:
            seg = Segment(self._accepted, path, start + run.offset, end + run.offset, time.time(), run=run.index)
            self._accepted += 1
            self.segments.append(seg)
            self.stats.segments_seen += 1
            self.stats.completion_walls.append(seg.completed_wall)
            if len(self.stats.completion_walls) > MAX_WALLS:
                del self.stats.completion_walls[: -MAX_WALLS]
            self._evict()
            self.lock.notify_all()

    def _evict(self) -> None:
        """Caller holds self.lock."""
        if not self.segments:
            return
        horizon = self.segments[-1].end - self.buffer_s
        keep: list[Segment] = []
        for s in self.segments:
            if s.end < horizon:
                if s.pins > 0:
                    self.stats.evict_blocked_by_pin += 1
                    keep.append(s)
                else:
                    self._delete(s.path)
                    self.stats.evicted += 1
            else:
                keep.append(s)
        self.segments = keep
        size = 0
        for s in keep:
            try:
                size += s.path.stat().st_size
            except OSError:
                pass
        self.stats.max_bytes = max(self.stats.max_bytes, size)

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self.lock:
            changed = state != self.state
            self.state = state
            if error is not None:
                self.last_error = error
            self.lock.notify_all()
        if changed and self.on_state:
            try:
                self.on_state(state, self.last_error)
            except Exception:  # noqa: BLE001
                log.exception("on_state callback failed")

    # --- clip support ---------------------------------------------------------------
    def _check_stall(self, run: _Run) -> None:
        if not self.stall_timeout_s or run.stalled or self._stopping or run.proc is None:
            return
        with self.lock:
            last = max([run.started_wall] + [s.completed_wall for s in self.segments[-1:] if s.run == run.index])
        if time.time() - last > self.stall_timeout_s and run.proc.poll() is None:
            log.warning("ring: no segment for %.0f s; killing ffmpeg", time.time() - last)
            run.stalled = True
            run.proc.kill()          # handled as an unexpected exit on the next pass (restart / fail)

    def stream_now(self) -> float | None:
        """Estimate the stream time being captured right now, for a live (real-time) source.

        Each finished segment gives completed_wall - end >= the true wall-clock offset of the
        timeline (indexing and encoder delay only ever add), so the smallest recent value is the
        best estimate. Returns None before the first segment.
        """
        with self.lock:
            recent = self.segments[-30:]
            if not recent:
                return None
            run = recent[-1].run
            offset = min(s.completed_wall - s.end for s in recent if s.run == run)
        return time.time() - offset

    def oldest_start(self) -> float | None:
        with self.lock:
            return self.segments[0].start if self.segments else None

    def newest_end(self) -> float:
        with self.lock:
            return self.segments[-1].end if self.segments else float("-inf")

    def wait_until(self, t: float, timeout: float) -> bool:
        """Block until footage up to stream time t has been captured (post-roll).

        Returns False on timeout, or straight away once recording has stopped or failed.
        """
        deadline = time.monotonic() + timeout
        with self.lock:
            while not self.segments or self.segments[-1].end < t:
                left = deadline - time.monotonic()
                if left <= 0 or self.state not in ACTIVE_STATES:
                    return False
                self.lock.wait(timeout=min(left, 0.5))
        return True

    def pin(self, window: Window) -> Pin:
        """Hold the segments covering `window` so eviction keeps them until the Pin is released."""
        margin = 0.5  # list times can be off by a couple of frames; pin a little extra
        with self.lock:
            segs = [s for s in self.segments if s.end > window.start - margin and s.start < window.end + margin]
            if not segs or segs[0].start > window.start + 0.1:
                raise PreRollLost(f"window starts at {window.start:.2f}s but oldest buffered is "
                                  f"{self.segments[0].start if self.segments else float('nan'):.2f}s")
            if segs[-1].end < window.end - 0.1:
                raise FootageMissing(f"footage up to {window.end:.2f}s hasn't been recorded yet "
                                     f"(newest is {segs[-1].end:.2f}s)")
            for a, b in zip(segs, segs[1:]):
                if b.run != a.run or abs(b.start - a.end) > GAP_TOLERANCE_S:
                    raise FootageMissing(f"the footage has a gap at {a.end:.2f}s (recording restarted "
                                         "or a damaged segment was dropped), so this clip can't be cut exactly")
            for s in segs:
                s.pins += 1
            run = self._runs[segs[0].run] if segs[0].run < len(self._runs) else None
            origin = None if run is None or run.origin is None else run.origin - run.offset
            pin = Pin(self, window, segs, origin)
            self._pins.add(pin)
            return pin

    def unpin(self, pin: Pin) -> None:
        pin.release()

    def _release(self, pin: Pin) -> None:
        with self.lock:
            if pin.released:
                return
            pin.released = True
            self._pins.discard(pin)
            for s in pin.segments:
                s.pins = max(0, s.pins - 1)
            self._evict()
            self.lock.notify_all()

    def export(self, window: Window, out: Path, *, precise: bool = True, encoder: list[str] | None = None,
               wait_s: float = 0.0) -> float:
        """Wait for the post-roll (optional), pin, cut, release. The pin is always released."""
        if wait_s and not self.wait_until(window.end, wait_s):
            raise FootageMissing(f"footage up to {window.end:.2f}s wasn't recorded in time")
        with self.pin(window) as pin:
            return export_clip(self.ffmpeg, pin.segments, window, out, precise=precise, encoder=encoder,
                               origin=pin.origin, tmp_dir=self.dir / TMP_DIR, ffprobe=self.ffprobe)


# --- helpers ----------------------------------------------------------------------------
def _drain(stream, sink: deque) -> None:
    try:
        for line in iter(stream.readline, b""):
            sink.append(line.decode("utf-8", "replace").rstrip())
    except (OSError, ValueError):
        pass


def _ts_problem(path: Path) -> str | None:
    """Cheap integrity check of a finished MPEG-TS segment (None if it looks whole)."""
    try:
        size = path.stat().st_size
        if size == 0:
            return "empty file"
        if size % TS_PACKET:
            return f"truncated ({size} bytes is not whole 188-byte packets)"
        with open(path, "rb") as f:
            if f.read(1) != b"\x47":
                return "not MPEG-TS data"
    except FileNotFoundError:
        return "file missing"
    except OSError as exc:
        return f"unreadable ({exc.strerror or exc})"
    return None


def _starts(ffprobe: Path, path: Path, timeout: float = 30.0) -> tuple[float, float]:
    """(format start_time, video stream start_time) in seconds."""
    r = subprocess.run([str(ffprobe), "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", str(path)], capture_output=True, text=True, timeout=timeout)
    data = json.loads(r.stdout)
    fmt = float(data["format"].get("start_time", 0.0))
    v = next((float(s.get("start_time", fmt)) for s in data["streams"] if s["codec_type"] == "video"), fmt)
    return fmt, v


def _probe_output(ffprobe: Path, path: Path) -> tuple[bool, float]:
    """(has a video stream, video duration in seconds) of a finished export.

    Uses the video stream's own duration: a file whose video stopped early can still report
    the full length at container level because its audio runs on.
    """
    r = subprocess.run([str(ffprobe), "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", str(path)], capture_output=True, text=True, timeout=30)
    data = json.loads(r.stdout or "{}")
    fmt = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    video = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        return False, fmt
    v = video[0].get("duration")
    return True, min(fmt, float(v)) if v not in (None, "N/A") else fmt


def _copy_into(segs: list[Segment], dst: Path) -> None:
    """Join MPEG-TS segments byte for byte. Separate function so tests can simulate a full disk."""
    with open(dst, "wb") as out:
        for s in segs:
            with open(s.path, "rb") as src:
                while chunk := src.read(1 << 20):
                    out.write(chunk)


def _place(tmp: Path, out: Path) -> None:
    try:
        os.replace(tmp, out)
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:   # not "different drive"
            raise
        staged = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.part{out.suffix}")
        try:
            shutil.copyfile(tmp, staged)
            os.replace(staged, out)
        finally:
            staged.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)


def export_clip(ffmpeg: Path, segs: list[Segment], window: Window, out: Path,
                precise: bool = True, encoder: list[str] | None = None, origin: float | None = None,
                *, tmp_dir: Path | None = None, ffprobe: Path | None = None) -> float:
    """Cut `window` from pinned segments into `out`. Returns seconds taken. Runs at below-normal priority.

    `out` only appears once the clip is complete and checked; on any failure every temporary
    file is removed and ExportError says what went wrong. Segment files are only read.

    Phase 0 findings this implementation addresses:
      * ffmpeg's concat demuxer opens small timestamp gaps at each segment boundary
        (cut ~0.9 s off 27 s clips) -> join MPEG-TS segments byte-for-byte instead.
      * MPEG-TS input seeking is approximate (can land after the keyframe and lose ~1 s)
        -> precise mode seeks on the output side (decode + discard; the file is short).
      * Segments start with audio slightly ahead of the first video frame.
      * The segment list's start times run ~2 frames late after the first segment
        (B-frame reorder delay) -> pass `origin` (the recorder's measured pts origin)
        so cuts are computed from real video timestamps, not the list.
    """
    ffmpeg = Path(ffmpeg)
    ffprobe = Path(ffprobe) if ffprobe else ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not segs:
        raise ExportError("There's no buffered footage for this clip.")
    folder = out.parent
    tmp_dir = tmp_dir or folder
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(describe_storage_error(exc, folder)) from exc
    token = uuid.uuid4().hex[:8]
    joined = tmp_dir / f"{out.stem}.{token}.joined.ts"
    part = tmp_dir / f"{out.stem}.{token}.part{out.suffix or '.mp4'}"
    t0 = time.perf_counter()
    try:
        missing = [s.path.name for s in segs if not s.path.exists()]
        if missing:
            raise ExportError(f"Buffered footage for this clip was deleted before it could be saved ({missing[0]}).")
        try:
            _copy_into(segs, joined)
        except OSError as exc:
            raise ExportError(describe_storage_error(exc, tmp_dir)) from exc
        try:
            fmt_start, v_start = _starts(ffprobe, joined)
        except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
            raise ExportError(f"The buffered footage for this clip couldn't be read ({exc!r}).") from exc
        if origin is not None:
            # Exact: content time t is at pts (t + origin); output -ss is relative to the file start.
            ss = window.start + origin - fmt_start
        else:
            ss = (window.start - segs[0].start) + (v_start - fmt_start)
        base = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if precise:
            cmd = base + ["-i", str(joined), "-ss", f"{max(0.0, ss):.3f}", "-t", f"{window.length:.3f}"]
            cmd += (encoder or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]) + ["-c:a", "aac", "-b:a", "128k"]
        else:
            # Copy mode can only cut on keyframes; approximate by design.
            cmd = base + ["-ss", f"{max(0.0, ss):.3f}", "-i", str(joined), "-t", f"{window.length:.3f}", "-c", "copy"]
        cmd += ["-movflags", "+faststart", "-f", "mp4", str(part)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=BELOW_NORMAL_PRIORITY)
        except OSError as exc:
            raise ExportError(f"Couldn't start FFmpeg ({ffmpeg}): {exc.strerror or exc}") from exc
        kill_with_parent(proc)
        _, err = proc.communicate()
        if proc.returncode != 0:
            raise ExportError(describe_ffmpeg_failure(err.decode("utf-8", "replace"), proc.returncode, folder))
        try:
            has_video, duration = _probe_output(ffprobe, part)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ExportError(f"The saved clip couldn't be checked, so it was discarded: {exc}") from exc
        need = window.length - 0.25 if precise else window.length * 0.5
        if not has_video or duration < need:
            raise ExportError(f"The saved clip was incomplete ({duration:.2f} s of {window.length:.2f} s"
                              f"{'' if has_video else ', no video'}), so it was discarded.")
        try:
            _place(part, out)
        except OSError as exc:
            raise ExportError(describe_storage_error(exc, folder)) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        joined.unlink(missing_ok=True)
    return time.perf_counter() - t0


# --- Windows: child processes die with the app ---------------------------------------------
_job_handle = None
_job_lock = threading.Lock()


def kill_with_parent(proc: subprocess.Popen) -> bool:
    """Put `proc` in a kill-on-close job object, so Windows ends it if this app exits or crashes.

    Returns False where unsupported (then stop()/close() are the only cleanup).
    """
    global _job_handle
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        with _job_lock:
            if _job_handle is None:
                class Basic(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                                ("SchedulingClass", wintypes.DWORD)]

                class Io(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

                class Extended(ctypes.Structure):
                    _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

                job = k32.CreateJobObjectW(None, None)
                if not job:
                    return False
                info = Extended()
                info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    return False
                _job_handle = job
            return bool(k32.AssignProcessToJobObject(_job_handle, int(proc._handle)))
    except Exception:  # noqa: BLE001
        log.debug("couldn't attach ffmpeg to a kill-on-close job", exc_info=True)
        return False
