"""Real-desktop check of Mode D screen capture and "Clip that" timing (Windows, real display).

Shows a small window of its own that draws a machine-readable clock (the clock strip from
tests/support/timecode.py, updated continuously from the PC clock), captures ONLY that window's
area through the production path (gdigrab screen capture -> live H.264 encode -> ring buffer),
presses "Clip that" with the default 22 s before + 5 s after, and decodes the clip:
the first and last frames must show the PC clock at press - 22 s and press + 5 s.

No camera, microphone or other screen content is recorded. Everything goes to a temporary folder
that is deleted afterwards. Takes about 40 s; the window stays on top meanwhile.

    uv run python scripts/capture_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)
TMP = Path(tempfile.mkdtemp(prefix="tc-capture-check-"))
os.environ["TWITCHCLIPPER_HOME"] = str(TMP)

import numpy as np  # noqa: E402
from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QCursor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from tests.support.footage import decode_file  # noqa: E402
from tests.support.timecode import draw_strip  # noqa: E402

W, H = 640, 360
T0 = time.time()


def clock_ms() -> int:
    return int((time.time() - T0) * 1000)


class ClockWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setWindowTitle("TwitchClipper capture check")
        self.setFixedSize(W, H)
        yy, xx = np.mgrid[0:H, 0:W]
        self.bg = np.stack([(xx * 255 // W), (yy * 255 // H), np.full_like(xx, 96)], axis=2).astype(np.uint8)
        self.timer = QTimer(self)
        self.timer.setInterval(5)
        self.timer.timeout.connect(self.update)
        self.timer.start()

    def paintEvent(self, event):  # noqa: N802
        frame = self.bg.copy()
        draw_strip(frame, clock_ms())
        img = QImage(frame.data, W, H, 3 * W, QImage.Format.Format_RGB888)
        p = QPainter(self)
        p.drawImage(0, 0, img)
        p.setPen(Qt.GlobalColor.white)
        p.drawText(20, H - 20, "TwitchClipper capture check: this window is being recorded (only this area).")
        p.end()


def main() -> int:
    app = QApplication(sys.argv)
    from twitchclipper.core.context import create_context
    from twitchclipper.engine.capture import CaptureController
    from twitchclipper.export import ffmpeg as ft
    from twitchclipper.sources import capture as cap

    ctx = create_context()
    if not ctx.ffmpeg:
        print("ffmpeg not found")
        return 2
    ctx.detect_encoders()
    enc = ctx.encoder_for_export()[0]
    win = ClockWindow()
    screen = app.primaryScreen().availableGeometry()
    cur = QCursor.pos()
    x = screen.left() + 40 if cur.x() > screen.center().x() else screen.right() - W - 40
    win.move(x, screen.top() + 40)
    win.show()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    dpr = win.devicePixelRatio()
    tl = win.mapToGlobal(QPoint(0, 0))
    rect = (round(tl.x() * dpr), round(tl.y() * dpr), round(W * dpr) // 2 * 2, round(H * dpr) // 2 * 2)
    src = cap.CaptureSource("screen:check", "screen", "Screen: capture-check window", rect=rect)

    ctrl = CaptureController(ctx)
    report: dict = {"encoder": enc, "rect": rect, "dpr": dpr}
    finished: dict = {}
    ctrl.clip_finished.connect(lambda t, cid, path: finished.update(path=path, id=cid))
    ctrl.clip_failed.connect(lambda t, msg: finished.update(error=msg))
    states = []
    ctrl.state_changed.connect(lambda s, m: states.append((s, m)))

    def pump(cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not cond():
            app.processEvents()
            time.sleep(0.01)
        return cond()

    try:
        assert ctrl.start(src) is None
        if not pump(lambda: ctrl.recording, 15):
            report["error"] = f"capture didn't start: {states}"
            raise SystemExit
        before, after = ctrl.before_s, ctrl.after_s
        report["clip"] = {"before_s": before, "after_s": after}
        rec = ctrl.rec
        pump(lambda: (rec.stream_now() or 0) - (rec.oldest_start() or 1e9) >= before + 1.5, 60)
        seen = rec.stats.segments_seen
        press_ms = clock_ms()
        ok, msg = ctrl.clip_that()
        report["accepted"], report["message"] = ok, msg
        pump(lambda: finished, 60)
        report["segments_during_clip"] = rec.stats.segments_seen - seen
        report["recording_during_export"] = rec.running()
        if "path" in finished:
            path = Path(finished["path"])
            dec = decode_file(ctx.ffmpeg.ffmpeg, path)
            info = ft.video_info(ctx.ffmpeg.ffprobe, path)
            exp_first = press_ms - before * 1000
            exp_last = press_ms + after * 1000
            report["result"] = {
                "duration_s": round(info["duration"], 3), "size": [info["width"], info["height"]],
                "frames": dec.frames, "decoded": dec.decoded, "monotonic": dec.monotonic,
                "first_frame_vs_press_minus_before_ms": None if dec.first_ms is None else dec.first_ms - exp_first,
                "last_frame_vs_press_plus_after_ms": None if dec.last_ms is None else dec.last_ms - exp_last,
            }
            row = ctx.db.clip(finished["id"])
            report["results_row_source"] = row["source"] if row else None
        else:
            report["clip_error"] = finished.get("error")
        r = report.get("result", {})
        checks = {
            "screen capture recorded": bool(r),
            "clip has the configured length (27 s ±0.25)": bool(r) and abs(r["duration_s"] - (before + after)) <= 0.25,
            "every frame readable, in order": bool(r) and r["decoded"] == r["frames"] and r["monotonic"],
            "clip starts at press - 22 s (±0.5 s)": bool(r) and r["first_frame_vs_press_minus_before_ms"] is not None
                                                    and abs(r["first_frame_vs_press_minus_before_ms"]) <= 500,
            "clip ends at press + 5 s (±0.5 s)": bool(r) and r["last_frame_vs_press_plus_after_ms"] is not None
                                                  and abs(r["last_frame_vs_press_plus_after_ms"]) <= 500,
            "recording continued during the export": report["segments_during_clip"] > 0 and report["recording_during_export"],
            "clip recorded in Results as a live capture": report.get("results_row_source") == "capture",
        }
        report["checks"] = checks
    finally:
        ctrl.shutdown(10000)
        win.close()
        ctx.db.close()
        print(json.dumps(report, indent=2, default=str))
        import logging
        for h in list(logging.getLogger("twitchclipper").handlers):   # the log file keeps the folder busy
            h.close()
            logging.getLogger("twitchclipper").removeHandler(h)
        shutil.rmtree(TMP, ignore_errors=True)
        if TMP.exists():
            print(f"note: couldn't remove {TMP}")
    return 0 if report.get("checks") and all(report["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
