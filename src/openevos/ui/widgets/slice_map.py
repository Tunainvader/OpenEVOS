"""Z-stack slice map: a small side view of the stack, showing its extent, its direction and where it is now.

A vertical Z axis with higher Z at the top (the objective moves up into the sample). One tick per slice: planned
slices are faint, slices taken are solid, and the current slice (being taken, or being shown) is the accent bar with
its height. START and STOP are labelled at their heights, and an arrow beside the ticks runs from start to stop, so
the operator sees at a glance whether the stack goes up or down. A dashed line marks the live focus height when it is
within view. Clicking a slice that has been taken emits `slice_clicked(index)` (index in acquisition order).

The map only draws; the Z-stack page feeds it the plan, the slices taken and the current index.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from openevos.ui import theme

_CENTRE = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter


def _c(hex_color: str, alpha: int | None = None) -> QColor:
    c = QColor(hex_color or "#888888")
    if alpha is not None:
        c.setAlpha(alpha)
    return c


class SliceMap(QWidget):
    slice_clicked = Signal(int)

    TOP, BOTTOM = 34, 30  # room for the header and the footer
    AXIS_X = 58  # Z axis; height labels sit to its left
    TICK_W = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self.planned: list[float] = []  # slice heights, acquisition order (start first)
        self.stop_um: float | None = None  # requested stop (the last slice may end short of it)
        self.taken = 0  # slices taken so far (the first `taken` of `planned`)
        self.current: int | None = None  # highlighted slice
        self.focus_um: float | None = None  # live focus height
        self.setMinimumSize(170, 240)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setAccessibleName("Z-stack slice map")
        self.setMouseTracking(False)

    def sizeHint(self) -> QSize:
        return QSize(180, 360)

    # ------------------------------------------------------------------ state
    def set_plan(self, planned: list[float], stop_um: float | None = None, *, taken: int = 0,
                 current: int | None = None) -> None:
        self.planned = list(planned)
        self.stop_um = stop_um if stop_um is not None else (self.planned[-1] if self.planned else None)
        self.taken = max(0, min(taken, len(self.planned)))
        self.current = current if current is not None and 0 <= current < len(self.planned) else None
        self._changed()

    def set_taken(self, n: int, current: int | None = None) -> None:
        self.taken = max(0, min(n, len(self.planned)))
        self.current = current if current is not None and 0 <= current < len(self.planned) else None
        self._changed()

    def set_current(self, i: int | None) -> None:
        self.current = i if i is not None and 0 <= i < len(self.planned) else None
        self._changed()

    def set_focus(self, z: float | None) -> None:
        if z != self.focus_um:
            self.focus_um = z
            self.update()

    @property
    def direction(self) -> int:
        """+1 up (stop above start), -1 down, 0 when there is no plan."""
        if not self.planned:
            return 0
        end = self.stop_um if self.stop_um is not None else self.planned[-1]
        return -1 if end < self.planned[0] else 1

    def summary(self) -> str:
        if not self.planned:
            return "No stack planned"
        span = abs((self.stop_um if self.stop_um is not None else self.planned[-1]) - self.planned[0])
        way = "up" if self.direction > 0 else "down"
        if len(self.planned) == 1:
            way = "single plane"
        return f"{span:,.1f} µm · {len(self.planned)} slices · {way}"

    def _changed(self) -> None:
        tip = self.summary()
        if self.current is not None:
            tip += f"\nslice {self.current + 1}: z = {self.planned[self.current]:,.2f} µm"
        self.setToolTip(tip)
        self.update()

    # ------------------------------------------------------------------ geometry
    def _span(self) -> tuple[float, float]:
        vals = list(self.planned)
        if self.stop_um is not None:
            vals.append(self.stop_um)
        lo, hi = min(vals), max(vals)
        pad = max((hi - lo) * 0.08, 0.5)
        return lo - pad, hi + pad

    def _y(self, z: float) -> float:
        lo, hi = self._span()
        top, bottom = self.TOP, self.height() - self.BOTTOM
        return bottom - (z - lo) / (hi - lo) * (bottom - top)

    def index_at(self, y: float) -> int | None:
        """Nearest slice taken to a click at height `y` (None if nothing is taken yet)."""
        if not self.taken:
            return None
        return min(range(self.taken), key=lambda i: abs(self._y(self.planned[i]) - y))

    def mousePressEvent(self, e) -> None:
        i = self.index_at(e.position().y())
        if i is not None:
            self.slice_clicked.emit(i)
        super().mousePressEvent(e)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        text, muted, accent = _c(theme.TEXT), _c(theme.MUTED), _c(theme.ACCENT)
        ok = _c(theme.OK or theme.ACCENT)
        f = QFont(self.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() * 0.85))
        p.setFont(f)
        fm = p.fontMetrics()

        p.setPen(muted)
        p.drawText(QRectF(0, 4, w, 18), _CENTRE, "Z (µm) ↑ higher")
        if not self.planned:
            p.drawText(QRectF(0, 0, w, h), _CENTRE | Qt.TextFlag.TextWordWrap, "Set start, stop\nand step")
            p.end()
            return

        ax = self.AXIS_X
        top, bottom = self.TOP, h - self.BOTTOM
        p.setPen(QPen(_c(theme.BORDER_STRONG or theme.MUTED), 1.5))
        p.drawLine(QPointF(ax, top - 6), QPointF(ax, bottom + 6))

        # slices
        x0, x1 = ax + 4, ax + 4 + self.TICK_W
        for i, z in enumerate(self.planned):
            y = self._y(z)
            if i == self.current:
                continue
            p.setPen(QPen(text if i < self.taken else _c(theme.MUTED, 110), 1.6 if i < self.taken else 1.0))
            p.drawLine(QPointF(x0, y), QPointF(x1, y))

        # start / stop labels on the left of the axis
        z_start = self.planned[0]
        z_stop = self.stop_um if self.stop_um is not None else self.planned[-1]
        self._tag(p, fm, ax, self._y(z_start), "START", z_start, ok)
        if len(self.planned) > 1:
            self._tag(p, fm, ax, self._y(z_stop), "STOP", z_stop, _c(theme.WARN or theme.ACCENT))

        # direction arrow from start to stop
        if len(self.planned) > 1:
            xa = x1 + 14
            ya, yb = self._y(z_start), self._y(z_stop)
            p.setPen(QPen(accent, 2))
            p.drawLine(QPointF(xa, ya), QPointF(xa, yb))
            d = -1 if yb < ya else 1
            head = QPolygonF([QPointF(xa, yb), QPointF(xa - 5, yb - d * 9), QPointF(xa + 5, yb - d * 9)])
            p.setBrush(accent)
            p.drawPolygon(head)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.save()
            p.translate(xa + 12, (ya + yb) / 2)
            p.rotate(-90)
            p.setPen(accent)
            way = "UP" if self.direction > 0 else "DOWN"
            p.drawText(QRectF(-40, -8, 80, 16), Qt.AlignmentFlag.AlignCenter, way)
            p.restore()

        # live focus
        lo, hi = self._span()
        if self.focus_um is not None and lo <= self.focus_um <= hi:
            y = self._y(self.focus_um)
            pen = QPen(_c(theme.TEAL or theme.ACCENT, 200), 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(ax - 6, y), QPointF(w - 4, y))

        # current slice on top
        if self.current is not None:
            y = self._y(self.planned[self.current])
            p.setPen(QPen(accent, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
            path = QPainterPath()
            path.moveTo(x0 - 2, y)
            path.lineTo(x0 - 9, y - 5)
            path.lineTo(x0 - 9, y + 5)
            path.closeSubpath()
            p.fillPath(path, accent)

        # footer
        p.setPen(muted)
        foot = (f"slice {self.current + 1}/{len(self.planned)} · {self.planned[self.current]:,.1f}"
                if self.current is not None else f"{self.taken}/{len(self.planned)} taken")
        p.drawText(QRectF(0, h - 22, w, 18), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, foot)
        p.end()

    def _tag(self, p: QPainter, fm, ax: float, y: float, name: str, z: float, color: QColor) -> None:
        p.setPen(color)
        right = Qt.AlignmentFlag.AlignRight
        p.drawText(QRectF(0, y - fm.height() - 1, ax - 6, fm.height()), right | Qt.AlignmentFlag.AlignBottom, name)
        p.setPen(_c(theme.TEXT_2 or theme.TEXT))
        p.drawText(QRectF(0, y, ax - 6, fm.height()), right | Qt.AlignmentFlag.AlignTop, f"{z:,.1f}")
        p.setPen(QPen(color, 1.5))
        p.drawLine(QPointF(ax - 5, y), QPointF(ax + 2, y))
