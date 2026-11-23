"""Layout at different window sizes and display scaling.

Windows display scaling shrinks the *logical* screen Qt lays out on: a 1920x1080 display with a
40 px taskbar gives 1920x1040 at 100 %, 1536x832 at 125 %, 1280x693 at 150 % and 960x520 at 200 %.
These tests size the window for each of those screens and check every page:
  - the window fits on the screen,
  - nothing needs horizontal scrolling,
  - at 100 % and 125 % nothing needs vertical scrolling either,
  - no control is squeezed below the size it needs to show its text.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QLabel, QLineEdit, QWidget,
)

from twitchclipper.core.context import create_context
from twitchclipper.ui import theme
from twitchclipper.ui.main_window import MIN_SIZE, NAV, MainWindow

SCREENS = {100: (1920, 1040), 125: (1536, 832), 150: (1280, 693), 200: (960, 520)}
TITLE_BAR = 40


class FakeScreen:
    def __init__(self, w, h):
        self._r = QRect(0, 0, w, h)

    def availableGeometry(self):  # noqa: N802 - Qt naming
        return self._r


@pytest.fixture
def window(qtbot, qapp, app_home):
    theme.apply(qapp, dark=False)
    win = MainWindow(create_context(), detect_encoders=False)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.runner.wait(10000)


def squeezed_controls(page: QWidget) -> list[str]:
    """Visible controls whose size is below their minimum size hint (text cut off / overlapping)."""
    bad = []
    for w in page.findChildren(QWidget):
        if not w.isVisibleTo(page):
            continue
        if isinstance(w, QLabel) and (w.wordWrap() or not w.text()):
            continue
        if not isinstance(w, (QAbstractButton, QComboBox, QAbstractSpinBox, QLineEdit, QLabel)):
            continue
        need = w.minimumSizeHint().expandedTo(w.minimumSize())
        if w.width() < need.width() - 1 or w.height() < need.height() - 1:
            text = w.text() if hasattr(w, "text") and callable(w.text) else ""
            bad.append(f"{type(w).__name__} '{text[:30]}' {w.width()}x{w.height()} < {need.width()}x{need.height()}")
    return bad


def check_pages(qapp, win, allow_vertical_scroll: bool) -> list[str]:
    problems = []
    for key, _, _ in NAV:
        win.navigate(key)
        for _ in range(3):
            qapp.processEvents()
        scroll = win.scrolls[key]
        if scroll.horizontalScrollBar().maximum() > 0:
            problems.append(f"{key}: horizontal scrolling needed ({scroll.horizontalScrollBar().maximum()} px)")
        if not allow_vertical_scroll and scroll.verticalScrollBar().maximum() > 0:
            problems.append(f"{key}: vertical scrolling needed ({scroll.verticalScrollBar().maximum()} px)")
        problems += [f"{key}: {s}" for s in squeezed_controls(win.pages[key])]
    return problems


@pytest.mark.parametrize("scale", sorted(SCREENS))
def test_window_fits_screen_and_pages_adapt(qapp, window, scale):
    aw, ah = SCREENS[scale]
    window.fit_to_screen(FakeScreen(aw, ah))
    assert window.width() <= aw and window.height() + TITLE_BAR <= ah, (window.width(), window.height())
    assert window.minimumWidth() <= window.width() and window.minimumHeight() <= window.height()
    if scale == 100:
        assert (window.width(), window.height()) == (1320, 860)          # unchanged default on a normal screen
    problems = check_pages(qapp, window, allow_vertical_scroll=scale >= 150)
    assert problems == [], "\n".join(problems)


def test_minimum_window_size(qapp, window):
    window.resize(MIN_SIZE)
    qapp.processEvents()
    assert (window.width(), window.height()) == (MIN_SIZE.width(), MIN_SIZE.height())
    problems = check_pages(qapp, window, allow_vertical_scroll=True)
    assert problems == [], "\n".join(problems)


def test_large_window(qapp, window):
    window.resize(2400, 1300)
    qapp.processEvents()
    assert check_pages(qapp, window, allow_vertical_scroll=False) == []
    page = window.pages["analyze"]
    assert page.width() == window.scrolls["analyze"].viewport().width()   # page fills the space


def test_page_scroll_follows_content_changes(qapp, window):
    """A status line appearing makes the page taller; the scroll area must pick that up."""
    window.resize(MIN_SIZE)
    window.navigate("analyze")
    qapp.processEvents()
    page, scroll = window.pages["analyze"], window.scrolls["analyze"]
    before = page.height()
    page.status.setText("A long status message " * 20)
    page.progress.show()
    for _ in range(3):
        qapp.processEvents()
    assert page.height() >= page.minimumSizeHint().height() and page.height() > before
    assert scroll.verticalScrollBar().maximum() > 0


def test_dashboard_cards_wrap_on_narrow_windows(qapp, window):
    dash = window.pages["dashboard"]
    window.navigate("dashboard")
    window.resize(1320, 860)
    qapp.processEvents()
    assert dash.card_columns == 5
    window.resize(MIN_SIZE)
    for _ in range(3):
        qapp.processEvents()
    assert dash.card_columns == 3 and window.scrolls["dashboard"].horizontalScrollBar().maximum() == 0
    window.resize(1320, 860)
    for _ in range(3):
        qapp.processEvents()
    assert dash.card_columns == 5


def test_spin_box_arrows_are_drawn(qapp):
    t = theme.apply(qapp, dark=True)
    css = theme.stylesheet(t)
    for direction in ("up", "down"):
        path = Path(theme.arrow_image(direction, t.text_muted))
        assert path.is_file() and path.stat().st_size > 0 and path.as_posix() in css
    theme.apply(qapp, dark=False)


def test_renders_at_150_percent_device_pixel_ratio(tmp_path):
    """Real high-DPI rendering (device pixel ratio 1.5, Qt's own scaling layer as used for Windows
    display scaling) in a separate process with a fake 1920x1040 screen -> 1280x693 logical."""
    (tmp_path / "screen.json").write_text(
        '{"screens":[{"name":"Fake","x":0,"y":0,"width":1920,"height":1040,"logicalDpi":96,'
        '"logicalBaseDpi":96,"dpr":1}]}')
    script = tmp_path / "render.py"
    script.write_text(
        "import sys\n"
        "from PySide6.QtWidgets import QApplication\n"
        "from twitchclipper.core.context import create_context\n"
        "from twitchclipper.ui import theme\n"
        "from twitchclipper.ui.main_window import MainWindow, NAV\n"
        "app = QApplication(sys.argv); theme.apply(app, dark=False)\n"
        "w = MainWindow(create_context(), detect_encoders=False); w.show(); app.processEvents()\n"
        "s = app.primaryScreen()\n"
        "assert s.devicePixelRatio() == 1.5 and (s.availableGeometry().width(), s.availableGeometry().height()) == (1280, 693)\n"
        "assert w.width() <= 1280 and w.height() + 40 <= 693\n"
        "for key, _, _ in NAV:\n"
        "    w.navigate(key); app.processEvents()\n"
        "    img = w.grab().toImage()\n"
        "    assert img.devicePixelRatio() == 1.5 and img.width() == round(w.width() * 1.5), key\n"
        "    assert len({img.pixel(x, y) for x in range(0, img.width(), 37) for y in range(0, img.height(), 41)}) > 5, key\n"
        "    assert w.scrolls[key].horizontalScrollBar().maximum() == 0, key\n"
        "print('ok', w.width(), w.height())\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json", QT_SCALE_FACTOR="1.5",
               TWITCHCLIPPER_HOME=str(tmp_path / "home"),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    out = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().startswith("ok")
