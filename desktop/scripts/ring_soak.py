"""Soak test for the Mode D ring buffer (sources/local_ring.py): one long session with exports
running all the time and FFmpeg killed at intervals, watching for leaks and stalls.

Local synthetic footage only (generated into a temporary folder, deleted afterwards); no Twitch.

    uv run python scripts/ring_soak.py --seconds 90

Checks: every export either succeeds and is the requested length or is refused for a clear
reason (a restart gap); recording recovers after each kill; the buffer folder stays bounded; the
process's Windows handle count and thread count don't grow; nothing is left behind after close().
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.support.footage import generate  # noqa: E402
from twitchclipper.export import ffmpeg as ft  # noqa: E402
from twitchclipper.sources.local_ring import FootageMissing, LocalRingRecorder, PreRollLost, RingError  # noqa: E402
from twitchclipper.timing.window import Window  # noqa: E402


def handle_count() -> int:
    if sys.platform != "win32":
        return -1
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    n = ctypes.c_uint32()
    k32.GetProcessHandleCount(ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(n))
    return n.value


def folder_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            total += p.stat().st_size if p.is_file() else 0
        except OSError:
            pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=90.0, help="real seconds to run")
    ap.add_argument("--readrate", type=float, default=8.0, help="stream seconds per real second")
    ap.add_argument("--kill-every", type=float, default=20.0, help="kill ffmpeg every N real seconds")
    args = ap.parse_args()

    inst = ft.best_install()
    if not inst or not inst.ffprobe:
        print("ffmpeg not found")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="tc-ring-soak-"))
    try:
        src = generate(inst.ffmpeg, tmp / "src.mp4", 60.0, fps=30, width=640, height=360, keyframe_s=1.0)
        rec = LocalRingRecorder(inst.ffmpeg, ["-readrate", str(args.readrate), "-stream_loop", "-1", "-i", str(src)],
                                tmp / "buffer", buffer_s=30.0, segment_s=1.0, ffprobe=inst.ffprobe,
                                min_free_bytes=50 * 1024 * 1024, max_restarts=5, restart_delay_s=0.3)
        out_dir = tmp / "clips"
        results = {"ok": 0, "refused_gap": 0, "refused_other": 0, "failed": [], "bad_length": []}
        lock = threading.Lock()
        busy = threading.Semaphore(2)
        samples = []

        def export_one(i: int) -> None:
            with busy:
                newest = rec.newest_end()
                w = Window(newest - 6.0, newest - 1.0)
                out = out_dir / f"clip_{i:04d}.mp4"
                try:
                    rec.export(w, out)
                    dur = ft.video_info(inst.ffprobe, out)["duration"]
                    with lock:
                        results["ok"] += 1
                        if abs(dur - w.length) > 0.15:
                            results["bad_length"].append((out.name, dur))
                    out.unlink()
                except FootageMissing as exc:
                    with lock:
                        results["refused_gap" if "gap" in str(exc) else "refused_other"] += 1
                except PreRollLost:
                    with lock:
                        results["refused_other"] += 1
                except RingError as exc:
                    with lock:
                        results["failed"].append(str(exc))

        rec.start()
        t0 = time.monotonic()
        rec.wait_until(8.0, timeout=30)
        gc.collect()
        base_handles, base_threads = handle_count(), threading.active_count()
        next_kill = t0 + args.kill_every
        threads: list[threading.Thread] = []
        i = kills = 0
        while time.monotonic() - t0 < args.seconds:
            if time.monotonic() >= next_kill and rec.running():
                rec.proc.kill()
                kills += 1
                next_kill += args.kill_every
            th = threading.Thread(target=export_one, args=(i,))
            th.start()
            # keep only running threads: a finished Thread object still holds an OS handle in 3.12
            threads = [t for t in threads if t.is_alive()] + [th]
            i += 1
            samples.append({"t": round(time.monotonic() - t0, 1), "handles": handle_count(),
                            "threads": threading.active_count(), "buffer_mb": round(folder_bytes(rec.dir) / 1e6, 1),
                            "state": rec.state, "segments": len(rec.segments)})
            time.sleep(random.uniform(1.0, 2.5))
        for th in threads:
            th.join()
        walls = list(rec.stats.completion_walls)
        stream_s = rec.newest_end()
        rec.close()
        time.sleep(0.5)
        gc.collect()
        end_handles, end_threads = handle_count(), threading.active_count()
        leftovers = sorted(p.name for p in rec.dir.rglob("*") if p.is_file() and p.name != "ring.lock")
        mid = samples[len(samples) // 3:]
        gaps = [b - a for a, b in zip(walls, walls[1:])]
        checks = {
            "exports succeeded": results["ok"] > 0 and not results["failed"],
            "exports have the requested length": not results["bad_length"],
            "recovered from every kill": rec.stats.restarts >= kills and rec.state == "stopped",
            "recording never stalled (no gap > 3 s between segments)": bool(gaps) and max(gaps) <= 3.0,
            "buffer folder bounded (<= 2x the steady size)": max(s["buffer_mb"] for s in mid) <= 2 * max(1.0, mid[0]["buffer_mb"]) + 5,
            "handle count stable (end within +40 of start)": end_handles <= base_handles + 40,
            "thread count back to start": end_threads <= base_threads,
            "no files left after close()": leftovers == [],
        }
        report = {
            "real_seconds": args.seconds, "kills": kills, "stream_seconds": round(stream_s, 1),
            "segments_seen": rec.stats.segments_seen, "evicted": rec.stats.evicted, "restarts": rec.stats.restarts,
            "rejected": rec.stats.rejected, "watcher_errors": rec.stats.watcher_errors,
            "slowest_watcher_pass_s": round(rec.stats.max_tick_s, 3),
            "exports": {k: (len(v) if isinstance(v, list) else v) for k, v in results.items()},
            "export_failures": results["failed"][:5],
            "max_gap_between_segments_s": round(max(gaps), 2) if gaps else None,
            "gaps_over_3s": [round(g, 2) for g in gaps if g > 3.0],
            "handles": {"start": base_handles, "end": end_handles, "max": max(s["handles"] for s in samples)},
            "threads": {"start": base_threads, "end": end_threads},
            "buffer_mb_max": max(s["buffer_mb"] for s in samples),
            "leftover_files": leftovers,
            "checks": checks,
        }
        print(json.dumps(report, indent=2))
        return 0 if all(checks.values()) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
