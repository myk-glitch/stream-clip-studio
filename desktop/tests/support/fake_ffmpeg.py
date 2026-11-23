"""Stand-in for ffmpeg's segment muxer, for ring-buffer failure tests.

Run as `python fake_ffmpeg.py <the recorder's ffmpeg arguments>`. It writes small
MPEG-TS-shaped segment files and CSV list lines the way ffmpeg does, and misbehaves
on cue. The plan is a JSON file named by FAKE_FFMPEG_PLAN:

    {"runs": [{...run 0...}, {...run 1...}], "default": {...later runs...}}

Run options (all optional):
    segments   segments to write before exiting (default: until "q" on stdin)
    interval   seconds between segments (0.05)
    seg_s      listed duration of each segment (0.5)
    exit       exit code when the segments are done (0 = input ended)
    stderr     text written to stderr before exiting
    truncated / bad_sync / empty / missing / duplicate   lists of segment indexes to damage
    garbage_after   write an unreadable list line after this segment index
    partial    leave an unfinished, unlisted segment file when exiting
    ignore_q   keep going after "q" (tests the terminate fallback)
    stall_after  stop writing after this many segments but keep running, like ffmpeg waiting on an
                 unplugged capture device (until "q" or killed)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

PACKET = b"\x47" + bytes(187)


def arg(args: list[str], name: str, default: str | None = None) -> str | None:
    return args[args.index(name) + 1] if name in args else default


def main() -> int:
    args = sys.argv[1:]
    plan_path = Path(os.environ["FAKE_FFMPEG_PLAN"])
    plan = json.loads(plan_path.read_text())
    counter = plan_path.with_suffix(".count")
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    runs = plan.get("runs", [])
    cfg = runs[n] if n < len(runs) else plan.get("default", {})

    list_path = Path(arg(args, "-segment_list"))
    number = int(arg(args, "-segment_start_number", "0"))
    pattern = args[-1]
    seg_s = float(cfg.get("seg_s", 0.5))
    interval = float(cfg.get("interval", 0.05))
    total = cfg.get("segments")

    quit_requested = threading.Event()

    def watch_stdin() -> None:
        while True:
            b = sys.stdin.buffer.read(1)
            if not b:
                return          # parent gone: like ffmpeg, keep running
            if b == b"q":
                quit_requested.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    lst = open(list_path, "w", encoding="utf-8", newline="\n")
    i = 0
    try:
        while total is None or i < total:
            if quit_requested.is_set() and not cfg.get("ignore_q"):
                break
            if cfg.get("stall_after") is not None and i >= cfg["stall_after"]:
                time.sleep(0.05)
                continue
            name = Path(pattern % (number + i)).name
            path = Path(pattern % (number + i))
            data = PACKET * int(cfg.get("packets", 20))
            if i in cfg.get("truncated", []):
                data = data[:-50]
            if i in cfg.get("bad_sync", []):
                data = b"\x00" + data[1:]
            if i in cfg.get("empty", []):
                data = b""
            if i not in cfg.get("missing", []):
                path.write_bytes(data)
            line = f"{name},{i * seg_s:.6f},{(i + 1) * seg_s:.6f}\n"
            lst.write(line)
            if i in cfg.get("duplicate", []):
                lst.write(line)
            if cfg.get("garbage_after") == i:
                lst.write("\x00garbage,line\n")
            lst.flush()
            i += 1
            time.sleep(interval)
        if cfg.get("partial"):
            Path(pattern % (number + i)).write_bytes(PACKET * 3 + b"\x47\x00")
        if cfg.get("stderr"):
            sys.stderr.write(cfg["stderr"] + "\n")
            sys.stderr.flush()
        return int(cfg.get("exit", 0)) if total is not None else 0
    finally:
        lst.close()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Not sys.exit(): interpreter shutdown with the stdin thread still blocked in read() can crash
    # on Windows (0xC0000005), which would look like an ffmpeg crash to the recorder.
    os._exit(code)
