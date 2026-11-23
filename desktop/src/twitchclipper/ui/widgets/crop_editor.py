"""9:16 crop editor: a still frame with a draggable full-height crop box."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import theme


class CropEditor(QWidget):
    """centerChanged(frac): horizontal centre of the 9:16 box, 0 = left edge, 1 = right edge."""

    centerChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.pixmap: QPixmap | None = None
        self.center = 0.5
        self._drag = False
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def set_frame(self, path: Path | str | None) -> None:
        self.pixmap = QPixmap(str(path)) if path else None
        if self.pixmap is not None and self.pixmap.isNull():
            self.pixmap = None
        self.update()

    def set_center(self, frac: float) -> None:
        self.center = min(1.0, max(0.0, frac))
        self.update()

    def _image_rect(self) -> QRectF:
        if not self.pixmap:
            return QRectF(self.rect())
        pw, ph = self.pixmap.width(), self.pixmap.height()
        scale = min(self.width() / pw, self.height() / ph)
        w, h = pw * scale, ph * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def crop_rect(self) -> QRectF:
        img = self._image_rect()
        cw = min(img.width(), img.height() * 9 / 16)
        x = img.left() + img.width() * self.center - cw / 2
        x = max(img.left(), min(img.right() - cw, x))
        return QRectF(x, img.top(), cw, img.height())

    def paintEvent(self, _event) -> None:
        t = theme.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        img = self._image_rect()
        if self.pixmap:
            p.drawPixmap(img.toRect(), self.pixmap)
        else:
            p.fillRect(img, QColor(t.surface_alt))
            p.setPen(QColor(t.text_muted))
            p.drawText(img, Qt.AlignmentFlag.AlignCenter, "Select a highlight to preview the crop")
        crop = self.crop_rect()
        shade = QColor(0, 0, 0, 140)
        p.fillRect(QRectF(img.left(), img.top(), crop.left() - img.left(), img.height()), shade)
        p.fillRect(QRectF(crop.right(), img.top(), img.right() - crop.right(), img.height()), shade)
        p.setPen(QPen(QColor(t.accent), 2))
        p.drawRect(crop)
        p.end()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        self._drag = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._move_to(e.position().x())

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag:
            self._move_to(e.position().x())

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        self._drag = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _move_to(self, x: float) -> None:
        img = self._image_rect()
        if img.width() <= 0:
            return
        self.set_center((x - img.left()) / img.width())
        self.centerChanged.emit(self.center)
