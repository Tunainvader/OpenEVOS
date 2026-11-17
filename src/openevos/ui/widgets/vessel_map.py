"""Virtual vessel (manual p.8): regions drawn to scale, draggable crosshair, click a well to centre
it, current field of view, reachable-stage outline, optional tile/beacon overlays and selection."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from openevos.instrument.vessels import RegionShape, Vessel
from openevos.ui import theme


class VesselMap(QWidget):
    move_requested = Signal(float, float)  # vessel mm
    region_clicked = Signal(str)

    def __init__(self, parent=None, *, interactive: bool = True, selectable: bool = False):
        super().__init__(parent)
        self.vessel: Vessel | None = None
        self.position_mm: tuple[float, float] | None = None
        self.fov_mm: tuple[float, float] | None = None
        self.reachable_mm: tuple[float, float, float, float] | None = None
        self.tiles_mm: list[tuple[float, float]] = []
        self.beacons_mm: list[tuple[float, float, str]] = []
        self.selected: set[str] = set()
        self.interactive, self.selectable = interactive, selectable
        self._drag: QPointF | None = None
        self._press: QPointF | None = None
        self.setMinimumSize(220, 150)
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(260, 175)

    def heightForWidth(self, w: int) -> int:
        return int(w * 0.67)

    def set_vessel(self, v: Vessel) -> None:
        self.vessel = v
        self.selected.clear()
        self.update()

    # ------------------------------------------------------------------ geometry
    def _xform(self) -> tuple[float, float, float]:
        v = self.vessel
        pad = 8
        s = min((self.width() - 2 * pad) / v.width_mm, (self.height() - 2 * pad) / v.height_mm)
        ox = (self.width() - v.width_mm * s) / 2
        oy = (self.height() - v.height_mm * s) / 2
        return s, ox, oy

    def to_widget(self, x: float, y: float) -> QPointF:
        s, ox, oy = self._xform()
        return QPointF(ox + x * s, oy + y * s)

    def to_vessel(self, p: QPointF) -> tuple[float, float]:
        s, ox, oy = self._xform()
        return ((p.x() - ox) / s, (p.y() - oy) / s)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.vessel:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Select a vessel")
            return
        v = self.vessel
        s, ox, oy = self._xform()
        # the plate sits in an inset well, like the stylesheet's fields (soft UI; Impeccable audit 2026-11-18)
        theme.paint_surface(p, QRectF(ox, oy, v.width_mm * s, v.height_mm * s), 10, inset=True, control=False)
        if self.reachable_mm:
            x0, y0, x1, y1 = self.reachable_mm
            p.setPen(QPen(QColor(theme.WARN), 1, Qt.DotLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(self.to_widget(x0, y0), self.to_widget(x1, y1)))
        cur = v.region_at(*self.position_mm) if self.position_mm else None
        for r in v.regions:
            rect = QRectF(self.to_widget(r.cx - r.w / 2, r.cy - r.h / 2), self.to_widget(r.cx + r.w / 2, r.cy + r.h / 2))
            sel = r.region_id in self.selected
            active = cur is not None and cur.region_id == r.region_id
            fill = QColor(theme.ACCENT_DIM) if sel else QColor(theme.PANEL_2)
            if active:
                fill = QColor(theme.ACCENT)
                fill.setAlpha(150)
            p.setBrush(fill)
            p.setPen(QPen(QColor(theme.ACCENT) if (sel or active) else QColor(theme.BORDER_STRONG), 0.8))
            if r.shape is RegionShape.CIRCLE:
                p.drawEllipse(rect)
            else:
                p.drawRoundedRect(rect, 2, 2)
        if v.is_plate and s * (v.regions[1].cx - v.regions[0].cx if len(v.regions) > 1 else 10) > 13:
            p.setPen(QColor(theme.MUTED))
            p.setFont(QFont(self.font().family(), 7))
            for c in range(v.cols):
                r = v.regions[c]
                q = self.to_widget(r.cx, r.cy - r.h / 2)
                p.drawText(QRectF(q.x() - 10, q.y() - 13, 20, 12), Qt.AlignCenter, str(c + 1))
            for row in range(v.rows):
                r = v.regions[row * v.cols]
                q = self.to_widget(r.cx - r.w / 2, r.cy)
                p.drawText(QRectF(q.x() - 14, q.y() - 6, 12, 12), Qt.AlignCenter, chr(65 + row) if row < 26 else "")
        tc = QColor(theme.TEAL)
        p.setPen(QPen(tc, 0.8))
        tc.setAlpha(60)
        p.setBrush(tc)
        if self.tiles_mm and self.fov_mm:
            fw, fh = self.fov_mm
            for x, y in self.tiles_mm[:5000]:
                p.drawRect(QRectF(self.to_widget(x - fw / 2, y - fh / 2), self.to_widget(x + fw / 2, y + fh / 2)))
        for x, y, label in self.beacons_mm:
            q = self.to_widget(x, y)
            p.setPen(QPen(QColor(theme.WARN), 1.5))
            p.setBrush(QColor(245, 185, 66, 90))
            p.drawEllipse(q, 4, 4)
            if len(self.beacons_mm) <= 24:
                p.setFont(QFont(self.font().family(), 7))
                p.drawText(q + QPointF(5, -4), label)
        if self.position_mm:
            q = self.to_widget(*self.position_mm)
            if self.fov_mm:
                fw, fh = self.fov_mm
                p.setPen(QPen(QColor(theme.TEXT), 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(self.to_widget(self.position_mm[0] - fw / 2, self.position_mm[1] - fh / 2),
                                  self.to_widget(self.position_mm[0] + fw / 2, self.position_mm[1] + fh / 2)))
            p.setPen(QPen(QColor(theme.FAULT), 1.4))
            p.drawLine(QPointF(q.x() - 9, q.y()), QPointF(q.x() + 9, q.y()))
            p.drawLine(QPointF(q.x(), q.y() - 9), QPointF(q.x(), q.y() + 9))
            p.drawEllipse(q, 4, 4)
        if self._drag is not None:
            p.setPen(QPen(QColor(theme.ACCENT), 1, Qt.DashLine))
            p.drawLine(QPointF(self._drag.x() - 12, self._drag.y()), QPointF(self._drag.x() + 12, self._drag.y()))
            p.drawLine(QPointF(self._drag.x(), self._drag.y() - 12), QPointF(self._drag.x(), self._drag.y() + 12))

    # ------------------------------------------------------------------ interaction
    def mousePressEvent(self, e):
        if not self.vessel or not self.interactive:
            return
        x, y = self.to_vessel(e.position())
        r = self.vessel.region_at(x, y)
        if self.selectable:
            if r is not None:
                self.selected ^= {r.region_id}
                self.region_clicked.emit(r.region_id)
                self.update()
            return
        self._drag = self._press = e.position()
        self.update()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self._drag = e.position()
            self.update()
        elif self.vessel:
            x, y = self.to_vessel(e.position())
            r = self.vessel.region_at(x, y)
            self.setToolTip(f"{r.region_id}  ({x:.2f}, {y:.2f}) mm" if r else f"({x:.2f}, {y:.2f}) mm")

    def mouseReleaseEvent(self, e):
        if self._drag is None or self._press is None or not self.vessel:
            return
        moved = (e.position() - self._press).manhattanLength()
        x, y = self.to_vessel(e.position())
        self._drag = self._press = None
        r = self.vessel.region_at(x, y)
        if r is not None and moved < 5:  # click on a well => centre of the well (manual p.8)
            self.region_clicked.emit(r.region_id)
            self.move_requested.emit(r.cx, r.cy)
        else:  # drag the crosshair => drop point
            self.move_requested.emit(min(max(x, 0), self.vessel.width_mm), min(max(y, 0), self.vessel.height_mm))
        self.update()
