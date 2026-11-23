"""Small reusable widgets: page header, stat card, notice, capability badge."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import theme


def page_header(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 8)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


class StatCard(QFrame):
    def __init__(self, title: str, value: str = "-", detail: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        self.value = QLabel(value)
        self.value.setObjectName("CardValue")
        self.detail = QLabel(detail)
        self.detail.setObjectName("CardDetail")
        self.detail.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.detail)
        lay.addStretch(1)

    def set(self, value: str, detail: str = "", status: str | None = None) -> None:
        self.value.setText(value)
        self.detail.setText(detail)
        t = theme.tokens()
        color = {"ok": t.ok, "warn": t.warn, "bad": t.bad}.get(status or "", t.text)
        self.value.setStyleSheet(f"color: {color};")


def notice(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Notice")
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setOpenExternalLinks(True)
    return lab


def breakable(text: str) -> str:
    """Let word-wrapped labels break long paths/URLs after separators (zero-width spaces), so one long
    path can't force a page wider than the window."""
    return text.replace("\\", "\\​").replace("/", "/​")


class StatusLabel(QLabel):
    """Muted, word-wrapped status line that takes no space while it is empty."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Muted")
        self.setWordWrap(True)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self.setVisible(bool(text))


class Badge(QLabel):
    """Pill showing whether a capability is available, e.g. 'Chat ✓' or 'Download ✗'."""

    def __init__(self, text: str = "", ok: bool | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.set(text, ok)

    def set(self, text: str, ok: bool | None) -> None:
        t = theme.tokens()
        color = t.text_muted if ok is None else (t.ok if ok else t.bad)
        mark = "" if ok is None else (" ✓" if ok else " ✗")
        self.setText(text + mark)
        self.setStyleSheet(f"border: 1px solid {color}; color: {color}; border-radius: 10px; padding: 2px 10px;")


def hbox(*widgets: QWidget, stretch_end: bool = True, spacing: int = 8) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        lay.addWidget(x)
    if stretch_end:
        lay.addStretch(1)
    return w


def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
