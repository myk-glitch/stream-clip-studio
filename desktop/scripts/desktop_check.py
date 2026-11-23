"""Desktop check of the Results page on the real Windows platform (not offscreen).

    uv run python scripts/desktop_check.py [--close-player]

Runs against a throw-away app home (your library is untouched), then:
  1. shows the real main window and imports a copy of test_media/twitchclipper_test_pattern_12s.mp4
  2. presses Play and checks playback state, position, frames, the audio track, and that the test
     pattern is actually visible on screen inside the preview (desktop capture)
  3. presses "Show in folder" and confirms through the Windows Shell that Explorer opened the right
     folder with the clip selected (then closes that Explorer window)
  4. presses "Open" and checks that your default video player comes to the front showing the clip
     (left open unless --close-player is given)
  5. presses Close on the preview and deletes the copy; if the external player still holds the file,
     the app's "file in use" message is expected
  6. portrait: exports a 9:16 clip from the fixture, imports it, checks the first frame is on screen
     before Play and is displayed 9:16 (pillarboxed, not stretched), then deletes it while shown
  7. confirms the original fixture is byte-identical
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["TWITCHCLIPPER_HOME"] = tempfile.mkdtemp(prefix="tc-desktop-check-")

from PySide6.QtCore import QEventLoop, Qt  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from twitchclipper.core.context import create_context  # noqa: E402
from twitchclipper.export import ffmpeg as ft  # noqa: E402
from twitchclipper.pipeline import local as pipeline  # noqa: E402
from twitchclipper.timing.window import Window  # noqa: E402
from twitchclipper.ui import theme  # noqa: E402
from twitchclipper.ui.main_window import MainWindow  # noqa: E402

FIXTURE = ROOT / "test_media" / "twitchclipper_test_pattern_12s.mp4"
OUT = Path(os.environ.get("TC_CHECK_OUT", tempfile.gettempdir()))
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def wait_until(pred, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def pump(seconds: float) -> None:
    wait_until(lambda: False, seconds)


def ps(script: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                          timeout=60).stdout.strip()


EXPLORER_WINDOWS = r"""
$sh = New-Object -ComObject Shell.Application
$out = @()
foreach ($w in $sh.Windows()) { try {
  $sel = @($w.Document.SelectedItems() | ForEach-Object { $_.Path })
  $out += [pscustomobject]@{ hwnd = $w.HWND; folder = $w.Document.Folder.Self.Path; selected = $sel }
} catch {} }
ConvertTo-Json -InputObject @($out) -Depth 3 -Compress
"""

def explorer_windows() -> list[dict]:
    raw = ps(EXPLORER_WINDOWS)
    return json.loads(raw) if raw else []


def close_explorer_window(hwnd: int) -> None:
    ps(f"$sh = New-Object -ComObject Shell.Application; foreach ($w in $sh.Windows()) "
       f"{{ if ($w.HWND -eq {hwnd}) {{ $w.Quit() }} }}")


user32 = ctypes.windll.user32


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def grab_hwnd(hwnd: int):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    screen = QApplication.primaryScreen()
    g = screen.geometry()
    return screen.grabWindow(0, r.left - g.x(), r.top - g.y(), r.right - r.left, r.bottom - r.top).toImage()


def count_bars(img, step: int = 6) -> tuple[int, int]:
    """Samples that match the test pattern's pure red and pure blue bars."""
    reds = blues = 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            c = img.pixelColor(x, y)
            reds += c.red() > 190 and c.green() < 70 and c.blue() < 70
            blues += c.blue() > 190 and c.red() < 70 and c.green() < 70
    return reds, blues


def main() -> int:
    if not FIXTURE.exists():
        print("Run scripts/make_test_media.py first.")
        return 2
    digest = ft.sha256_file(FIXTURE)
    app = QApplication(sys.argv)
    check("real Windows platform", app.platformName() == "windows", app.platformName())
    theme.apply(app)
    win = MainWindow(create_context(), detect_encoders=False)
    win.show()
    win.navigate("results")
    page = win.pages["results"]
    pump(0.5)

    # 1. import
    page.import_video(FIXTURE)
    wait_until(lambda: page.table.rowCount() == 1 and page.import_btn.isEnabled(), 30)
    clip = page.selected()
    copy = Path(clip["path"]) if clip else None
    check("import shows a selectable clip", bool(copy and copy.exists() and copy != FIXTURE), str(copy))

    # 2. play
    player = page.preview.player
    wait_until(lambda: player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia,
                                                QMediaPlayer.MediaStatus.BufferedMedia), 10)
    check("preview loaded with video and audio tracks", player.hasVideo() and player.hasAudio(), page.preview.info.text())
    frames = []
    page.preview.video.videoSink().videoFrameChanged.connect(lambda f: frames.append(1))
    QTest.mouseClick(page.preview.play_btn, Qt.MouseButton.LeftButton)
    playing = wait_until(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState, 5)
    wait_until(lambda: player.position() >= 4000, 10)
    pos = player.position()
    check("Play starts playback", playing and pos >= 4000, f"position {pos} ms, button '{page.preview.play_btn.text()}'")
    check("video frames rendered on screen", len(frames) >= 90, f"{len(frames)} frames in ~4 s")
    audio = page.preview.audio
    check("audio routed to default output", not audio.isMuted() and audio.volume() > 0,
          f"device '{audio.device().description()}', volume {audio.volume():.1f}")
    # what is actually on screen inside the video area (desktop capture, includes GPU-composited video)
    win.raise_()
    win.activateWindow()
    pump(0.5)
    vw = page.preview.video
    top_left = vw.mapToGlobal(vw.rect().topLeft())
    screen = win.screen()
    shot_img = screen.grabWindow(0, top_left.x() - screen.geometry().x(), top_left.y() - screen.geometry().y(),
                                 vw.width(), vw.height()).toImage()
    shot = OUT / "tc_desktop_check_video_area.png"
    shot_img.save(str(shot))
    full = OUT / "tc_desktop_check_window.png"
    g = win.frameGeometry()
    screen.grabWindow(0, g.x() - screen.geometry().x(), g.y() - screen.geometry().y(), g.width(), g.height()).save(str(full))
    reds, blues = count_bars(shot_img)
    check("video is visible on screen (test-pattern bars in the video area)", reds > 20 and blues > 20,
          f"red samples {reds}, blue samples {blues}; {shot}")
    print(f"window screenshot: {full}")
    QTest.mouseClick(page.preview.play_btn, Qt.MouseButton.LeftButton)   # pause
    wait_until(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PausedState, 5)

    # 3. Show in folder (real Explorer)
    before = {w["hwnd"] for w in explorer_windows()}
    QTest.mouseClick(page.folder_btn, Qt.MouseButton.LeftButton)
    found: list[dict] = []

    def explorer_opened() -> bool:
        found[:] = [w for w in explorer_windows() if w["hwnd"] not in before]
        return bool(found)

    def selection_applied() -> bool:   # Explorer opens the window first and selects the file a moment later
        explorer_opened()
        return bool(found) and str(copy) in (found[0].get("selected") or [])

    wait_until(explorer_opened, 15)
    wait_until(selection_applied, 10)
    w = found[0] if found else {}
    sel = [Path(p) for p in (w.get("selected") or [])]
    check("Show in folder opens Explorer at the clip's folder",
          bool(w) and Path(w.get("folder", "")) == copy.parent, w.get("folder", "no new Explorer window"))
    check("Explorer has the clip selected", copy in sel, ", ".join(map(str, sel)) or "nothing selected")
    if w:
        close_explorer_window(w["hwnd"])

    # 4. Open (default player): it should come to the front showing this clip
    own = int(win.winId())
    QTest.mouseClick(page.open_btn, Qt.MouseButton.LeftButton)
    fg = {"hwnd": 0, "title": ""}

    def player_in_front() -> bool:
        h = user32.GetForegroundWindow()
        if h and h != own:
            fg.update(hwnd=h, title=window_title(h))
        return bool(fg["hwnd"]) and fg["title"] not in ("", "TwitchClipper")

    wait_until(player_in_front, 20)
    check("Open brings up the default video app", player_in_front(), f"foreground window '{fg['title']}'")
    reds = blues = 0
    if fg["hwnd"]:
        pump(4)   # let the player start playing
        img = grab_hwnd(fg["hwnd"])
        img.save(str(OUT / "tc_desktop_check_default_player.png"))
        reds, blues = count_bars(img)
    check("default player shows the test clip", reds > 20 and blues > 20,
          f"red samples {reds}, blue samples {blues}; {OUT / 'tc_desktop_check_default_player.png'}")
    if "--close-player" in sys.argv and fg["hwnd"]:
        user32.PostMessageW(fg["hwnd"], 0x0010, 0, 0)   # WM_CLOSE to that one window, like clicking X
        pump(1.5)

    # 5. Close + delete
    QTest.mouseClick(page.preview.close_btn, Qt.MouseButton.LeftButton)
    check("Close releases the preview", page.preview.source_path == "" and not page.preview.play_btn.isEnabled())
    warnings: list[str] = []
    page.warn = lambda title, text: warnings.append(text)
    page.confirm = lambda text: True
    page.delete_selected()
    if copy.exists():
        print("  the copy is still open in the external player, as expected; the app showed:", warnings[:1])
        check("app reports 'file in use' instead of hanging", bool(warnings) and "close it and try again" in warnings[0].lower())
    else:
        check("copy deleted", page.table.rowCount() == 0)

    # 6. portrait 9:16 clip: first frame shown before Play, pillarboxed (not stretched), deletable while paused
    inst = ft.best_install()
    portrait = Path(os.environ["TWITCHCLIPPER_HOME"]) / "portrait_check.mp4"
    pipeline.export(inst.ffmpeg, inst.ffprobe, pipeline.ExportJob(FIXTURE, Window(2.0, 10.0), portrait, "portrait",
                                                                  0.5, "libx264"))
    page.import_video(portrait)
    wait_until(lambda: page.table.rowCount() == 1 and page.import_btn.isEnabled(), 30)
    pclip = Path(page.selected()["path"])
    wait_until(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PausedState, 10)
    win.raise_()
    win.activateWindow()
    pump(1.0)
    vw = page.preview.video
    tl = vw.mapToGlobal(vw.rect().topLeft())
    area = screen.grabWindow(0, tl.x() - screen.geometry().x(), tl.y() - screen.geometry().y(),
                             vw.width(), vw.height()).toImage()
    area.save(str(OUT / "tc_desktop_check_portrait.png"))
    xs, ys = [], []
    for y in range(0, area.height(), 3):
        for x in range(0, area.width(), 3):
            c = area.pixelColor(x, y)
            if c.hsvSaturation() > 120 and c.value() > 120:      # the coloured test bars, not black bars/UI
                xs.append(x)
                ys.append(y)
    aspect = (max(xs) - min(xs)) / max(1, max(ys) - min(ys)) if xs else 0.0
    check("portrait: first frame shown before Play", bool(xs) and player.position() == 0,
          f"{len(xs)} coloured samples, state {player.playbackState().name}")
    check("portrait: displayed 9:16, not stretched", abs(aspect - 9 / 16) < 0.06,
          f"picture {max(xs) - min(xs) if xs else 0}x{max(ys) - min(ys) if ys else 0} px, aspect {aspect:.3f} "
          f"(9:16 = 0.5625) in a {vw.width()}x{vw.height()} area; {OUT / 'tc_desktop_check_portrait.png'}")
    page.delete_selected()          # still shown (paused) in the preview: delete must release it
    check("portrait: deleted while shown in the preview", not pclip.exists() and page.table.rowCount() == 0)

    # 7. original intact
    check("original fixture unchanged", FIXTURE.exists() and ft.sha256_file(FIXTURE) == digest)
    win.close()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    print(f"app home used: {os.environ['TWITCHCLIPPER_HOME']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
