"""Light/dark theme following the Windows setting, with a neutral indigo accent."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPalette, QPolygonF
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Tokens:
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    accent: str
    accent_text: str
    ok: str
    warn: str
    bad: str
    plot_bg: str
    plot_line: str


LIGHT = Tokens("#f5f6f8", "#ffffff", "#eef0f4", "#dde1e8", "#1d2129", "#5f6673", "#5b5bd6", "#ffffff",
               "#1f8f4e", "#b7791f", "#c53030", "#ffffff", "#5b5bd6")
DARK = Tokens("#15171c", "#1e2128", "#262a33", "#323743", "#e8eaf0", "#9aa1ae", "#8b8bf0", "#0f1014",
              "#48bb78", "#ecc94b", "#fc8181", "#1e2128", "#9d9df5")

_current = LIGHT


def tokens() -> Tokens:
    return _current


def is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    try:
        return hints.colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return False


def apply(app: QApplication, dark: bool | None = None) -> Tokens:
    global _current
    _current = DARK if (is_dark() if dark is None else dark) else LIGHT
    t = _current
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in [
        (QPalette.ColorRole.Window, t.bg), (QPalette.ColorRole.WindowText, t.text),
        (QPalette.ColorRole.Base, t.surface), (QPalette.ColorRole.AlternateBase, t.surface_alt),
        (QPalette.ColorRole.Text, t.text), (QPalette.ColorRole.Button, t.surface),
        (QPalette.ColorRole.ButtonText, t.text), (QPalette.ColorRole.Highlight, t.accent),
        (QPalette.ColorRole.HighlightedText, t.accent_text), (QPalette.ColorRole.ToolTipBase, t.surface),
        (QPalette.ColorRole.ToolTipText, t.text), (QPalette.ColorRole.PlaceholderText, t.text_muted),
    ]:
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t.text_muted))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t.text_muted))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(t))
    return t


def arrow_image(direction: str, color: str) -> str:
    """Small up/down triangle PNG for spin box buttons, drawn at 4x so it stays sharp up to 400 % scaling.

    Styling a spin box's frame hides Qt's built-in arrows, and Qt style sheets can't draw CSS border
    triangles, so the arrows are tiny generated images (cached in the temp folder)."""
    folder = Path(tempfile.gettempdir()) / "twitchclipper-theme"
    path = folder / f"arrow-{direction}-{color.lstrip('#')}.png"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        w, h = 32, 20
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        pts = [QPointF(2, h - 2), QPointF(w - 2, h - 2), QPointF(w / 2, 2)] if direction == "up" else \
              [QPointF(2, 2), QPointF(w - 2, 2), QPointF(w / 2, h - 2)]
        p.drawPolygon(QPolygonF(pts))
        p.end()
        img.save(str(path))
    return path.as_posix()


def stylesheet(t: Tokens) -> str:
    up, down = arrow_image("up", t.text_muted), arrow_image("down", t.text_muted)
    up_off, down_off = arrow_image("up", t.border), arrow_image("down", t.border)
    return f"""
    QWidget {{ font-family: "Segoe UI"; font-size: 10pt; }}
    QMainWindow, QStackedWidget > QWidget {{ background: {t.bg}; }}
    #Sidebar {{ background: {t.surface}; border: none; border-right: 1px solid {t.border}; padding: 8px 4px; outline: 0; }}
    #Sidebar::item {{ padding: 9px 10px; margin: 2px 4px; border-radius: 8px; color: {t.text_muted}; }}
    #Sidebar::item:hover {{ background: {t.surface_alt}; color: {t.text}; }}
    #Sidebar::item:selected {{ background: {t.accent}; color: {t.accent_text}; }}
    #AppTitle {{ font-size: 13pt; font-weight: 600; padding: 14px 16px 6px 16px; color: {t.text}; }}
    #PageTitle {{ font-size: 18pt; font-weight: 600; color: {t.text}; }}
    #PageSubtitle {{ color: {t.text_muted}; }}
    #Card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; }}
    #CardTitle {{ color: {t.text_muted}; font-size: 9pt; text-transform: uppercase; letter-spacing: 0.5px; }}
    #CardValue {{ font-size: 15pt; font-weight: 600; color: {t.text}; }}
    #CardDetail {{ color: {t.text_muted}; }}
    #Muted {{ color: {t.text_muted}; }}
    #Notice {{ background: {t.surface_alt}; border: 1px solid {t.border}; border-radius: 8px; padding: 10px; color: {t.text}; }}
    QPushButton {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px; padding: 7px 14px; color: {t.text}; }}
    QPushButton:hover {{ border-color: {t.accent}; }}
    QPushButton:disabled {{ color: {t.text_muted}; background: {t.surface_alt}; }}
    QPushButton#Primary {{ background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent}; font-weight: 600; }}
    QPushButton#Primary:disabled {{ background: {t.surface_alt}; color: {t.text_muted}; border-color: {t.border}; }}
    QPushButton#Danger {{ color: {t.bad}; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
        background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; padding: 5px 8px; color: {t.text}; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {t.accent}; }}
    QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
        width: 20px; border-left: 1px solid {t.border}; border-bottom: 1px solid {t.border};
        border-top-right-radius: 6px; background: {t.surface_alt}; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
        width: 20px; border-left: 1px solid {t.border}; border-bottom-right-radius: 6px; background: {t.surface_alt}; }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {t.border}; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up}"); width: 8px; height: 5px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down}"); width: 8px; height: 5px; }}
    QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off,
    QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:off {{ image: url("{up_off}"); }}
    QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off,
    QDoubleSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:off {{ image: url("{down_off}"); }}
    QTableWidget, QListWidget {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
        gridline-color: {t.border}; selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
    QHeaderView::section {{ background: {t.surface_alt}; color: {t.text_muted}; border: none;
        border-bottom: 1px solid {t.border}; padding: 6px; font-weight: 600; }}
    QGroupBox {{ border: 1px solid {t.border}; border-radius: 10px; margin-top: 14px; padding: 12px 10px 10px 10px;
        background: {t.surface}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {t.text_muted}; font-weight: 600; }}
    QProgressBar {{ border: 1px solid {t.border}; border-radius: 6px; background: {t.surface_alt}; text-align: center;
        color: {t.text}; height: 16px; }}
    QProgressBar::chunk {{ background: {t.accent}; border-radius: 5px; }}
    QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; color: {t.text_muted}; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea#PageScroll, QScrollArea#PageScroll > QWidget, QWidget#Page {{ background: {t.bg}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {t.border}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {t.accent}; width: 14px; margin: -6px 0; border-radius: 7px; }}
    QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 8px; background: {t.surface}; }}
    QTabBar::tab {{ padding: 7px 14px; color: {t.text_muted}; }}
    QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
    """
