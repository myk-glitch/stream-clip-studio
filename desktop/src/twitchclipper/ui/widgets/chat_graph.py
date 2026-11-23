"""Chat activity graph (pyqtgraph): score over time, thresholds, highlight windows."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .. import theme


class ChatGraph(QWidget):
    clicked = Signal(float)   # time in seconds

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        t = theme.tokens()
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background=t.plot_bg)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("left", "Chat score", color=t.text_muted)
        self.plot.setLabel("bottom", "Time (s)", color=t.text_muted)
        self.plot.getAxis("left").setTextPen(t.text_muted)
        self.plot.getAxis("bottom").setTextPen(t.text_muted)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setXRange(0, 60, padding=0)
        self.plot.setYRange(0, 8, padding=0)
        self.curve = self.plot.plot(pen=pg.mkPen(t.plot_line, width=1.5))
        self.high_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(t.bad, width=1, style=pg.QtCore.Qt.PenStyle.DashLine),
                                         label="high", labelOpts={"color": t.bad, "position": 0.02})
        self.low_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(t.warn, width=1, style=pg.QtCore.Qt.PenStyle.DotLine),
                                        label="low", labelOpts={"color": t.warn, "position": 0.02})
        self.plot.addItem(self.high_line)
        self.plot.addItem(self.low_line)
        self.high_line.hide()   # shown once there is data
        self.low_line.hide()
        self._ymax = 8.0
        self._regions: list[pg.LinearRegionItem] = []
        self._labels: list[pg.TextItem] = []
        self.cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen(t.text_muted, width=1))
        self.cursor.hide()
        self.plot.addItem(self.cursor)
        self.plot.scene().sigMouseClicked.connect(self._on_click)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

    def set_data(self, times: np.ndarray, scores: np.ndarray, high: float, low: float) -> None:
        self.curve.setData(times, scores)
        self.high_line.setValue(high)
        self.low_line.setValue(low)
        self.high_line.show()
        self.low_line.show()
        if len(times):
            peak = float(np.max(scores)) if len(scores) else 0.0
            self._ymax = max(peak * 1.18, high * 1.3)   # headroom for the "#n" labels
            self.plot.setXRange(float(times[0]), float(times[-1]), padding=0.01)
            self.plot.setYRange(0, self._ymax, padding=0)

    def set_highlights(self, windows: list[tuple[int, float, float]]) -> None:
        """windows: (rank, start, end)."""
        for item in self._regions + self._labels:
            self.plot.removeItem(item)
        self._regions, self._labels = [], []
        t = theme.tokens()
        accent = pg.mkColor(t.accent)
        accent.setAlpha(45)
        for rank, start, end in windows:
            region = pg.LinearRegionItem((start, end), movable=False, brush=accent, pen=pg.mkPen(None))
            region.setZValue(-10)
            label = pg.TextItem(f"#{rank}", color=t.accent, anchor=(0.5, 0.0))
            label.setPos((start + end) / 2, self._ymax * 0.98)
            self.plot.addItem(region)
            self.plot.addItem(label)
            self._regions.append(region)
            self._labels.append(label)

    def show_cursor(self, t: float) -> None:
        self.cursor.setValue(t)
        self.cursor.show()

    def clear(self) -> None:
        self.curve.setData([], [])
        self.set_highlights([])
        self.cursor.hide()
        self.high_line.hide()
        self.low_line.hide()
        self._ymax = 8.0
        self.plot.setXRange(0, 60, padding=0)
        self.plot.setYRange(0, 8, padding=0)

    def _on_click(self, ev) -> None:
        vb = self.plot.getViewBox()
        if self.plot.sceneBoundingRect().contains(ev.scenePos()):
            self.clicked.emit(float(vb.mapSceneToView(ev.scenePos()).x()))
