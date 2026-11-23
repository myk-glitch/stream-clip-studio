"""Small helpers shared by several test files."""

import time
from pathlib import Path


def wait_for(pred, timeout=15.0, step=0.02) -> bool:
    """Poll `pred` until it is true or the timeout ends; returns its final value."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class RecordingLauncher:
    """Stands in for the desktop launcher and records what would have been opened."""

    def __init__(self):
        self.calls = []

    def open(self, path):
        self.calls.append(("open", Path(path)))

    def reveal(self, path):
        self.calls.append(("reveal", Path(path)))


def import_via_ui(qtbot, page, src: Path):
    """Import a video through the Results page and return the selected row."""
    with qtbot.waitSignal(page.ctx.events.clips_changed, timeout=30000):
        job = page.import_video(src)
    assert job is not None
    qtbot.waitUntil(lambda: page.import_btn.isEnabled(), timeout=10000)
    return page.selected()
