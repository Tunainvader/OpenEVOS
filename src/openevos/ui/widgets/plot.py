"""Tiny plot for intensity profiles and ROI histograms (lines/bars, axis labels, tabular numbers)."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from openevos.ui import theme


class MiniPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.series: list[tuple[str, np.ndarray, str]] = []  # name, y, colour
        self.kind = "line"
        self.xlabel = ""
        self.title = ""
        self.setMinimumHeight(130)

    def sizeHint(self) -> QSize:
        return QSize(320, 150)

    def set_data(self, series, kind: str = "line", xlabel: str = "", title: str = "") -> None:
        self.series, self.kind, self.xlabel, self.title = series, kind, xlabel, title
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(34, 18, -8, -20)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QColor(theme.INPUT))
        p.drawRect(r)
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(r.left(), 2, r.width(), 14), Qt.AlignLeft | Qt.AlignVCenter, self.title or "Select a line or ROI")  # noqa: E501
        p.drawText(QRectF(r.left(), r.bottom() + 3, r.width(), 14), Qt.AlignRight | Qt.AlignVCenter, self.xlabel)
        if not self.series:
            return
        ymax = max(float(np.max(y)) for _n, y, _c in self.series if len(y)) or 1.0
        p.drawText(QRectF(0, r.top() - 6, 32, 12), Qt.AlignRight | Qt.AlignVCenter, f"{ymax:.0f}")
        p.drawText(QRectF(0, r.bottom() - 6, 32, 12), Qt.AlignRight | Qt.AlignVCenter, "0")
        for i, (name, y, color) in enumerate(self.series):
            if not len(y):
                continue
            col = QColor(color)
            n = len(y)
            if self.kind == "bar":
                bw = r.width() / n
                col.setAlpha(170)
                for k, v in enumerate(y):
                    h = r.height() * v / ymax
                    p.fillRect(QRectF(r.left() + k * bw, r.bottom() - h, max(1.0, bw - 1), h), col)
            else:
                path = QPainterPath()
                for k, v in enumerate(y):
                    pt = QPointF(r.left() + r.width() * k / max(1, n - 1), r.bottom() - r.height() * v / ymax)
                    path.moveTo(pt) if k == 0 else path.lineTo(pt)
                pen = QPen(col, 1.6)
                if i % 2:  # second series dashed: never colour alone
                    pen.setStyle(Qt.DashLine)
                p.strokePath(path, pen)
            p.setPen(col)
            p.drawText(QRectF(r.right() - 120, r.top() + 2 + 12 * i, 116, 12), Qt.AlignRight | Qt.AlignVCenter, name)
