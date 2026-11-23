"""Main window: sidebar navigation + stacked pages + status bar."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QScrollArea,
    QStackedWidget, QStyle, QVBoxLayout, QWidget,
)

from ..core.context import AppContext
from ..engine.jobs import Job, JobRunner
from .pages.account import AccountPage
from .pages.analyze import AnalyzePage
from .pages.dashboard import DashboardPage
from .pages.live_monitor import LiveMonitorPage
from .pages.results import ResultsPage
from .pages.settings import SettingsPage

DEFAULT_SIZE = QSize(1320, 860)
MIN_SIZE = QSize(860, 560)


class PageScroll(QScrollArea):
    """Holds a page. The page always fills the visible area; scroll bars appear only when the window
    is smaller than the page's *minimum* size (instead of Qt squeezing widgets on top of each other).

    QScrollArea's own widgetResizable mode sizes pages with word-wrapped labels to their preferred
    height, which made pages scroll even when everything fitted, so the sizing is done here.
    """

    def __init__(self, page: QWidget):
        super().__init__()
        self.setObjectName("PageScroll")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(False)
        page.setObjectName("Page")
        self.setWidget(page)
        page.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.fit()   # the page's minimum size may have changed (e.g. a status line appeared)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit()

    def fit(self) -> None:
        page = self.widget()
        if page is None:
            return
        if hasattr(page, "arrange_for_width"):
            # responsive pages pick their arrangement for the visible width before being measured
            page.arrange_for_width(self.maximumViewportSize().width())
        need = page.minimumSizeHint().expandedTo(page.minimumSize())
        layout = page.layout()
        hfw = layout is not None and layout.hasHeightForWidth()

        def min_height(width: int) -> int:
            # word-wrapped text needs more lines on narrow pages; plain minimumSizeHint ignores that
            return max(need.height(), layout.totalMinimumHeightForWidth(width)) if hfw else need.height()

        room = self.maximumViewportSize()          # visible area without scroll bars
        bar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        w_short = need.width() > room.width()
        width = max(need.width(), room.width())
        h_short = min_height(width) > room.height() - (bar if w_short else 0)
        if h_short:                                 # a vertical bar takes width away: re-check
            width = max(need.width(), room.width() - bar)
            if not w_short and need.width() > room.width() - bar:
                w_short = True
        height = max(min_height(width), room.height() - (bar if w_short else 0))
        if page.size() != QSize(width, height):
            page.resize(width, height)


def page_scroll(page: QWidget) -> PageScroll:
    return PageScroll(page)


NAV = [
    ("dashboard", "Dashboard", QStyle.StandardPixmap.SP_DesktopIcon),
    ("live", "Live Monitor", QStyle.StandardPixmap.SP_MediaPlay),
    ("analyze", "Analyze", QStyle.StandardPixmap.SP_FileDialogContentsView),
    ("results", "Results", QStyle.StandardPixmap.SP_DirOpenIcon),
    ("account", "Account", QStyle.StandardPixmap.SP_DialogApplyButton),
    ("settings", "Settings", QStyle.StandardPixmap.SP_FileDialogDetailedView),
]


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, detect_encoders: bool = True):
        super().__init__()
        self.ctx = ctx
        self.runner = JobRunner(self)
        self.setWindowTitle("TwitchClipper")
        self.fit_to_screen()

        side = QWidget()
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        title = QLabel("TwitchClipper")
        title.setObjectName("AppTitle")
        self.nav = QListWidget()
        self.nav.setObjectName("Sidebar")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setTextElideMode(Qt.TextElideMode.ElideRight)
        sl.addWidget(title)
        sl.addWidget(self.nav, 1)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "dashboard": DashboardPage(ctx, self.navigate),
            "live": LiveMonitorPage(ctx),
            "analyze": AnalyzePage(ctx, self.runner),
            "results": ResultsPage(ctx, self.runner),
            "account": AccountPage(ctx, self.runner),
            "settings": SettingsPage(ctx, self.runner),
        }
        self._keys = []
        self.scrolls: dict[str, QScrollArea] = {}
        for key, label, icon in NAV:
            item = QListWidgetItem(self.style().standardIcon(icon), label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
            self.scrolls[key] = scroll = page_scroll(self.pages[key])
            self.stack.addWidget(scroll)
            self._keys.append(key)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        central = QWidget()
        cl = QHBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(side)
        cl.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.sb_ffmpeg = QLabel()
        self.sb_encoder = QLabel()
        self.sb_account = QLabel()
        for w in (self.sb_ffmpeg, self.sb_encoder, self.sb_account):
            self.statusBar().addPermanentWidget(w)
        for sig in (ctx.events.ffmpeg_changed, ctx.events.account_changed, ctx.events.settings_changed):
            sig.connect(self.update_status)
        self.update_status()
        if detect_encoders:
            self.start_encoder_detection()

    def fit_to_screen(self, screen=None) -> None:
        """Default 1320x860, but never larger than the screen's usable area (e.g. 1280x693 at 150 % on a
        1080p display). Pages scroll instead of squeezing when the window is smaller than they need."""
        screen = screen or self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        frame_h = 40   # title bar allowance (logical px)
        w = min(DEFAULT_SIZE.width(), int(avail.width() * 0.95))
        h = min(DEFAULT_SIZE.height(), int((avail.height() - frame_h) * 0.98))
        self.setMinimumSize(min(MIN_SIZE.width(), w), min(MIN_SIZE.height(), h))
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2, avail.y() + max(0, (avail.height() - frame_h - h) // 2))

    def navigate(self, key: str) -> None:
        self.nav.setCurrentRow(self._keys.index(key))

    def current_key(self) -> str:
        return self._keys[self.nav.currentRow()]

    def start_encoder_detection(self, force: bool = False) -> None:
        if not self.ctx.ffmpeg:
            return
        self.statusBar().showMessage("Checking which video encoders work on this PC…")
        job = Job(lambda: self.ctx.detect_encoders(force=force), pass_progress=False)

        def done(_):
            self.statusBar().showMessage("Encoder check finished", 4000)
            self.ctx.events.ffmpeg_changed.emit()

        job.signals.finished.connect(done)
        job.signals.failed.connect(lambda m: self.statusBar().showMessage("Encoder check failed: " + m.splitlines()[0]))
        self.runner.submit(job)

    def update_status(self) -> None:
        ctx = self.ctx
        self.sb_ffmpeg.setText("FFmpeg ✓" if ctx.ffmpeg else "FFmpeg ✗")
        enc = ctx.encoder_for_export()[0] if ctx.encoders else ("testing…" if ctx.ffmpeg else "-")
        self.sb_encoder.setText(f"Encoder: {enc}")
        self.sb_account.setText(f"Twitch: {ctx.account_login}" if ctx.account_login else "Twitch: not signed in")

    def closeEvent(self, event) -> None:
        for page in self.pages.values():
            if hasattr(page, "shutdown"):
                page.shutdown()          # e.g. stop Mode D capture, let a running clip finish
            if hasattr(page, "cancel_exports"):
                page.cancel_exports()
            if hasattr(page, "preview"):
                page.preview.stop()
        self.runner.wait(10000)
        self.ctx.db.close()
        super().closeEvent(event)
