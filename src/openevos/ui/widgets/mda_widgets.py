"""Small painted widgets for the Multi-D page (ADR-0065; kit mockup 08 "Multi-D plan / acquire / review").

* `DimChip`: a dimension in the page header: a lettered badge in the dimension's colour, its name and count; off
  dimensions read "off" (words, not colour alone).
* `PositionMap`: the vessel's wells or regions as a grid; positions in the plan are filled, the one being acquired is
  ringed. A click asks the page to add or remove a position at that well's centre.
* `ZPlanRuler`: the planned planes along a horizontal Z axis, bottom to top, with the focus reference marked.
* `AxisBar`: one dimension's progress, its letter, name, a bar in its colour and "12 / 49".
* `KeyValues`: label / value rows (the summary and status cards).
* `Checklist`: pre-flight lines with a mark and its word (passed, check, blocked, not used).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget

from openevos.ui import theme


def dim_color(key: str) -> str:
    """Z blue, C green, T violet, P amber (kit mockup 08); the same everywhere on the page."""
    return {"z": theme.ACCENT, "c": theme.OK, "t": theme.VIOLET or theme.ACCENT, "p": theme.AMBER or theme.WARN}[key]


# ====================================================================================== header chip
class DimChip(QWidget):
    def __init__(self, key: str, name: str, parent=None):
        super().__init__(parent)
        self.key, self.name = key, name
        self.count: int | None = None
        self.on = True
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # shrinks to the count only

    def set_state(self, on: bool, count: int | None) -> None:
        if (on, count) == (self.on, self.count):
            return
        self.on, self.count = on, count
        self.setAccessibleName(self._text())
        self.setToolTip(self._text())
        self.updateGeometry()
        self.update()

    def _text(self) -> str:
        if not self.on:
            return f"{self.name} off"
        return f"{self.name} ({self.count})" if self.count is not None else self.name

    def _short(self) -> str:
        return "off" if not self.on else str(self.count) if self.count is not None else ""

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._text()) + 46, max(28, fm.height() + 12))

    def minimumSizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._short()) + 46, max(28, fm.height() + 12))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        col = QColor(dim_color(self.key))
        edge = QColor(col if self.on else theme.BORDER_STRONG)
        edge.setAlpha(200 if self.on else 255)
        fill = QColor(col)
        fill.setAlpha(28 if self.on else 0)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRoundedRect(r, 6, 6)
        b = QRectF(r.left() + 6, r.center().y() - 9, 18, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col if self.on else QColor(theme.BORDER_STRONG))
        p.drawRoundedRect(b, 4, 4)
        f = QFont(self.font())
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(QColor(theme.on(dim_color(self.key))) if self.on else QColor(theme.TEXT_2))
        p.drawText(b, Qt.AlignmentFlag.AlignCenter, self.key.upper())
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT if self.on else theme.MUTED))
        room = QRectF(b.right() + 8, r.top(), r.right() - b.right() - 10, r.height())
        text = self._text() if p.fontMetrics().horizontalAdvance(self._text()) <= room.width() else self._short()
        p.drawText(room, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        p.end()


# ====================================================================================== position map
class PositionMap(QWidget):
    """The vessel's regions drawn to scale; filled = in the plan, ringed = being acquired now."""

    clicked = Signal(str)  # region id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.regions: list = []
        self.rows = self.cols = 0
        self.width_mm = self.height_mm = 1.0
        self.selected: set[str] = set()
        self.current: str | None = None
        self.done: set[str] = set()
        self.interactive = True
        self._hover: str | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName("Position map")

    def sizeHint(self) -> QSize:
        return QSize(300, 190)

    def set_vessel(self, vessel) -> None:
        self.regions = list(vessel.regions)
        self.rows, self.cols = vessel.rows, vessel.cols
        self.width_mm, self.height_mm = max(1.0, vessel.width_mm), max(1.0, vessel.height_mm)
        self.update()

    def set_marks(self, selected: set[str], current: str | None = None, done: set[str] | None = None) -> None:
        self.selected, self.current, self.done = set(selected), current, set(done or ())
        n = len(self.selected)
        self.setAccessibleDescription(f"{n} position{'s' if n != 1 else ''} selected"
                                      + (f", acquiring {current}" if current else ""))
        self.update()

    # ---------------------------------------------------------------- geometry
    def _labels(self) -> tuple[float, float]:
        """Room on the left and top for row letters and column numbers (plates only)."""
        return (18.0, 16.0) if self.rows and self.cols else (0.0, 0.0)

    def _frame(self) -> tuple[float, float, float, float, float]:
        """(x0, y0, scale, min x mm, min y mm) of the regions' bounding box fitted into the widget."""
        if not self.regions:
            return 0, 0, 1, 0, 0
        lx, ly = self._labels()
        xs0 = min(r.bounds[0] for r in self.regions)
        ys0 = min(r.bounds[1] for r in self.regions)
        xs1 = max(r.bounds[2] for r in self.regions)
        ys1 = max(r.bounds[3] for r in self.regions)
        w, h = self.width() - lx - 6, self.height() - ly - 6
        s = min(w / max(1e-6, xs1 - xs0), h / max(1e-6, ys1 - ys0))
        x0 = lx + 3 + (w - (xs1 - xs0) * s) / 2
        y0 = ly + 3 + (h - (ys1 - ys0) * s) / 2
        return x0, y0, s, xs0, ys0

    def _rect(self, reg) -> QRectF:
        x0, y0, s, mx, my = self._frame()
        b = reg.bounds
        r = QRectF(x0 + (b[0] - mx) * s, y0 + (b[1] - my) * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s)
        shrink = min(r.width(), r.height()) * 0.08
        return r.adjusted(shrink, shrink, -shrink, -shrink)

    def region_at(self, pos: QPointF) -> str | None:
        for reg in self.regions:
            if self._rect(reg).contains(pos):
                return reg.region_id
        return None

    # ---------------------------------------------------------------- events
    def mouseMoveEvent(self, e):
        h = self.region_at(e.position()) if self.interactive else None
        if h != self._hover:
            self._hover = h
            self.setToolTip(f"{h}: click to {'remove' if h in self.selected else 'add'}" if h else "")
            self.update()

    def leaveEvent(self, e):
        self._hover = None
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if self.interactive and e.button() == Qt.MouseButton.LeftButton:
            rid = self.region_at(e.position())
            if rid:
                self.clicked.emit(rid)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.regions:
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No vessel")
            return
        cap = QFont(self.font())
        cap.setPointSizeF(max(7.0, cap.pointSizeF() - 1))
        p.setFont(cap)
        lx, ly = self._labels()
        # row letters and column numbers, once per row / column
        if lx:
            seen_r: set[str] = set()
            seen_c: set[str] = set()
            for reg in self.regions:
                rid = reg.region_id
                row, col = rid[:1], rid[1:]
                r = self._rect(reg)
                p.setPen(QColor(theme.TEXT_2))
                if row not in seen_r:
                    seen_r.add(row)
                    p.drawText(QRectF(0, r.top(), lx, r.height()), Qt.AlignmentFlag.AlignCenter, row)
                if col not in seen_c:
                    seen_c.add(col)
                    p.drawText(QRectF(r.left() - 4, 0, r.width() + 8, ly), Qt.AlignmentFlag.AlignCenter, col)
        acc = QColor(dim_color("p") if not self.interactive else theme.ACCENT)
        for reg in self.regions:
            r = self._rect(reg)
            rid = reg.region_id
            sel = rid in self.selected
            fill = QColor(theme.ACCENT if sel else theme.INPUT)
            if rid in self.done:
                fill = QColor(theme.OK)
            if not sel and rid not in self.done:
                fill = QColor(theme.INPUT)
            edge = QColor(theme.ACCENT if sel else theme.BORDER_STRONG)
            if rid == self._hover:
                edge = QColor(theme.ACCENT)
            p.setPen(QPen(edge, 1.2))
            p.setBrush(fill)
            if reg.shape.value == "circle":
                p.drawEllipse(r)
            else:
                p.drawRoundedRect(r, 3, 3)
            if rid == self.current:
                ring = r.adjusted(-3, -3, 3, 3)
                p.setPen(QPen(acc, 2.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(ring) if reg.shape.value == "circle" else p.drawRoundedRect(ring, 4, 4)
            if not lx and r.width() > 30:  # slides and dishes: their names
                p.setPen(QColor(theme.on(theme.ACCENT)) if sel else QColor(theme.TEXT_2))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, rid)
        p.end()


# ====================================================================================== Z ruler
class ZPlanRuler(QWidget):
    """Planned planes on a horizontal axis (bottom on the left, top on the right), the focus reference marked."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.planes: list[float] = []
        self.anchor: float | None = None
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Z planes")

    def sizeHint(self) -> QSize:
        return QSize(280, 58)

    def set_planes(self, planes: list[float], anchor: float | None) -> None:
        self.planes, self.anchor = list(planes), anchor
        if planes:
            self.setToolTip(f"{len(planes)} planes from {planes[0]:,.2f} to {planes[-1]:,.2f} µm"
                            + (f"; focus reference {anchor:,.2f} µm" if anchor is not None else ""))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.planes:
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "One plane at the focus reference")
            return
        cap = QFont(self.font())
        cap.setPointSizeF(max(7.0, cap.pointSizeF() - 1))
        p.setFont(cap)
        fm = p.fontMetrics()
        lo, hi = self.planes[0], self.planes[-1]
        if self.anchor is not None:
            lo, hi = min(lo, self.anchor), max(hi, self.anchor)
        span = max(hi - lo, 1e-6)
        x0, x1 = 8.0 + fm.horizontalAdvance("0,000.0") / 2, self.width() - 8.0 - fm.horizontalAdvance("0,000.0") / 2
        y = 22.0

        def X(z: float) -> float:
            return x0 + (z - lo) / span * (x1 - x0)

        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1.4))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        col = QColor(dim_color("z"))
        step = max(1, len(self.planes) // 80)
        for i, z in enumerate(self.planes):
            if i % step and i != len(self.planes) - 1:
                continue
            p.setPen(QPen(col, 1.4))
            p.drawLine(QPointF(X(z), y - 7), QPointF(X(z), y + 7))
        for z in (self.planes[0], self.planes[-1]):
            p.setBrush(col)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(X(z), y), 4, 4)
        if self.anchor is not None:
            xa = X(self.anchor)
            p.setPen(QPen(QColor(theme.TEXT), 1.4))
            p.setBrush(QColor(theme.PANEL))
            d = 5.0
            pts = [QPointF(xa, y - d), QPointF(xa + d, y), QPointF(xa, y + d), QPointF(xa - d, y)]
            p.drawPolygon(pts)
        p.setPen(QColor(theme.TEXT_2))
        n_lab = 5 if len(self.planes) > 4 else len(self.planes)
        for k in range(n_lab):
            z = self.planes[0] + (self.planes[-1] - self.planes[0]) * (k / max(1, n_lab - 1))
            t = f"{z:,.1f}"
            w = fm.horizontalAdvance(t)
            p.drawText(QRectF(X(z) - w / 2 - 2, y + 10, w + 4, fm.height()), Qt.AlignmentFlag.AlignCenter, t)
        lab = f"{len(self.planes)} planes · µm"
        p.drawText(QRectF(0, 0, self.width() - 4, 14), Qt.AlignmentFlag.AlignRight, lab)
        p.end()


# ====================================================================================== axis progress
class AxisBar(QWidget):
    def __init__(self, key: str, name: str, parent=None):
        super().__init__(parent)
        self.key, self.name = key, name
        self.done, self.total = 0, 0
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(260, 26)

    def set_value(self, done: int, total: int) -> None:
        self.done, self.total = done, total
        text = f"{self.name}: {done} of {total}"
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = p.fontMetrics()
        h = self.height()
        name = f"{self.key.upper()} ({self.total})"
        nw = fm.horizontalAdvance("Z (0000)") + 6
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(0, 0, nw, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        val = f"{self.done} / {self.total}"
        vw = fm.horizontalAdvance("0000 / 0000") + 4
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(self.width() - vw, 0, vw, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   val)
        r = QRectF(nw, h / 2 - 4, max(10.0, self.width() - nw - vw - 10), 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.INPUT))
        p.drawRoundedRect(r, 4, 4)
        if self.total > 0 and self.done > 0:
            f = QRectF(r.left(), r.top(), r.width() * min(1.0, self.done / self.total), r.height())
            p.setBrush(QColor(dim_color(self.key)))
            p.drawRoundedRect(f, 4, 4)
        p.end()


# ====================================================================================== label / value rows
class KeyValues(QWidget):
    def __init__(self, keys: list[tuple[str, str]], parent=None, *, mono_values: bool = False):
        super().__init__(parent)
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(12)
        lay.setVerticalSpacing(5)
        self.values: dict[str, QLabel] = {}
        self._lay = lay
        for i, (key, text) in enumerate(keys):
            self.add_row(key, text, mono_values=mono_values, row=i)
        lay.setColumnStretch(1, 1)  # values take the room left and elide by clipping, never widen the card

    def add_row(self, key: str, text: str, *, mono_values: bool = False, row: int | None = None,
                separator: bool = False) -> None:
        lay = self._lay
        r = lay.rowCount() if row is None else row
        k = QLabel(text)
        k.setObjectName("caption")
        v = QLabel("–")
        v.setObjectName("mono" if mono_values else "")
        v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setMinimumWidth(1)
        v.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if separator:
            k.setStyleSheet(f"padding-top: 6px; border-top: 1px solid {theme.BORDER};")
            v.setStyleSheet(f"padding-top: 6px; border-top: 1px solid {theme.BORDER};")
        lay.addWidget(k, r, 0)
        lay.addWidget(v, r, 1)
        self.values[key] = v

    def set(self, key: str, value: str, tip: str = "") -> None:
        v = self.values[key]
        v.setText(value)
        v.setToolTip(tip)


# ====================================================================================== checklist
MARKS = {"ok": ("✓", "passed"), "warn": ("!", "check"), "fail": ("✕", "blocked"), "skip": ("–", "not used")}


def checklist_html(items: list[tuple[str, str, str]]) -> str:
    """`items` = [(name, state, detail)]: a mark in the state's colour and the name; the detail in grey."""
    colour = {"ok": theme.OK, "warn": theme.WARN, "fail": theme.FAULT, "skip": theme.MUTED}
    rows = []
    for name, state, detail in items:
        mark, word = MARKS.get(state, ("·", ""))
        c = colour.get(state, theme.MUTED)
        rows.append(f"<tr><td style='color:{c}; font-weight:700; padding-right:8px' title='{word}'>{mark}</td>"
                    f"<td style='padding-bottom:3px'><b>{name}</b> <span style='color:{theme.TEXT_2}'>{detail}"
                    f"</span></td></tr>")
    return "<table cellspacing=0 cellpadding=0>" + "".join(rows) + "</table>"
