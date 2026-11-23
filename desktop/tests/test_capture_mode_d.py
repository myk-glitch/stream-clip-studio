"""Mode D in the app: capture source picker, Start/Stop capture, "Clip that", Settings → Capture,
and the Results page — through the real Live Monitor page and CaptureController.

Recorders used:
  * the production path (`CaptureController._default_recorder`) on the built-in test pattern, which
    really encodes a live source (lavfi, real time) — no camera or screen is recorded;
  * real FFmpeg on generated clock-strip footage played at 1x (a real-time source whose frames carry
    their own time stamp, so clip contents can be checked);
  * tests/support/fake_ffmpeg.py for disconnections and hangs.
No Twitch, no webcam/microphone capture, and the test_media fixture isn't touched.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer

from tests.support.footage import decode_file, generate
from tests.support.helpers import wait_for as shared_wait_for
from twitchclipper.config import settings as cfg
from twitchclipper.core.context import create_context
from twitchclipper.engine import capture as engine_capture
from twitchclipper.engine.capture import CaptureController
from twitchclipper.export import ffmpeg as ft
from twitchclipper.sources import capture as cap
from twitchclipper.sources import local_ring as lr
from twitchclipper.sources.local_ring import LocalRingRecorder
from twitchclipper.ui.pages.live_monitor import LiveMonitorPage

FAKE = Path(__file__).parent / "support" / "fake_ffmpeg.py"
LEFT = Qt.MouseButton.LeftButton

# The shape of `ffmpeg -list_devices true -f dshow -i dummy` output, with made-up device names and ids.
REAL_LISTING = r'''[in#0 @ 0000000000000000] "Example HD Camera" (video)
[in#0 @ 0000000000000000]   Alternative name "@device_pnp_\\?\usb#vid_0000&pid_0000&mi_00#0&0000000&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[in#0 @ 0000000000000000] "Internal Microphone (Example Audio)" (audio)
[in#0 @ 0000000000000000]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{00000000-0000-0000-0000-000000000000}"
Error opening input file dummy.
'''

CAM = cap.CaptureSource("dshow:Test Cam", "dshow", "Camera / capture device: Test Cam", device="Test Cam")
MIC = cap.AudioDevice("Test Mic", "Test Mic")


def fake_list(*, camera=True, audio=True, error=None) -> cap.SourceList:
    video = ([CAM] if camera else []) + [cap.CaptureSource(cap.DESKTOP_KEY, "screen", "Screen: entire desktop"),
                                          cap.CaptureSource(cap.TEST_KEY, "test", "Test pattern (built in, no device needed)")]
    return cap.SourceList(video, [MIC] if audio else [], error)


def wait_for(pred, timeout=20.0, step=0.02) -> bool:
    return shared_wait_for(pred, timeout, step)


class Responsiveness:
    """Records the gaps between 20 ms timer ticks on the UI thread."""

    def __init__(self):
        self.ticks: list[float] = []
        self.timer = QTimer()
        self.timer.setInterval(20)
        self.timer.timeout.connect(lambda: self.ticks.append(time.monotonic()))
        self.timer.start()

    def max_gap(self) -> float:
        self.timer.stop()
        return max((b - a for a, b in zip(self.ticks, self.ticks[1:])), default=0.0)


@pytest.fixture(scope="session")
def clock_source(tmp_path_factory, ffmpeg_install):
    d = tmp_path_factory.mktemp("capture_src")
    return generate(ffmpeg_install.ffmpeg, d / "clock.mp4", 40.0, fps=30, width=640, height=360, keyframe_s=0.5)


@pytest.fixture
def ctx(app_home):
    c = create_context()
    c.settings.capture.clip_before_s = 2.0       # short windows keep the tests fast; defaults tested below
    c.settings.capture.clip_after_s = 1.0
    c.settings.capture.buffer_s = 60.0
    yield c
    c.db.close()


class Harness:
    def __init__(self, qtbot, ctx, enumerate_fn=None, factory=None):
        self.calls = {"enumerate": 0, "factory": 0}
        self.listing = enumerate_fn or (lambda: fake_list())
        self.made: list[LocalRingRecorder] = []

        def enum():
            self.calls["enumerate"] += 1
            return self.listing()

        def make(src, audio):
            self.calls["factory"] += 1
            rec = (factory or (lambda s, a: ctrl._default_recorder(s, a)))(src, audio)
            self.made.append(rec)
            return rec

        ctrl = CaptureController(ctx, enumerate_fn=enum, recorder_factory=make)
        self.ctrl = ctrl
        self.page = LiveMonitorPage(ctx, controller=ctrl)
        qtbot.addWidget(self.page)
        self.page.resize(1100, 800)
        self.page.show()
        qtbot.waitUntil(lambda: self.page.source_combo.count() > 0 or self.calls["enumerate"] > 0, timeout=5000)
        qtbot.waitUntil(lambda: not ctrl._listing, timeout=5000)

    def select(self, key: str) -> None:
        self.page.source_combo.setCurrentIndex(self.page.source_combo.findData(key))

    def close(self) -> None:
        self.ctrl.shutdown(5000)


@pytest.fixture
def harness(qtbot, ctx):
    made = []

    def build(**kw):
        h = Harness(qtbot, ctx, **kw)
        made.append(h)
        return h

    yield build
    for h in made:
        h.close()


def file_factory(ctx, ffmpeg_install, clock_source, **kw):
    """A real-time recorder on clock footage (1x), standing in for a live device."""
    def make(src, audio):
        return LocalRingRecorder(ffmpeg_install.ffmpeg, ["-readrate", "1", "-stream_loop", "-1", "-i", str(clock_source)],
                                 ctx.paths.buffer_dir, buffer_s=ctx.settings.capture.buffer_s, segment_s=0.5,
                                 ffprobe=ffmpeg_install.ffprobe, min_free_bytes=50 * 1024 * 1024, **kw)
    return make


class FakeRing(LocalRingRecorder):
    def _command(self, list_path, start_number):
        return [sys.executable, str(FAKE)] + super()._command(list_path, start_number)[1:]


def start_and_wait(qtbot, h, key=cap.TEST_KEY, buffered=None):
    h.select(key)
    qtbot.mouseClick(h.page.start_capture_btn, LEFT)

    def diag():
        rec = h.ctrl.rec
        return (f"state={h.page._state!r} msg={h.page.capture_msg.text()!r} "
                f"rec={rec.status() if rec else None} now={rec.stream_now() if rec else None} "
                f"oldest={rec.oldest_start() if rec else None}")
    try:
        qtbot.waitUntil(lambda: h.ctrl.recording, timeout=15000)
        if buffered is not None:
            def enough():
                rec = h.ctrl.rec
                now, oldest = rec.stream_now(), rec.oldest_start()
                return now is not None and oldest is not None and now - oldest >= buffered
            qtbot.waitUntil(enough, timeout=30000)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"capture didn't get going: {diag()}") from exc


# --- source enumeration --------------------------------------------------------------------
def test_parse_real_dshow_listing():
    video, audio = cap.parse_dshow_list(REAL_LISTING)
    assert [v[0] for v in video] == ["Example HD Camera"] and video[0][1].startswith("@device_pnp_")
    assert [a[0] for a in audio] == ["Internal Microphone (Example Audio)"]
    assert cap.parse_dshow_list("[dshow @ 0x1] Could not enumerate video devices (or none found).\n") == ([], [])


def test_duplicate_device_names_open_by_alternative_name(monkeypatch, ffmpeg_install):
    text = ('"USB Video" (video)\n  Alternative name "@device_pnp_A"\n'
            '"USB Video" (video)\n  Alternative name "@device_pnp_B"\n"Line In" (audio)\n')
    monkeypatch.setattr(cap, "list_dshow", lambda ffmpeg, timeout=15.0: (*cap.parse_dshow_list(text), None))
    res = cap.enumerate_sources(ffmpeg_install.ffmpeg)
    cams = [s for s in res.video if s.kind == "dshow"]
    assert [(s.label, s.device) for s in cams] == [("Camera / capture device: USB Video #1", "@device_pnp_A"),
                                                   ("Camera / capture device: USB Video #2", "@device_pnp_B")]
    assert len({s.key for s in cams}) == 2 and [a.name for a in res.audio] == ["Line In"]


def test_enumeration_failures_still_offer_screen_and_test_pattern(monkeypatch, ffmpeg_install):
    res = cap.enumerate_sources(None)
    assert res.dshow_error and "FFmpeg isn't installed" in res.dshow_error
    assert [s.kind for s in res.video] == ["screen", "test"]

    def slow(*a, **k):
        raise cap.subprocess.TimeoutExpired("ffmpeg", 15)
    monkeypatch.setattr(cap.subprocess, "run", slow)
    res = cap.enumerate_sources(ffmpeg_install.ffmpeg)
    assert "took longer than" in res.dshow_error and res.devices[0].kind == "screen"


def test_real_enumeration_on_this_pc(ffmpeg_install):
    res = cap.enumerate_sources(ffmpeg_install.ffmpeg)
    assert res.dshow_error is None
    assert res.find(cap.DESKTOP_KEY) and res.find(cap.TEST_KEY)
    assert all(s.key.startswith("dshow:") and s.device for s in res.video if s.kind == "dshow")


def test_ffmpeg_arguments_per_source():
    assert cap.input_args(CAM) == ["-f", "dshow", "-rtbufsize", "256M", "-i", "video=Test Cam"]
    assert cap.input_args(CAM, MIC)[-1] == "video=Test Cam:audio=Test Mic"
    out = cap.output_args(CAM, MIC, ft.encoder_args("h264_qsv"))
    assert out[:4] == ["-map", "0:v", "-map", "0:a"] and "-force_key_frames" in out and "aac" in out
    assert "-sc_threshold" not in out
    screen = cap.CaptureSource("screen:2", "screen", "Screen 2", rect=(1920, 0, 2560, 1440))
    args = cap.input_args(screen, MIC)
    assert args[:2] == ["-f", "gdigrab"] and ["-offset_x", "1920"] == args[6:8] and args[-1] == "audio=Test Mic"
    out = cap.output_args(screen, None, ft.encoder_args("libx264"))
    assert "-map" in out and "1:a" not in out and "-sc_threshold" in out and "-c:a" not in out
    test_out = cap.output_args(cap.CaptureSource(cap.TEST_KEY, "test", "t"), None, ft.encoder_args("libx264"))
    assert test_out[2:4] == ["-map", "1:a"]
    assert out[out.index("-g") + 1] == "60"                     # keyframe every 2 s segment at 30 fps


# --- the picker on the Live Monitor page ------------------------------------------------------------
def test_picker_lists_sources_and_refreshes(harness, qtbot):
    h = harness()
    p = h.page
    assert p.tabs.currentIndex() == 0 and p.tabs.tabText(0) == "Local capture (Mode D)"
    assert [p.source_combo.itemData(i) for i in range(p.source_combo.count())] == [CAM.key, cap.DESKTOP_KEY, cap.TEST_KEY]
    assert p.source_combo.currentData() == CAM.key                # a real device is preferred
    assert [p.audio_combo.itemText(i) for i in range(p.audio_combo.count())] == ["No audio", "Test Mic"]
    assert p.selected_lbl.text() == "Source: Camera / capture device: Test Cam"
    assert p.start_capture_btn.isEnabled() and not p.stop_capture_btn.isEnabled() and not p.clip_btn.isEnabled()
    h.select(cap.DESKTOP_KEY)
    p.audio_combo.setCurrentIndex(1)
    assert p.selected_lbl.text() == "Source: Screen: entire desktop + Test Mic"
    n = h.calls["enumerate"]
    qtbot.mouseClick(p.refresh_btn, LEFT)
    qtbot.waitUntil(lambda: h.calls["enumerate"] == n + 1 and not h.ctrl._listing, timeout=5000)
    assert p.source_combo.currentData() == cap.DESKTOP_KEY and p.audio_combo.currentData() == "Test Mic"


def test_no_capture_devices_is_explained(harness):
    h = harness(enumerate_fn=lambda: fake_list(camera=False, audio=False))
    text = h.page.sources_info.text()
    assert "No cameras or capture devices were found" in text and "No audio input devices" in text
    assert h.page.source_combo.currentData() == cap.DESKTOP_KEY and h.page.start_capture_btn.isEnabled()


def test_no_supported_source_at_all(harness):
    h = harness(enumerate_fn=lambda: cap.SourceList(dshow_error="Listing DirectShow devices took longer than 15 s."))
    assert "No supported capture source is available." in h.page.sources_info.text()
    assert "took longer" in h.page.sources_info.text()
    assert not h.page.start_capture_btn.isEnabled() and h.page.selected_lbl.text() == "Source: none selected"


def test_missing_device_from_last_time_is_shown_but_cannot_start(harness, ctx):
    ctx.settings.capture.source = "dshow:Old Capture Card"
    h = harness()
    p = h.page
    assert p.source_combo.currentText() == "Old Capture Card (not connected)"
    assert not p.start_capture_btn.isEnabled()
    assert "“Old Capture Card”, isn't connected" in p.sources_info.text()
    assert "connect it and press Refresh" in p.selected_lbl.text()


def test_device_unplugged_before_start(harness, qtbot):
    h = harness()
    h.listing = lambda: fake_list(camera=False)                  # unplugged after the list was shown
    qtbot.mouseClick(h.page.start_capture_btn, LEFT)
    qtbot.waitUntil(lambda: h.page._state == "failed", timeout=5000)
    assert "“Test Cam” isn't connected any more" in h.page.capture_msg.text()
    assert h.calls["factory"] == 0 and h.ctrl.rec is None
    # the page looks again and shows the camera as gone; it can't be started, other sources can
    qtbot.waitUntil(lambda: "(not connected)" in h.page.source_combo.currentText(), timeout=5000)
    assert not h.page.start_capture_btn.isEnabled()
    h.select(cap.DESKTOP_KEY)
    assert h.page.start_capture_btn.isEnabled()


def test_device_disconnected_while_recording(harness, qtbot, ctx, monkeypatch, tmp_path, ffmpeg_install):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"runs": [{"stall_after": 6, "interval": 0.05}],
                                "default": {"segments": 0, "exit": 1, "stderr":
                                            "[dshow @ 0x1] Could not find video device with name [Test Cam] "
                                            "among source devices of type video."}}))
    monkeypatch.setenv("FAKE_FFMPEG_PLAN", str(plan))

    def make(src, audio):
        return FakeRing(ffmpeg_install.ffmpeg, cap.input_args(src, audio), ctx.paths.buffer_dir, buffer_s=60,
                        segment_s=0.5, ffprobe=ffmpeg_install.ffprobe, min_free_bytes=1, poll_s=0.02,
                        stall_timeout_s=1.0, max_restarts=2, restart_delay_s=0.05)
    h = harness(factory=make)
    qtbot.mouseClick(h.page.start_capture_btn, LEFT)
    qtbot.waitUntil(lambda: h.ctrl.recording, timeout=5000)
    h.listing = lambda: fake_list(camera=False)                  # the camera is gone
    n = h.calls["enumerate"]
    qtbot.waitUntil(lambda: h.page._state == "failed", timeout=20000)
    msg = h.page.capture_msg.text()
    assert "Could not find video device" in msg and "plug it back in and press Refresh" in msg
    assert h.made[0].stats.restarts == 2                         # it tried to reconnect first
    qtbot.waitUntil(lambda: h.calls["enumerate"] > n and not h.ctrl._listing, timeout=5000)
    assert "(not connected)" in h.page.source_combo.currentText() or h.page.source_combo.currentData() != CAM.key
    assert h.page.stop_capture_btn.isEnabled() and not h.page.clip_btn.isEnabled()
    qtbot.mouseClick(h.page.stop_capture_btn, LEFT)
    qtbot.waitUntil(lambda: h.page._state == "stopped", timeout=10000)


def test_stalled_source_message(tmp_path, monkeypatch, ffmpeg_install):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"runs": [{"stall_after": 3}], "default": {}}))
    monkeypatch.setenv("FAKE_FFMPEG_PLAN", str(plan))
    rec = FakeRing(ffmpeg_install.ffmpeg, ["-i", "x"], tmp_path / "buf", buffer_s=60, segment_s=0.5,
                   ffprobe=ffmpeg_install.ffprobe, min_free_bytes=1, poll_s=0.02, stall_timeout_s=0.8,
                   restart_delay_s=0.05)
    rec.start()
    try:
        assert wait_for(lambda: rec.stats.restarts == 1 and rec.state == "recording")
        assert rec.last_error.startswith("No new video for 1 s, so recording was restarted.")
        assert wait_for(lambda: rec.stats.segments_seen >= 6)   # the new run records normally
    finally:
        rec.stop()


# --- start / stop / responsiveness ---------------------------------------------------------------
def test_start_and_stop_capture_through_the_page(harness, qtbot, ctx):
    h = harness()
    p = h.page
    gaps = Responsiveness()
    start_and_wait(qtbot, h, buffered=1.0)
    assert gaps.max_gap() < 0.25                                   # UI thread never blocked while starting
    assert p.state_badge.text().startswith("● Recording")
    assert not p.start_capture_btn.isEnabled() and p.stop_capture_btn.isEnabled() and p.clip_btn.isEnabled()
    assert not p.source_combo.isEnabled()
    assert p.selected_lbl.text() == "Source: Test pattern (built in, no device needed)"
    qtbot.waitUntil(lambda: p.buffer_lbl.text().startswith("Buffer 0:0"), timeout=3000)
    rec = h.ctrl.rec
    assert rec.output_args and "-force_key_frames" in rec.output_args      # live source is encoded
    # duplicate sessions are refused
    assert h.ctrl.start(h.page.selected_source()) == "Capture is already running."
    assert len(h.made) == 1
    seg = rec.segments[0].path
    info = ft.probe(ctx.ffmpeg.ffprobe, seg)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    gaps = Responsiveness()
    qtbot.mouseClick(p.stop_capture_btn, LEFT)
    qtbot.waitUntil(lambda: p._state == "stopped", timeout=15000)
    assert gaps.max_gap() < 0.25                                   # ... or stopping
    assert rec.state == "stopped" and not rec.running() and rec._lock_file is None
    assert p.start_capture_btn.isEnabled() and not p.clip_btn.isEnabled() and p.state_badge.text() == "Stopped"
    assert ctx.settings.capture.source == cap.TEST_KEY            # remembered for next time
    assert cfg.load(ctx.paths.settings_file)[0].capture.source == cap.TEST_KEY
    # and again: repeated start/stop works
    start_and_wait(qtbot, h)
    qtbot.mouseClick(p.stop_capture_btn, LEFT)
    qtbot.waitUntil(lambda: p._state == "stopped", timeout=15000)
    assert len(h.made) == 2


def test_ui_stays_responsive_while_ffmpeg_is_slow_to_stop(harness, qtbot, ctx, tmp_path, monkeypatch, ffmpeg_install):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"runs": [{"ignore_q": True}]}))
    monkeypatch.setenv("FAKE_FFMPEG_PLAN", str(plan))

    def slow_list():
        time.sleep(1.0)                                            # a slow DirectShow driver
        return fake_list()

    def make(src, audio):
        return FakeRing(ffmpeg_install.ffmpeg, ["-i", "x"], ctx.paths.buffer_dir, buffer_s=60, segment_s=0.5,
                        ffprobe=ffmpeg_install.ffprobe, min_free_bytes=1, poll_s=0.02)
    gaps = Responsiveness()
    h = harness(enumerate_fn=slow_list, factory=make)
    h.select(cap.TEST_KEY)
    h.ctrl.start(h.page.selected_source())
    assert h.ctrl.start(h.page.selected_source()) == "Capture is already starting or stopping."
    qtbot.waitUntil(lambda: h.ctrl.recording, timeout=5000)
    t0 = time.monotonic()
    qtbot.mouseClick(h.page.stop_capture_btn, LEFT)                   # ffmpeg ignores "q": ~5 s until terminate
    assert not h.page.stop_capture_btn.isEnabled() and not h.page.start_capture_btn.isEnabled()
    qtbot.waitUntil(lambda: h.page._state == "stopped", timeout=15000)
    assert time.monotonic() - t0 > 4.0
    assert gaps.max_gap() < 0.25


# --- Clip that ------------------------------------------------------------------------------------
def test_clip_that_end_to_end_into_results(harness, qtbot, ctx):
    """Production path: live-encoded test pattern -> Clip that -> clip on the Results page."""
    from twitchclipper.ui.pages.results import ResultsPage

    results = ResultsPage(ctx, confirm=lambda text: True)
    qtbot.addWidget(results)
    h = harness()
    start_and_wait(qtbot, h, buffered=2.5)
    rec = h.ctrl.rec
    gaps = Responsiveness()
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    assert h.page.clip_list.count() == 1 and "waiting for the last 1 s" in h.page.clip_list.item(0).text()
    seen_at_press = rec.stats.segments_seen
    states = []
    h.ctrl.status_changed.connect(lambda st: states.append(st["state"]))
    qtbot.waitUntil(lambda: "saved" in h.page.clip_list.item(0).text() or "not saved" in h.page.clip_list.item(0).text(),
                    timeout=30000)
    assert "saved capture_" in h.page.clip_list.item(0).text(), h.page.clip_list.item(0).text()
    assert rec.running() and rec.stats.segments_seen > seen_at_press      # recording carried on
    assert set(states) <= {"recording"}
    assert gaps.max_gap() < 0.25
    row = ctx.db.clips()[0]
    path = Path(row["path"])
    assert row["source"] == "capture" and path.parent == ctx.clips_dir and path.exists()
    assert abs(row["duration_s"] - 3.0) <= 0.25 and abs((row["window_end"] - row["window_start"]) - 3.0) < 1e-6
    info = ft.probe(ctx.ffmpeg.ffprobe, path)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert decode_file(ctx.ffmpeg.ffmpeg, path).frames >= 85
    assert rec.pinned_count() == 0
    # Results page shows it as a live capture and can delete it
    results.refresh()
    assert results.table.item(0, 1).text() == "Live capture"
    assert results.select_clip(row["id"])
    results.delete_selected()
    assert not path.exists() and ctx.db.clips() == []


def test_clip_contains_the_window_around_the_press(harness, qtbot, ctx, ffmpeg_install, clock_source):
    ctx.settings.capture.clip_before_s = 3.0
    h = harness(factory=file_factory(ctx, ffmpeg_install, clock_source))
    start_and_wait(qtbot, h, buffered=3.5)
    rec = h.ctrl.rec
    with rec.pin(lr.Window(0.0, 0.3)) as p:                      # calibrate stream time -> clock value
        first = p.segments[0]
        offset_ms = decode_file(ffmpeg_install.ffmpeg, first.path).first_ms - first.start * 1000
    ok, msg = h.ctrl.clip_that()
    assert ok, msg
    window = next(iter(h.ctrl.pending.values())).window
    assert abs(window.length - 4.0) < 1e-6
    qtbot.waitUntil(lambda: not h.ctrl.pending, timeout=30000)
    path = Path(ctx.db.clips()[0]["path"])
    dec = decode_file(ffmpeg_install.ffmpeg, path)
    assert dec.monotonic and dec.decode_rate == 1.0 and abs(dec.frames - 120) <= 1
    assert abs(dec.first_ms - (window.start * 1000 + offset_ms)) <= 34
    assert abs(dec.last_ms - (window.end * 1000 + offset_ms - 33.3)) <= 34


def test_not_enough_footage_yet_is_explained(harness, qtbot, ctx, ffmpeg_install, clock_source):
    ctx.settings.capture.clip_before_s = 22.0                     # the default
    ctx.settings.capture.clip_after_s = 5.0
    h = harness(factory=file_factory(ctx, ffmpeg_install, clock_source))
    ok, msg = h.ctrl.clip_that()
    assert not ok and msg.startswith("Start capture first")
    assert not h.page.clip_btn.isEnabled()
    start_and_wait(qtbot, h)
    qtbot.waitUntil(lambda: h.ctrl.rec.stream_now() is not None, timeout=10000)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    text = h.page.capture_msg.text()
    assert text.startswith("Only ") and "needs 22 s from before the moment" in text and "No clip was made." in text
    assert h.page.clip_list.count() == 0 and not h.ctrl.pending and h.ctrl.rec.pinned_count() == 0
    assert not list(ctx.clips_dir.glob("*.mp4")) if ctx.clips_dir.exists() else True
    qtbot.waitUntil(lambda: "Clip that needs 22 s: ready in" in h.page.buffer_lbl.text(), timeout=3000)


def test_repeated_presses(harness, qtbot, ctx, ffmpeg_install, clock_source, monkeypatch):
    monkeypatch.setattr(engine_capture, "DOUBLE_PRESS_S", 0.6)
    monkeypatch.setattr(engine_capture, "MAX_PENDING_CLIPS", 2)
    h = harness(factory=file_factory(ctx, ffmpeg_install, clock_source))
    start_and_wait(qtbot, h, buffered=2.5)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    qtbot.mouseClick(h.page.clip_btn, LEFT)                       # double click: one clip
    assert h.page.capture_msg.text() == "Already clipping that moment." and len(h.ctrl.pending) == 1
    qtbot.wait(700)
    qtbot.mouseClick(h.page.clip_btn, LEFT)                       # a separate, overlapping moment
    assert len(h.ctrl.pending) == 2                               # (the first still waits for its 1 s post-roll)
    monkeypatch.setattr(engine_capture, "DOUBLE_PRESS_S", 0.0)
    ok, msg = h.ctrl.clip_that()
    assert not ok and "still being made" in msg
    qtbot.waitUntil(lambda: not h.ctrl.pending, timeout=40000)
    rows = ctx.db.clips()
    assert len(rows) == 2 and len({r["path"] for r in rows}) == 2 and all(Path(r["path"]).exists() for r in rows)
    assert rows[0]["window_start"] != rows[1]["window_start"]
    assert h.ctrl.rec.pinned_count() == 0 and h.ctrl.rec.running()


def test_export_failure_is_reported_and_recording_continues(harness, qtbot, ctx, ffmpeg_install, clock_source,
                                                            monkeypatch, tmp_path):
    h = harness(factory=file_factory(ctx, ffmpeg_install, clock_source))
    start_and_wait(qtbot, h, buffered=2.5)

    def full_disk(segs, dst):
        raise OSError(28, "No space left on device")
    real_copy = lr._copy_into
    monkeypatch.setattr(lr, "_copy_into", full_disk)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    qtbot.waitUntil(lambda: "not saved" in h.page.clip_list.item(0).text(), timeout=30000)
    assert "is full" in h.page.clip_list.item(0).text() and "is full" in h.page.capture_msg.text()
    assert ctx.db.clips() == [] and h.ctrl.rec.pinned_count() == 0 and h.ctrl.rec.running()
    assert not list(ctx.clips_dir.glob("*")) if ctx.clips_dir.exists() else True
    monkeypatch.setattr(lr, "_copy_into", real_copy)
    blocked = tmp_path / "not_a_folder"
    blocked.write_text("x")
    ctx.settings.export.output_dir = str(blocked)                  # clips folder unusable
    qtbot.wait(2100)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    qtbot.waitUntil(lambda: h.page.clip_list.count() == 2 and "not saved" in h.page.clip_list.item(0).text(),
                    timeout=30000)
    assert ctx.db.clips() == [] and h.ctrl.rec.running()
    ctx.settings.export.output_dir = ""                            # fixed: the next clip works
    qtbot.wait(2100)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    qtbot.waitUntil(lambda: len(ctx.db.clips()) == 1, timeout=30000)


def test_stop_right_after_clip_that_waits_for_the_post_roll(harness, qtbot, ctx, ffmpeg_install, clock_source):
    ctx.settings.capture.clip_after_s = 2.0
    h = harness(factory=file_factory(ctx, ffmpeg_install, clock_source))
    start_and_wait(qtbot, h, buffered=2.5)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    qtbot.mouseClick(h.page.stop_capture_btn, LEFT)
    assert "pending clip" in h.page.capture_msg.text()
    qtbot.waitUntil(lambda: h.page._state == "stopped" and not h.ctrl.pending, timeout=30000)
    rows = ctx.db.clips()
    assert len(rows) == 1 and abs(rows[0]["duration_s"] - 4.0) <= 0.25


def test_clip_fails_cleanly_when_the_source_dies_before_the_post_roll(harness, qtbot, ctx, ffmpeg_install,
                                                                      clock_source):
    ctx.settings.capture.clip_after_s = 3.0
    h = harness(factory=file_factory(ctx, ffmpeg_install, clock_source))
    start_and_wait(qtbot, h, buffered=2.5)
    qtbot.mouseClick(h.page.clip_btn, LEFT)
    h.ctrl.rec.stop()                                              # the recorder ends underneath the clip
    qtbot.waitUntil(lambda: "not saved" in h.page.clip_list.item(0).text(), timeout=30000)
    assert "before the seconds after the moment were recorded" in h.page.clip_list.item(0).text()
    assert ctx.db.clips() == [] and h.ctrl.rec.pinned_count() == 0


def test_closing_the_app_while_capturing(qtbot, app_home):
    from twitchclipper.ui.main_window import MainWindow

    c = create_context()
    c.settings.capture.clip_before_s, c.settings.capture.clip_after_s = 2.0, 1.0
    win = MainWindow(c, detect_encoders=False)
    qtbot.addWidget(win)
    win.show()
    page = win.pages["live"]
    ctrl = page.capture
    win.navigate("live")
    qtbot.waitUntil(lambda: page.source_combo.findData(cap.TEST_KEY) >= 0, timeout=10000)
    page.source_combo.setCurrentIndex(page.source_combo.findData(cap.TEST_KEY))
    page.start_capture()
    qtbot.waitUntil(lambda: ctrl.recording and ctrl.rec.stats.segments_seen >= 2, timeout=20000)
    rec = ctrl.rec
    pid = rec.proc.pid
    win.close()
    assert rec.state == "stopped" and not rec.running()
    assert not list(c.paths.buffer_dir.glob("seg_*.ts"))           # buffer files removed on exit
    from tests.test_ring_buffer import process_alive
    assert not process_alive(pid)


# --- Settings → Capture ----------------------------------------------------------------------------
def test_capture_settings_defaults_and_validation(tmp_path):
    s = cfg.Settings()
    assert (s.capture.clip_before_s, s.capture.clip_after_s, s.capture.buffer_s) == (22.0, 5.0, 120.0)
    assert s.validate() == []
    s.capture.clip_before_s, s.capture.clip_after_s, s.capture.buffer_s = 500.0, -3.0, 5.0
    warnings = s.validate()
    assert (s.capture.clip_before_s, s.capture.clip_after_s) == (120.0, 0.0)
    assert s.capture.buffer_s == 150.0 and any("can't hold" in w for w in warnings)
    s.capture.clip_before_s = float("nan")
    s.validate()
    assert s.capture.clip_before_s == 22.0
    f = tmp_path / "settings.toml"
    f.write_text('[capture]\nclip_before_s = "lots"\nclip_after_s = 1e9\nbuffer_s = -1\n', encoding="utf-8")
    loaded, warn = cfg.load(f)
    assert loaded.capture.clip_before_s == 22.0                    # wrong type ignored
    assert loaded.capture.clip_after_s == 60.0 and loaded.capture.buffer_s == 112.0 and warn


def test_settings_page_capture_tab(qtbot, app_home):
    from twitchclipper.engine.jobs import JobRunner
    from twitchclipper.ui.pages.settings import SettingsPage

    c = create_context()
    page = SettingsPage(c, JobRunner())
    qtbot.addWidget(page)
    tabs = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert "Capture" in tabs
    assert (page.cap_before.value(), page.cap_after.value(), page.cap_buffer.value()) == (22.0, 5.0, 120.0)
    page.cap_before.setValue(9999)                                  # spin boxes refuse out-of-range input
    page.cap_after.setValue(-5)
    assert (page.cap_before.value(), page.cap_after.value()) == (120.0, 0.0)
    page.cap_buffer.setValue(60)
    page.save()
    assert c.settings.capture.buffer_s == 150.0 and "can't hold" in page.message.text()
    assert cfg.load(c.paths.settings_file)[0].capture.clip_before_s == 120.0
    page.cap_before.setValue(10)
    page.cap_after.setValue(3)
    page.save()
    live = LiveMonitorPage(c, controller=CaptureController(c, enumerate_fn=fake_list))
    qtbot.addWidget(live)
    assert "10 s before the press + 3 s after it (13 s)" in live.window_lbl.text()
    c.db.close()
