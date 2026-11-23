"""Mode D local ring buffer (sources/local_ring.py): lifecycle, segments, pinning, export while
recording, storage and FFmpeg failures, recovery.

Real-FFmpeg tests record generated clock-strip footage (tests/support/footage.py) played at 4x
real time with 0.5 s keyframes and segments, so a few seconds of test time cover tens of seconds of
"stream". Failure tests use tests/support/fake_ffmpeg.py, which writes segments and list lines like
ffmpeg's segment muxer and misbehaves on cue. Nothing touches Twitch or the test_media fixture;
every recording lives in tmp_path.
"""

from __future__ import annotations

import ctypes
import gc
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from tests.support.footage import decode_file, generate
from tests.support.helpers import wait_for
from twitchclipper.export import ffmpeg as ft
from twitchclipper.sources import local_ring as lr
from twitchclipper.sources.local_ring import (
    AlreadyRecording, ExportError, FootageMissing, LocalRingRecorder, PreRollLost, RingBusy, RingError,
    StorageError, export_clip,
)
from twitchclipper.timing.window import Window

FAKE = Path(__file__).parent / "support" / "fake_ffmpeg.py"
SRC = Path(__file__).resolve().parents[1] / "src"
FPS = 30
FRAME_MS = 1000 / FPS
Usage = namedtuple("Usage", "total used free")


# --- helpers -------------------------------------------------------------------------
def ring_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ring-") and t.is_alive()]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def seg_files(d: Path) -> set[str]:
    return {p.name for p in d.glob("seg_*.ts") if p.is_file()}


def indexed(rec) -> set[str]:
    with rec.lock:
        return {s.path.name for s in rec.segments}


def process_alive(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)     # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102            # WAIT_TIMEOUT: still running
    finally:
        k32.CloseHandle(h)


def handle_count() -> int:
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    n = ctypes.c_uint32()
    k32.GetProcessHandleCount(ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(n))
    return n.value


class FakeRing(LocalRingRecorder):
    """The real recorder, with tests/support/fake_ffmpeg.py in place of ffmpeg."""

    def _command(self, list_path, start_number):
        return [sys.executable, str(FAKE)] + super()._command(list_path, start_number)[1:]


@pytest.fixture(scope="session")
def ring_source(tmp_path_factory, ffmpeg_install):
    d = tmp_path_factory.mktemp("ring_src")
    return generate(ffmpeg_install.ffmpeg, d / "clock.mp4", 30.0, fps=FPS, width=640, height=360, keyframe_s=0.5)


@pytest.fixture
def make_ring(tmp_path, ffmpeg_install, ring_source):
    made = []

    def make(buffer_s=30.0, workdir=None, loop=True, **kw):
        args = ["-readrate", "4"] + (["-stream_loop", "-1"] if loop else []) + ["-i", str(ring_source)]
        kw.setdefault("min_free_bytes", 50 * 1024 * 1024)
        kw.setdefault("restart_delay_s", 0.2)
        rec = LocalRingRecorder(ffmpeg_install.ffmpeg, args, workdir or tmp_path / "buffer",
                                buffer_s=buffer_s, segment_s=0.5, ffprobe=ffmpeg_install.ffprobe, **kw)
        made.append(rec)
        return rec

    yield make
    for r in made:
        for p in list(r._pins):
            p.release()
        r.stop()


@pytest.fixture
def make_fake(tmp_path, monkeypatch, ffmpeg_install):
    made = []

    def make(plan: dict, buffer_s=30.0, workdir=None, **kw):
        plan_path = tmp_path / f"plan{len(made)}.json"
        plan_path.write_text(json.dumps(plan))
        monkeypatch.setenv("FAKE_FFMPEG_PLAN", str(plan_path))
        kw.setdefault("min_free_bytes", 1)
        kw.setdefault("restart_delay_s", 0.1)
        kw.setdefault("poll_s", 0.02)
        rec = FakeRing(ffmpeg_install.ffmpeg, ["-i", "fake"], workdir or tmp_path / "buffer",
                       buffer_s=buffer_s, segment_s=0.5, ffprobe=ffmpeg_install.ffprobe, **kw)
        rec.plan_path = plan_path
        made.append(rec)
        return rec

    yield make
    for r in made:
        for p in list(r._pins):
            p.release()
        r.stop()


def set_plan(rec, plan: dict) -> None:
    rec.plan_path.write_text(json.dumps(plan))
    rec.plan_path.with_suffix(".count").unlink(missing_ok=True)


def clock_offset_ms(rec, ffmpeg) -> float:
    """Clock value of stream time 0 (calibrated from the first segment, as in Phase 0)."""
    with rec.pin(Window(0.0, 0.3)) as p:
        first = p.segments[0]
        d0 = decode_file(ffmpeg, first.path)
    return (d0.first_ms or 0) - first.start * 1000.0


def check_clip(ffmpeg, ffprobe, out: Path, window: Window, offset_ms: float) -> None:
    dec = decode_file(ffmpeg, out)
    info = ft.probe(ffprobe, out)
    kinds = {s["codec_type"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - window.length) <= 0.1
    assert dec.decode_rate == 1.0 and dec.monotonic
    assert abs(dec.frames - window.length * FPS) <= 1
    assert abs(dec.first_ms - (window.start * 1000 + offset_ms)) <= FRAME_MS + 1
    assert abs(dec.last_ms - (window.end * 1000 + offset_ms - FRAME_MS)) <= FRAME_MS + 1


# --- 1. start and stop ----------------------------------------------------------------
def test_start_stop_releases_everything(make_ring, tmp_path):
    rec = make_ring()
    assert rec.state == "idle"
    rec.start()
    assert rec.state == "recording" and rec.running()
    assert wait_for(lambda: rec.stats.segments_seen >= 4)
    proc, run = rec.proc, rec._runs[-1]
    assert run.kill_on_exit                      # Windows job: ffmpeg can't outlive the app
    assert {t.name for t in ring_threads()} >= {"ring-watcher", "ring-stderr-0"}

    rec.stop()
    assert rec.state == "stopped" and not rec.running()
    assert proc.returncode == 0                  # finished cleanly on "q", not killed
    assert ring_threads() == []
    assert proc.stdin.closed and proc.stderr.closed
    assert rec._lock_file is None
    assert seg_files(rec.dir) == indexed(rec)    # last segment finished and listed; no partial file left
    assert rec.status()["segments"] == rec.stats.segments_seen
    # the folder lock is free again
    other = make_ring(workdir=rec.dir)
    other.start()
    other.stop()


def test_duplicate_sessions_are_refused(make_ring):
    rec = make_ring()
    rec.start()
    proc = rec.proc
    with pytest.raises(AlreadyRecording):
        rec.start()
    other = make_ring(workdir=rec.dir)           # second recorder, same buffer folder
    with pytest.raises(RingBusy, match="already using the buffer folder"):
        other.start()
    assert other.proc is None and other.state == "idle"
    assert rec.proc is proc and rec.running() and len(rec._runs) == 1
    rec.stop()


def test_concurrent_start_calls_start_one_session(make_ring):
    rec = make_ring()
    results = []
    barrier = threading.Barrier(5)

    def go():
        barrier.wait()
        try:
            rec.start()
            results.append("ok")
        except AlreadyRecording:
            results.append("refused")

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok"] + ["refused"] * 4
    assert len(rec._runs) == 1 and rec.running()
    rec.stop()


def test_stop_before_start_and_repeated_stop(make_ring):
    rec = make_ring()
    rec.stop()
    rec.stop()
    assert rec.state == "idle" and rec.proc is None
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 2)
    rec.stop()
    rec.stop()
    assert rec.state == "stopped" and ring_threads() == []


def test_repeated_start_stop_cycles(make_ring):
    rec = make_ring()
    codes, pids, handles = [], set(), []          # not the Popen objects: each holds 4 OS handles
    for cycle in range(6):
        rec.start()
        assert wait_for(lambda: rec.stats.segments_seen >= 3), f"cycle {cycle}"
        with rec.lock:
            first = rec.segments[0]
        assert first.seq == 0 and first.start == 0.0 and first.path.name == "seg_000000.ts"
        pids.add(rec.proc.pid)
        rec.stop()
        codes.append(rec._runs[-1].returncode)
        assert ring_threads() == []
        assert seg_files(rec.dir) == indexed(rec)
        gc.collect()
        handles.append(handle_count())
    assert codes == [0] * 6 and len(pids) == 6                   # six separate ffmpeg runs, each ended cleanly
    assert handles[-1] - handles[1] <= 6, handles                # no handle leak per session (measured: flat)


def test_missing_ffmpeg_is_reported_and_start_works_later(make_ring, tmp_path):
    rec = make_ring()
    good = rec.ffmpeg
    rec.ffmpeg = tmp_path / "no-such-ffmpeg.exe"
    with pytest.raises(RingError, match="Couldn't start FFmpeg"):
        rec.start()
    assert rec.state != "recording" and rec._lock_file is None and ring_threads() == []
    rec.ffmpeg = good
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 2)
    rec.stop()


# --- 2. segment management ------------------------------------------------------------
def test_segments_are_ordered_contiguous_and_whole(make_ring, ffmpeg_install):
    rec = make_ring()
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 8.0)
    rec.stop()
    segs = list(rec.segments)
    assert [s.seq for s in segs] == list(range(len(segs)))
    assert [s.path.name for s in segs] == [f"seg_{i:06d}.ts" for i in range(len(segs))]
    assert segs[0].start == 0.0
    for a, b in zip(segs, segs[1:]):
        assert abs(b.start - a.end) < 0.01                       # no gaps, no overlaps
        assert b.completed_wall >= a.completed_wall
    assert all(0.4 <= s.duration <= 1.1 for s in segs[:-1])     # cut on the 0.5 s keyframes (last: cut by stop)
    assert all(lr._ts_problem(s.path) is None for s in segs)
    assert rec.stats.rejected == 0 and rec.stats.watcher_errors == 0
    info = ft.probe(ffmpeg_install.ffprobe, segs[3].path)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert rec.origin is not None                                # pts origin measured for exact cuts


def test_retention_removes_old_unpinned_segments(make_ring):
    rec = make_ring(buffer_s=3.0)
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 30)
    with rec.lock:                                               # while recording: bounded
        span = rec.segments[-1].end - rec.segments[0].start
        assert rec.stats.evicted == rec.stats.segments_seen - len(rec.segments) > 0
    assert span <= 3.0 + 1.1
    rec.stop()
    with rec.lock:
        segs = list(rec.segments)
    assert segs[-1].end - segs[0].start <= 3.0 + 1.1
    assert seg_files(rec.dir) == indexed(rec)                    # evicted files are gone from disk
    assert "seg_000000.ts" not in seg_files(rec.dir)


def test_pinned_segments_survive_retention_until_released(make_ring):
    rec = make_ring(buffer_s=2.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 3.0)
    pin = rec.pin(Window(0.5, 1.5))
    names = [s.path.name for s in pin.segments]
    assert wait_for(lambda: rec.newest_end() >= 10.0)
    rec.stop()
    assert all((rec.dir / n).exists() for n in names)
    assert set(names) <= indexed(rec)
    assert rec.stats.evict_blocked_by_pin > 0
    after = [s for s in rec.segments if s.start >= 2.5 and s.end < rec.newest_end() - 2.0]
    assert not after                                             # unpinned neighbours were evicted
    pin.release()
    assert not any((rec.dir / n).exists() for n in names)
    assert not set(names) & indexed(rec)


def test_overlapping_pins_and_double_release(make_ring):
    # A 2 s buffer left only ~0.2 s of real time to pin before the window could be evicted, and the
    # watcher's first-segment ffprobe can take longer than that on a busy PC.
    rec = make_ring(buffer_s=6.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 2.2, step=0.005)
    a = rec.pin(Window(0.5, 2.0))
    assert wait_for(lambda: rec.newest_end() >= 4.2, step=0.005)
    b = rec.pin(Window(2.5, 4.0))                                # overlaps a by a segment or two (pin margin)
    assert wait_for(lambda: rec.newest_end() >= 12.0)            # both windows are now past the buffer
    rec.stop()
    shared = [s for s in a.segments if s in b.segments]
    only_a = [s for s in a.segments if s not in b.segments]
    assert shared and only_a
    assert all(s.pins == 2 for s in shared)
    a.release()
    a.release()                                                  # second release must not touch b's pins
    rec.unpin(a)
    assert all(s.pins == 1 for s in shared)
    assert all(s.path.exists() for s in b.segments)
    assert not any(s.path.exists() for s in only_a)
    assert rec.pinned_count() == 1
    with b:
        pass
    assert rec.pinned_count() == 0
    assert all(s.pins == 0 for s in rec.segments)
    assert not any(s.path.exists() for s in b.segments)


def test_pin_refuses_lost_preroll_and_unrecorded_footage(make_ring):
    rec = make_ring(buffer_s=2.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 8.0)
    with pytest.raises(PreRollLost):
        rec.pin(Window(0.0, 2.0))                                # left the 2 s buffer long ago
    newest = rec.newest_end()
    with pytest.raises(FootageMissing, match="hasn't been recorded yet"):
        rec.pin(Window(newest - 1.0, newest + 30.0))
    assert rec.pinned_count() == 0 and all(s.pins == 0 for s in rec.segments)
    rec.stop()


def test_damaged_and_duplicate_segments_are_rejected(make_fake):
    rec = make_fake({"runs": [{"segments": 14, "truncated": [3], "bad_sync": [5], "empty": [7],
                               "missing": [9], "duplicate": [1], "garbage_after": 10, "exit": 0}]})
    rec.start()
    assert wait_for(lambda: rec.state == "ended")
    names = indexed(rec)
    assert rec.stats.rejected == 6
    for bad in (3, 5, 7, 9):
        assert f"seg_{bad:06d}.ts" not in names
        assert not (rec.dir / f"seg_{bad:06d}.ts").exists()      # damaged files deleted, never exported
    assert names == {f"seg_{i:06d}.ts" for i in range(14) if i not in (3, 5, 7, 9)}
    assert rec.stats.watcher_errors == 0
    assert rec.origin is None                                    # unprobeable data doesn't stop indexing
    with pytest.raises(FootageMissing, match="gap"):
        rec.pin(Window(1.2, 2.2))                                # spans dropped segment 3 (1.5-2.0 s)
    with rec.pin(Window(5.0, 6.8)) as p:                         # 10..13 are clean: segments 10-13 cover 5.0-7.0
        assert [s.path.name for s in p.segments][0] == "seg_000010.ts"


# --- 3. export while recording --------------------------------------------------------
def test_export_during_recording_is_exact_and_leaves_segments_alone(make_ring, ffmpeg_install, tmp_path):
    ff, fp = ffmpeg_install.ffmpeg, ffmpeg_install.ffprobe
    rec = make_ring(buffer_s=60.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 7.0)
    offset = clock_offset_ms(rec, ff)
    window = Window(2.0, 5.0)
    guard = rec.pin(window)
    before = {s.path: sha(s.path) for s in guard.segments}
    seen_before = rec.stats.segments_seen
    out = tmp_path / "clips" / "ring_clip.mp4"
    result = {}

    def run():
        try:
            result["s"] = rec.export(window, out)
        except Exception as exc:  # noqa: BLE001
            result["err"] = exc

    t = threading.Thread(target=run)
    t.start()
    alive_during = []
    while t.is_alive():
        alive_during.append(rec.running())
        time.sleep(0.02)
    t.join()
    assert "err" not in result, result.get("err")
    assert all(alive_during) and rec.running()
    assert rec.stats.segments_seen > seen_before                 # recording carried on during the export
    check_clip(ff, fp, out, window, offset)
    assert {p: sha(p) for p in before} == before                 # segments only read
    assert rec.pinned_count() == 1                               # export released its own pin
    assert list((rec.dir / lr.TMP_DIR).iterdir()) == []
    assert sorted(p.name for p in out.parent.iterdir()) == ["ring_clip.mp4"]
    # approximate (stream copy) mode also produces a playable file (inside the guarded range, so a
    # slow machine can't let the footage roll out of the 60 s buffer first)
    copy_out = tmp_path / "clips" / "copy.mp4"
    rec.export(Window(2.5, 5.0), copy_out, precise=False)
    assert decode_file(ff, copy_out).decode_rate == 1.0
    guard.release()
    rec.stop()


def test_concurrent_exports_do_not_stall_recording(make_ring, ffmpeg_install, tmp_path):
    ff, fp = ffmpeg_install.ffmpeg, ffmpeg_install.ffprobe
    rec = make_ring(buffer_s=60.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 8.0)
    offset = clock_offset_ms(rec, ff)
    windows = {"a": Window(2.0, 5.0), "b": Window(3.5, 6.5), "c": Window(4.0, 7.0)}
    guard = rec.pin(Window(2.0, 7.0))           # a slow machine mustn't let the footage roll out first
    errors = []
    start_count = rec.stats.segments_seen

    def run(name, w):
        try:
            rec.export(w, tmp_path / f"{name}.mp4")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=kv) for kv in windows.items()]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    walls = [w for w in rec.stats.completion_walls if w >= t0]
    gaps = [b - a for a, b in zip(walls, walls[1:])]
    assert rec.stats.segments_seen > start_count and gaps and max(gaps) < 1.0
    for name, w in windows.items():
        check_clip(ff, fp, tmp_path / f"{name}.mp4", w, offset)
    assert rec.pinned_count() == 1               # only the guard: every export released its pin
    guard.release()
    rec.stop()


def test_ring_export_plays_in_the_app_preview(qtbot, make_ring, tmp_path):
    from PySide6.QtMultimedia import QAudioBufferOutput
    from twitchclipper.ui.widgets.video_preview import VideoPreview

    rec = make_ring()
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 5.0)
    out = tmp_path / "preview.mp4"
    rec.export(Window(1.0, 4.0), out)
    rec.stop()
    w = VideoPreview()
    qtbot.addWidget(w)
    w.show()
    frames, audio = [], []
    w.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(f))
    tap = QAudioBufferOutput()
    w.player.setAudioBufferOutput(tap)
    tap.audioBufferReceived.connect(lambda b: audio.append(b))
    w.load(out)
    w.player.play()
    qtbot.waitUntil(lambda: w.player.position() > 1000, timeout=10000)
    assert len(frames) > 10 and audio
    w.release()
    out.unlink()                                                 # released: deletable straight away


# --- 4. storage failures ----------------------------------------------------------------
def test_start_refused_when_disk_is_low_then_recovers(make_ring, monkeypatch):
    rec = make_ring(min_free_bytes=500 * 1024 * 1024)
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 10**12 - 100 * 10**6, 100 * 10**6))
    with pytest.raises(StorageError, match=r"Only 100 MB free on [A-Z]:.*needs at least 524 MB"):
        rec.start()
    assert rec.proc is None and rec._lock_file is None and ring_threads() == []
    monkeypatch.setattr(shutil, "disk_usage", real)
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 2)
    rec.stop()


def test_disk_full_while_saving_a_clip(make_ring, ffmpeg_install, tmp_path, monkeypatch):
    ff, fp = ffmpeg_install.ffmpeg, ffmpeg_install.ffprobe
    rec = make_ring(buffer_s=60.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 6.0)
    offset = clock_offset_ms(rec, ff)
    window = Window(1.0, 4.0)
    guard = rec.pin(window)
    before = {s.path: sha(s.path) for s in guard.segments}
    real_copy = lr._copy_into

    def full_disk(segs, dst):
        with open(dst, "wb") as f:
            f.write(b"\x47" * 188 * 50)
        raise OSError(28, "No space left on device")             # ENOSPC halfway through

    monkeypatch.setattr(lr, "_copy_into", full_disk)
    out = tmp_path / "clips" / "full.mp4"
    with pytest.raises(ExportError, match=r"The drive [A-Z]: is full"):
        rec.export(window, out)
    assert not out.exists()
    assert list((rec.dir / lr.TMP_DIR).iterdir()) == []          # no half-written temp files
    assert {p: sha(p) for p in before} == before and rec.running()
    assert rec.pinned_count() == 1                               # the failed export released its pin
    monkeypatch.setattr(lr, "_copy_into", real_copy)             # space is back
    rec.export(window, out)
    check_clip(ff, fp, out, window, offset)
    guard.release()
    rec.stop()


def test_failed_or_incomplete_exports_are_never_presented(make_ring, ffmpeg_install, tmp_path):
    rec = make_ring(buffer_s=60.0)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 6.0)
    window = Window(1.0, 4.0)
    out = tmp_path / "clips" / "bad.mp4"
    with pytest.raises(ExportError, match="FFmpeg stopped unexpectedly"):
        rec.export(window, out, encoder=["-c:v", "no_such_encoder"])
    assert not out.exists()
    # ffmpeg "succeeds" but writes only 5 frames: caught by the length check
    with pytest.raises(ExportError, match=r"incomplete \(0\.\d+ s of 3\.00 s\)"):
        rec.export(window, out, encoder=["-c:v", "libx264", "-frames:v", "5"])
    assert not out.exists()
    assert list((rec.dir / lr.TMP_DIR).iterdir()) == [] and list(out.parent.iterdir()) == []
    # the clips folder can't be created (a file is in the way)
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    with pytest.raises(ExportError, match="Couldn't write to|denied access"):
        rec.export(window, blocker / "clip.mp4")
    assert rec.pinned_count() == 0 and rec.running()
    rec.stop()


def test_export_refuses_segments_deleted_underneath(make_ring, tmp_path):
    rec = make_ring()
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 4.0)
    rec.stop()
    with rec.pin(Window(1.0, 3.0)) as p:
        segs = [lr.Segment(s.seq, s.path, s.start, s.end, s.completed_wall) for s in p.segments]
    segs[1] = lr.Segment(99, tmp_path / "gone.ts", segs[1].start, segs[1].end, 0.0)
    with pytest.raises(ExportError, match="deleted before it could be saved"):
        export_clip(rec.ffmpeg, segs, Window(1.0, 3.0), tmp_path / "x.mp4", ffprobe=rec.ffprobe)
    assert not (tmp_path / "x.mp4").exists()


def test_disk_full_while_recording_recovers_when_space_returns(make_fake, monkeypatch):
    free = {"bytes": 10**6}
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 10**12 - free["bytes"], free["bytes"]))
    states = []
    rec = make_fake({"runs": [{"segments": 4, "exit": 1, "partial": True,
                               "stderr": "[segment @ 0x1] Failed to write seg: No space left on device"}],
                     "default": {}},
                    min_free_bytes=50 * 10**6, restart_delay_s=0.1, max_restarts=10,
                    on_state=lambda s, e: states.append(s))
    free["bytes"] = 10**9
    rec.start()
    free["bytes"] = 10**6                                        # the drive fills up
    assert wait_for(lambda: rec.state == "restarting")
    assert "is full" in rec.last_error
    time.sleep(0.5)
    assert rec.state == "restarting" and len(rec._runs) == 1     # no restart while the drive is full
    free["bytes"] = 10**9                                        # space freed
    assert wait_for(lambda: rec.state == "recording" and len(rec._runs) == 2)
    assert wait_for(lambda: rec.stats.segments_seen >= 8)
    assert states[:3] == ["recording", "restarting", "recording"]
    assert not (rec.dir / "seg_000004.ts").exists() and rec.stats.orphans_removed >= 1
    with rec.lock:
        new = [s for s in rec.segments if s.run == 1]
    assert new[0].path.name == "seg_000005.ts"                   # numbering continued past the partial file
    rec.stop()


def test_persistent_failure_gives_up_then_start_works(make_fake):
    rec = make_fake({"runs": [], "default": {"segments": 1, "exit": 1, "stderr": "Error: capture device lost"}},
                    max_restarts=3, restart_delay_s=0.05)
    rec.start()
    assert wait_for(lambda: rec.state == "failed", timeout=20)
    assert rec.last_error.startswith("FFmpeg stopped unexpectedly (exit code 1)")
    assert "capture device lost" in rec.last_error
    assert rec.stats.restarts == 3 and rec._lock_file is None
    assert wait_for(lambda: ring_threads() == [])
    t0 = time.monotonic()
    assert rec.wait_until(1e9, timeout=30) is False and time.monotonic() - t0 < 1.0
    set_plan(rec, {"default": {}})                               # device back
    rec.start()
    assert rec.state == "recording" and rec.last_error is None
    assert wait_for(lambda: rec.stats.segments_seen >= 3)
    rec.stop()
    assert rec.state == "stopped"


def test_storage_error_messages():
    folder = Path(r"D:\Clips")
    assert lr.describe_storage_error(OSError(28, "No space left on device"), folder).startswith("The drive D: is full")
    disk_full = OSError(None, "x")
    disk_full.winerror = 112
    assert "is full" in lr.describe_storage_error(disk_full, folder)
    assert "denied access" in lr.describe_storage_error(PermissionError(13, "Permission denied"), folder)
    assert "is full" in lr.describe_ffmpeg_failure("av_interleaved_write_frame(): No space left on device", 1, folder)
    msg = lr.describe_ffmpeg_failure("line one\nInvalid data found when processing input", -22, folder)
    assert msg == "FFmpeg stopped unexpectedly (exit code -22): Invalid data found when processing input"


# --- 5. FFmpeg failures -------------------------------------------------------------------
def test_ffmpeg_killed_during_recording_restarts_and_cleans_up(make_ring):
    rec = make_ring(buffer_s=60.0)
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 6)
    first = rec.proc
    first.kill()                                                 # crash mid-segment
    assert wait_for(lambda: rec.state == "recording" and len(rec._runs) == 2)
    assert "exit code 1" in rec.last_error and rec.stats.restarts == 1
    n0 = rec.stats.segments_seen
    assert wait_for(lambda: rec.stats.segments_seen >= n0 + 6)
    assert first.stderr.closed and rec._runs[0].reader is None and rec._runs[0].proc is None
    assert rec._runs[0].returncode == 1
    with rec.lock:
        old = [s for s in rec.segments if s.run == 0]
        new = [s for s in rec.segments if s.run == 1]
    assert new and new[0].start >= old[-1].end                   # timeline keeps moving forward
    assert int(new[0].path.stem[4:]) > int(old[-1].path.stem[4:])
    stray = seg_files(rec.dir) - indexed(rec)
    # Unlisted files can only be newer than the index: the one being written, plus any finished one the
    # watcher hasn't read yet. A leftover from the crashed run would be older and fail this.
    assert all(int(n[4:10]) > int(new[-1].path.stem[4:]) for n in stray)
    with pytest.raises(FootageMissing, match="gap"):
        rec.pin(Window(old[-1].end - 1.0, new[0].start + 1.0))   # can't cut across the crash
    with rec.pin(Window(new[1].start, new[1].start + 1.0)):
        pass
    rec.stop()
    assert seg_files(rec.dir) == indexed(rec)


def test_recording_write_failure_restarts(make_ring):
    rec = make_ring(buffer_s=60.0)
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 2)
    with rec.lock:
        nxt = max(int(n[4:10]) for n in seg_files(rec.dir)) + 4
    blocker = rec.dir / f"seg_{nxt:06d}.ts"
    blocker.mkdir()                                              # ffmpeg can't open this segment for writing
    try:
        assert wait_for(lambda: rec.stats.restarts >= 1 and rec.state == "recording")
        assert "denied access" in rec.last_error
        n0 = rec.stats.segments_seen
        assert wait_for(lambda: rec.stats.segments_seen >= n0 + 4)
        assert rec.stats.watcher_errors == 0
        assert blocker in rec._pending_delete or not blocker.exists()
    finally:
        rec.stop()
        blocker.rmdir()


def test_crash_leaves_partial_file_which_is_removed(make_fake):
    rec = make_fake({"runs": [{"segments": 3, "exit": 1, "partial": True, "stderr": "boom"}], "default": {}})
    rec.start()
    assert wait_for(lambda: len(rec._runs) == 2 and rec.stats.segments_seen >= 6)
    assert not (rec.dir / "seg_000003.ts").exists()              # unfinished, unlisted: removed
    assert "seg_000004.ts" in indexed(rec)
    assert rec.stats.orphans_removed == 1 and rec.stats.rejected == 0
    rec.stop()


def test_stop_terminates_ffmpeg_that_ignores_q(make_fake):
    rec = make_fake({"runs": [{"ignore_q": True}]})
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 2)
    t0 = time.monotonic()
    rec.stop(timeout=1.0)
    assert time.monotonic() - t0 < 4.0
    assert rec.state == "stopped" and rec._runs[0].returncode not in (None, 0)
    assert ring_threads() == []


def test_input_end_is_not_treated_as_a_crash(make_fake):
    rec = make_fake({"runs": [{"segments": 5, "exit": 0}]})
    rec.start()
    assert wait_for(lambda: rec.state == "ended")
    assert rec.stats.restarts == 0 and rec.last_error is None and rec.stats.segments_seen == 5
    assert rec._lock_file is None and wait_for(lambda: ring_threads() == [])
    set_plan(rec, {"runs": [{"segments": 2, "exit": 0}]})
    rec.start()
    assert wait_for(lambda: rec.state == "ended")


def test_watcher_survives_unexpected_errors(make_fake, monkeypatch):
    rec = make_fake({"runs": [{}]})
    calls = {"n": 0}
    real = rec._retry_deletes

    def flaky():
        calls["n"] += 1
        if calls["n"] in (3, 4, 5):
            raise RuntimeError("simulated watcher bug")
        real()

    monkeypatch.setattr(rec, "_retry_deletes", flaky)
    rec.start()
    assert wait_for(lambda: rec.stats.watcher_errors == 3 and rec.stats.segments_seen >= 10)
    rec.stop()


# --- 6. recovery and reliability -----------------------------------------------------------
def test_app_killed_during_recording(tmp_path, ffmpeg_install, ring_source, make_ring):
    work = tmp_path / "buffer"
    script = tmp_path / "child.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from pathlib import Path\n"
        "from twitchclipper.sources.local_ring import LocalRingRecorder\n"
        f"rec = LocalRingRecorder(Path({str(ffmpeg_install.ffmpeg)!r}), ['-readrate', '4', '-stream_loop', '-1', "
        f"'-i', {str(ring_source)!r}], Path({str(work)!r}), buffer_s=30, segment_s=0.5, min_free_bytes=1)\n"
        "rec.start()\n"
        "print(rec.proc.pid, flush=True)\n"
        "time.sleep(120)\n")
    (work / lr.TMP_DIR).mkdir(parents=True)
    (work / lr.TMP_DIR / "old.abc.joined.ts").write_bytes(b"x")  # left by an export the crash cut short
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        ffmpeg_pid = int(child.stdout.readline())
        assert wait_for(lambda: len(seg_files(work)) >= 4)
        child.kill()                                             # app crashes: no stop(), no cleanup code runs
        child.wait()
        assert wait_for(lambda: not process_alive(ffmpeg_pid), timeout=5)   # ffmpeg died with it
        stale = seg_files(work)
        assert stale and (work / "ring.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
    rec = make_ring(workdir=work)                                # next launch: lock is free, leftovers go
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 2)
    assert not (work / lr.TMP_DIR / "old.abc.joined.ts").exists()
    with rec.lock:
        assert rec.segments[0].path.name == "seg_000000.ts" and rec.segments[0].start == 0.0
    rec.stop()
    assert seg_files(work) == indexed(rec)


def test_start_cleans_stale_files_and_survives_locked_ones(make_fake):
    rec = make_fake({"runs": [{}]})
    d = rec.dir
    (d / lr.TMP_DIR).mkdir(parents=True)
    for name in ("seg_000500.ts", "seg_000007.ts", "segments.csv", "segments_000.csv", "segments_003.csv"):
        (d / name).write_bytes(b"old")
    (d / lr.TMP_DIR / "c.1.part.mp4").write_bytes(b"old")
    held = open(d / "seg_000900.ts", "wb")                       # e.g. antivirus scanning an old segment
    try:
        rec.start()
        assert wait_for(lambda: rec.stats.segments_seen >= 3)
        assert not any((d / n).exists() for n in ("seg_000500.ts", "segments.csv", "segments_003.csv"))
        assert not (d / lr.TMP_DIR / "c.1.part.mp4").exists()
        with rec.lock:
            assert rec.segments[0].path.name == "seg_000901.ts"  # numbering skips the file it couldn't delete
        assert (d / "seg_000900.ts").exists()
    finally:
        held.close()
    assert wait_for(lambda: not (d / "seg_000900.ts").exists())  # deleted once released
    rec.stop()


def test_eviction_retries_files_held_by_another_program(make_fake):
    rec = make_fake({"runs": [{"interval": 0.15}]}, buffer_s=1.0)
    rec.start()
    assert wait_for(lambda: rec.stats.segments_seen >= 1)
    target = rec.dir / "seg_000000.ts"
    held = open(target, "rb")
    try:
        assert wait_for(lambda: rec.stats.evicted >= 3)
        assert target.exists() and target in rec._pending_delete and rec.stats.delete_retries > 0
        assert rec.running() and rec.stats.watcher_errors == 0
    finally:
        held.close()
    assert wait_for(lambda: not target.exists())
    rec.stop()


def test_start_waits_for_clips_still_being_saved(make_fake):
    rec = make_fake({"runs": [{"segments": 6, "exit": 0}], "default": {"segments": 3, "exit": 0}})
    rec.start()
    assert wait_for(lambda: rec.state == "ended")
    pin = rec.pin(Window(0.5, 1.5))
    with pytest.raises(RingBusy, match="still being saved"):
        rec.start()
    with pytest.raises(RingBusy):
        rec.clear()
    assert all(s.path.exists() for s in pin.segments)
    rec.close()                                                  # shutdown keeps files a clip still needs
    assert all(s.path.exists() for s in pin.segments)
    pin.release()
    rec.start()
    assert wait_for(lambda: rec.state == "ended")
    rec.close()
    assert seg_files(rec.dir) == set() and not list(rec.dir.glob("segments*.csv"))


def test_wait_until_returns_when_recording_stops(make_fake):
    rec = make_fake({"runs": [{}]})
    rec.start()
    got = {}

    def waiter():
        t0 = time.monotonic()
        got["ok"] = rec.wait_until(1e9, timeout=60)
        got["s"] = time.monotonic() - t0

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.3)
    rec.stop()
    t.join(5)
    assert got == {"ok": False, "s": got["s"]} and got["s"] < 2.0


def test_pin_release_race_under_load(make_fake):
    rec = make_fake({"runs": [{"interval": 0.005, "packets": 4}]}, buffer_s=1.5)
    rec.start()
    assert wait_for(lambda: rec.newest_end() >= 3.0)
    stop = threading.Event()
    stats = {"pins": 0, "refused": 0, "missing": 0, "errors": []}
    lock = threading.Lock()

    def worker():
        while not stop.is_set():
            newest = rec.newest_end()
            try:
                p = rec.pin(Window(newest - 1.2, newest - 0.2))
            except (PreRollLost, FootageMissing):
                with lock:
                    stats["refused"] += 1
                continue
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(exc)
                return
            ok = all(s.path.exists() for s in p.segments)
            time.sleep(0.005)
            ok = ok and all(s.path.exists() for s in p.segments)
            p.release()
            p.release()
            with lock:
                stats["pins"] += 1
                stats["missing"] += not ok

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join()
    assert not stats["errors"] and stats["missing"] == 0 and stats["pins"] > 100
    assert rec.pinned_count() == 0 and all(s.pins == 0 for s in rec.segments)
    assert rec.running() and rec.stats.watcher_errors == 0 and rec.stats.evicted > 50
    with rec.lock:
        span = rec.segments[-1].end - rec.segments[0].start
    assert span <= 1.5 + 0.6                                     # buffer stayed bounded under pin churn
    rec.stop()
    assert seg_files(rec.dir) == indexed(rec)
