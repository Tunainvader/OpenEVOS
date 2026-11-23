"""Z focus panel for the Capture page (design handoff "OpenEVOS Z Focus control", 2026-11-18).

Header (title, one status line, state chip) · readback · moving tape scale beside a domed four-dot focus pad ·
Coarse/Fine with a step list · Find sample / Auto fine / Snap to last.

The panel never talks to a drive. It emits `step_requested(delta_um)`; the Capture page turns that into a target and
sends it through the existing `ctl.move_focus` path (router, safety checks, readback-verified completion; ADR-0053).
The Z it shows is the device readback the page passes in, never a requested target. The pad slides and tilts only
while pressed or while the focus drive reports motion (`set_state(moving=...)`); hover is a 2 px hint.

Zero is home (the lowest point; the drive never goes below it). Objectives of different heights change the Z at which
focus is found, not zero.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openevos.ui import theme
from openevos.ui import tokens as tk
from openevos.ui.icons import icon
from openevos.ui.widgets.segmented import SegmentedControl

FINE_STEPS = (0.5, 1.0, 2.0, 5.0, 10.0)
COARSE_STEPS = (10.0, 25.0, 50.0, 100.0, 500.0)
UM_PER_COUNT = 0.122  # 5000 counts per 0.61 mm (ADR-0052)

#: panel states (handoff table): chip token key, chip text, chip icon
STATES = {
    "ready": ("ok", "READY", "check"),
    "moving": ("busy", "MOVING", "spinner"),
    "simulated": ("simulated", "SIMULATED", "dashed"),
    "locked": ("locked", "LOCKED", "lock"),
    "fault": ("fault", "FAULT", "triangle"),
    "objective": ("busy", "OBJECTIVE", "down"),
}
INERT = {"locked", "fault", "objective"}


def fmt_um(v: float) -> str:
    return f"{v:,.2f}"


def _light() -> bool:
    return theme.current().name == "light"


def _soft_shadow(p: QPainter, rect: QRectF, radius: float, dy: float, blur: float, color: QColor,
                 steps: int = 6) -> None:
    """Approximate a CSS box-shadow under a rounded rect: the rect offset by dy, then `steps` outward outlines whose
    alpha falls to 0 at `blur`. Plain outline strokes only, so it stays cheap at the telemetry repaint rate."""
    r = rect.translated(0, dy)
    p.save()
    p.setBrush(Qt.NoBrush)
    for k in range(steps):
        grow = blur * (k + 0.5) / steps
        c = QColor(color)
        c.setAlphaF(color.alphaF() * (1 - k / steps) * 2 / steps)
        p.setPen(QPen(c, blur / steps + 0.6))
        p.drawRoundedRect(r.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)
    p.setPen(Qt.NoPen)
    c = QColor(color)
    c.setAlphaF(color.alphaF() * 0.6)
    p.setBrush(c)
    p.drawRoundedRect(r, radius, radius)
    p.restore()


def _inset(p: QPainter, rect: QRectF, radius: float, depth: float, color: QColor, steps: int = 6) -> None:
    """Inset shadow inside a rounded rect: inward outlines, darkest at the edge."""
    p.save()
    p.setBrush(Qt.NoBrush)
    for k in range(steps):
        d = depth * (k + 0.5) / steps
        c = QColor(color)
        c.setAlphaF(color.alphaF() * (1 - k / steps) ** 1.5 * 2 / steps)
        p.setPen(QPen(c, depth / steps + 0.6))
        p.drawRoundedRect(rect.adjusted(d, d, -d, -d), max(0.0, radius - d), max(0.0, radius - d))
    p.restore()


# ====================================================================== chip
class StateChip(QWidget):
    """Pill with text + colour + shape (colour never stands alone)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "ready"
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_state(self, state: str) -> None:
        self.state = state
        self.setAccessibleName(f"Focus state: {STATES[state][1].lower()}")
        self.updateGeometry()
        self.update()

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setWeight(QFont.DemiBold)
        f.setLetterSpacing(QFont.PercentageSpacing, 104)
        return f

    def sizeHint(self) -> QSize:
        from PySide6.QtGui import QFontMetrics

        w = QFontMetrics(self._font()).horizontalAdvance(STATES[self.state][1])
        return QSize(w + 20 + 14, 24)

    def paintEvent(self, _e):
        key, text, shape = STATES[self.state]
        col = QColor(tk.state_color(theme.current(), key))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(col, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        ic = QRectF(9, r.center().y() - 5, 10, 10)
        pen = QPen(col, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        if shape == "check":
            path = QPainterPath(QPointF(ic.left() + 1, ic.center().y()))
            path.lineTo(ic.left() + 4, ic.bottom() - 1.5)
            path.lineTo(ic.right() - 0.5, ic.top() + 1.5)
            p.drawPath(path)
        elif shape == "spinner":
            p.drawArc(ic.adjusted(1, 1, -1, -1), 90 * 16, 270 * 16)
        elif shape == "dashed":
            pen.setDashPattern([1.3, 1.0])
            p.setPen(pen)
            p.drawRoundedRect(ic.adjusted(1, 1, -1, -1), 2, 2)
        elif shape == "lock":
            p.drawRoundedRect(QRectF(ic.left() + 1, ic.top() + 4.5, 8, 5.5), 1, 1)
            p.drawArc(QRectF(ic.left() + 2.5, ic.top(), 5, 8), 0, 180 * 16)
        elif shape == "triangle":
            tri = QPainterPath(QPointF(ic.center().x(), ic.top() + 0.5))
            tri.lineTo(ic.right(), ic.bottom() - 0.5)
            tri.lineTo(ic.left(), ic.bottom() - 0.5)
            tri.closeSubpath()
            p.drawPath(tri)
        elif shape == "down":
            p.drawLine(QPointF(ic.center().x(), ic.top()), QPointF(ic.center().x(), ic.bottom()))
            p.drawLine(QPointF(ic.left() + 1.5, ic.center().y() + 1), QPointF(ic.center().x(), ic.bottom()))
            p.drawLine(QPointF(ic.right() - 1.5, ic.center().y() + 1), QPointF(ic.center().x(), ic.bottom()))
        p.setFont(self._font())
        p.setPen(col)
        p.drawText(r.adjusted(24, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, text)


# ====================================================================== readout
class ZReadout(QWidget):
    """`Z  3,015.00  µm from home`: one line, thin outline, no filled card."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = "— — —"
        self.dim = False
        self.warning = ""  # e.g. "above 40X limit": amber outline, value and text (colour never stands alone)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName("Z position")

    def set_warning(self, text: str) -> None:
        self.warning = text
        self.setAccessibleName(f"Z position, warning: {text}" if text else "Z position")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(240, 42)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, 42)

    def set_value(self, z: float | None, *, dim: bool = False) -> None:
        self.text = fmt_um(z) if z is not None else "— — —"
        self.dim = dim
        self.setAccessibleDescription(f"Z {self.text} micrometres from home" if z is not None else "Z unknown")
        self.update()

    def paintEvent(self, _e):
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        warn = QColor(t.warn) if self.warning else None
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1) if warn else QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if warn:
            fill = QColor(warn)
            fill.setAlpha(28)
            p.setBrush(fill)
            p.setPen(QPen(warn, 2))
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(t.bd), 1))
        p.drawRoundedRect(r, 8, 8)
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(warn or QColor(t.t2))
        p.drawText(r.adjusted(12, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "Z")
        unit = f"⚠ {self.warning}" if self.warning else "µm from home"
        f2 = QFont(self.font())
        f2.setPixelSize(12)
        if warn:
            f2.setWeight(QFont.DemiBold)
        p.setFont(f2)
        uw = p.fontMetrics().horizontalAdvance(unit)
        p.drawText(r.adjusted(0, 0, -12, 0), Qt.AlignVCenter | Qt.AlignRight, unit)
        mf = QFont(tk.FONT_MONO)
        mf.setPixelSize(22 if uw < 110 else 19)
        p.setFont(mf)
        p.setPen(warn or QColor(t.t2 if self.dim else t.tx))
        p.drawText(r.adjusted(30, 0, -(uw + 20), 0), Qt.AlignVCenter | Qt.AlignLeft, self.text)


# ====================================================================== tape
class FocusTape(QWidget):
    """Fixed accent pointer at the vertical centre; ticks scroll past it (higher Z is up). Fine: 1 px/µm, ticks
    10/25/50 µm. Coarse: 1 px per 20 µm, ticks 200/1,000 µm. ◆ vessel focus nominal, ● last capture Z, and a full-
    travel bar on the right edge."""

    W = 84

    def __init__(self, parent=None):
        super().__init__(parent)
        self.z = 0.0  # displayed (animated) Z
        self.z_max = 8050.0
        self.fine = True
        self.nominal: float | None = None
        self.last: float | None = None
        self.limit: float | None = None  # the objective's safe height: amber band above it
        self.known = True
        self.setFixedWidth(self.W)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def paintEvent(self, _e):
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        frame = QPainterPath()
        frame.addRoundedRect(r, 10, 10)
        p.fillPath(frame, QColor(t.s3))
        _inset(p, r, 10, 6, QColor(0, 0, 0, 90 if not _light() else 40), steps=4)
        p.save()
        p.setClipPath(frame)
        cy = r.center().y()
        ppu, minor, major = (1.0, 10, 50) if self.fine else (0.05, 200, 1000)
        reach = (r.height() / 2) / ppu + major
        mono = QFont(tk.FONT_MONO)
        mono.setPixelSize(12)
        p.setFont(mono)
        if self.known and self.limit is not None:
            y_lim = cy + (self.z - self.limit) * ppu  # higher Z is up: the band runs from the limit to the top
            if y_lim > r.top():
                amber = QColor(t.warn)
                band = QColor(amber)
                band.setAlpha(46)
                p.fillRect(QRectF(r.left(), r.top(), r.width(), min(y_lim, r.bottom()) - r.top()), band)
                if y_lim <= r.bottom():
                    pen = QPen(amber, 1.6)
                    pen.setDashPattern([3, 2])
                    p.setPen(pen)
                    p.drawLine(QPointF(r.left(), y_lim), QPointF(r.right() - 10, y_lim))
        if self.known:
            v = math.ceil((self.z - reach) / minor) * minor
            while v <= self.z + reach:
                if 0 <= v <= self.z_max:
                    y = cy + (self.z - v) * ppu
                    is_major = v % major == 0
                    mid = not is_major and self.fine and v % 25 == 0
                    w = 16 if is_major else 11 if mid else 7
                    p.setPen(QPen(QColor(t.t2 if is_major else t.bdd), 2 if is_major else 1))
                    p.drawLine(QPointF(r.left(), y), QPointF(r.left() + w, y))
                    if is_major and abs(y - cy) > 9:  # the pointer never strikes through a label (audit APP-09)
                        p.setPen(QColor(t.t2))
                        lab = "0 home" if v == 0 else f"{int(v):,}"
                        p.drawText(QRectF(r.left() + w + 4, y - 8, r.width() - w - 14, 16),
                                   Qt.AlignVCenter | Qt.AlignLeft, lab)
                v += minor
            for val, diamond, col in ((self.nominal, True, t.t2), (self.last, False, t.acc_tx)):
                if val is None:
                    continue
                y = cy + (self.z - val) * ppu
                if not r.top() <= y <= r.bottom():
                    continue
                p.save()
                p.translate(r.left() + 6.5, y)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(col))
                if diamond:
                    p.rotate(45)
                    p.drawRoundedRect(QRectF(-3.5, -3.5, 7, 7), 1, 1)
                else:
                    p.drawEllipse(QPointF(0, 0), 3.5, 3.5)
                p.restore()
        for top in (True, False):  # fades at both ends
            g = QLinearGradient(0, r.top() if top else r.bottom(), 0, r.top() + 30 if top else r.bottom() - 30)
            c0 = QColor(t.s3)
            c1 = QColor(t.s3)
            c1.setAlpha(0)
            g.setColorAt(0, c0)
            g.setColorAt(1, c1)
            p.fillRect(QRectF(r.left(), r.top() if top else r.bottom() - 30, r.width(), 30), g)
        acc = QColor(t.acc)
        glow = QColor(acc)
        glow.setAlpha(70)
        p.setPen(QPen(glow, 5))
        p.drawLine(QPointF(r.left(), cy), QPointF(r.right() - 10, cy))
        p.setPen(QPen(acc, 2))
        p.drawLine(QPointF(r.left(), cy), QPointF(r.right() - 10, cy))
        tri = QPainterPath(QPointF(r.left(), cy - 6))
        tri.lineTo(r.left() + 7, cy)
        tri.lineTo(r.left(), cy + 6)
        tri.closeSubpath()
        p.fillPath(tri, acc)
        bar = QRectF(r.right() - 7, r.top() + 8, 4, r.height() - 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.bd))
        p.drawRoundedRect(bar, 2, 2)
        if self.known and self.z_max > 0:
            f = 1 - min(1.0, max(0.0, self.z / self.z_max))
            p.setBrush(QColor(t.acc_tx))
            p.drawEllipse(QPointF(bar.center().x(), bar.top() + f * bar.height()), 4, 4)
        p.restore()
        p.setPen(QPen(QColor(t.bd), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 10, 10)


# ====================================================================== pad
class _Half(QAbstractButton):
    """One invisible, focusable hit area of the pad (top = up, bottom = down). The pad paints everything."""

    def __init__(self, direction: str, pad: FocusPad):
        super().__init__(pad)
        self.direction = direction
        self.pad = pad
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        # hold to keep moving: a click is one step; held past the delay it repeats (speed = step / interval)
        self.setAutoRepeat(True)
        self.setAutoRepeatDelay(400)
        self.setAutoRepeatInterval(100)
        # this press has auto-repeated (a hold). Reset only on a genuine new press: Qt emits pressed again during
        # every repeat, so the pressed signal cannot be used
        self.repeated = False

    def mousePressEvent(self, e):
        self.repeated = False
        super().mousePressEvent(e)

    def paintEvent(self, _e):  # the pad draws the button, chevrons and focus ring
        pass

    def enterEvent(self, e):
        self.pad.hover = self.direction
        self.pad._sync()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self.pad.hover == self.direction:
            self.pad.hover = None
        self.pad._sync()
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self.pad.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self.pad.update()
        super().focusOutEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() in (Qt.Key_Up, Qt.Key_Down) and not e.isAutoRepeat():
            if getattr(self, "_key_repeated", False):
                self._key_repeated = False
                self.pad.hold_released.emit()  # a held arrow key let go: stop Z now
            return
        super().keyReleaseEvent(e)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Up, Qt.Key_Down) and e.isAutoRepeat():
            self._key_repeated = True
        if k == Qt.Key_Space and not e.isAutoRepeat():
            self.repeated = False
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        if k in (Qt.Key_Up, Qt.Key_Down):
            if not self.pad.inert:
                self.pad.stepped.emit(1 if k == Qt.Key_Up else -1, 10 if e.modifiers() & Qt.ShiftModifier else 1,
                                      e.isAutoRepeat())
            return
        super().keyPressEvent(e)


class FocusPad(QWidget):
    """Stadium base, sunken well and a domed button whose face (four dots, top dot in the accent colour) turns with Z:
    one turn = 100 µm. Two hit areas: top half steps Z up (toward the sample), bottom half down."""

    #: (direction +1 / -1, multiplier, held): one step of the panel's current size; held = a repeat while the pad half
    #: or arrow key is held down (the page keeps a held move close behind the drive and stops it at the limit)
    stepped = Signal(int, int, bool)
    #: a hold (pad half or arrow key) was let go: the page cancels the running Z move so Z stops where it is
    hold_released = Signal()

    # design units (handoff: 340 x 370 pad area); the captions above and below keep a fixed 16 px band
    DW, DH = 340.0, 370.0
    CAP = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover: str | None = None
        self.moving: str | None = None  # "up"/"down" while the focus drive moves
        self.inert = False
        self.angle = 0.0
        self.step_label = "1 µm"
        self._off = 0.0  # animated button offset, design px (− = up)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_off)
        self.up = _Half("up", self)
        self.down = _Half("down", self)
        for h in (self.up, self.down):
            h.pressed.connect(self._sync)
            h.released.connect(self._sync)
            h.released.connect(lambda h=h: self._released(h))
        self.up.clicked.connect(lambda: self._clicked(self.up, 1))
        self.down.clicked.connect(lambda: self._clicked(self.down, -1))
        self.setMinimumSize(170, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.set_step_label("1 µm")

    def sizeHint(self) -> QSize:
        return QSize(220, 262)

    # ---------------------------------------------------------------- geometry
    def _room(self) -> float:
        return self.height() - 2 * (self.CAP + 2)

    def scale(self) -> float:
        return max(0.35, min(self.width() / self.DW, self._room() / self.DH, 1.0))

    def _origin(self) -> QPointF:
        s = self.scale()
        return QPointF((self.width() - self.DW * s) / 2, self.CAP + 2 + (self._room() - self.DH * s) / 2)

    def _d(self, x: float, y: float) -> QPointF:
        s, o = self.scale(), self._origin()
        return QPointF(o.x() + x * s, o.y() + y * s)

    def resizeEvent(self, e):
        s, o = self.scale(), self._origin()
        x, w = round(o.x() + 70 * s), round(200 * s)
        top, mid, bot = round(o.y()), round(o.y() + 185 * s), round(o.y() + 370 * s)
        self.up.setGeometry(x, top, w, mid - top)
        self.down.setGeometry(x, mid, w, bot - mid)
        super().resizeEvent(e)

    # ---------------------------------------------------------------- state
    def set_step_label(self, text: str) -> None:
        self.step_label = text
        self.up.setAccessibleName(f"Focus up {text}, toward the sample")
        self.down.setAccessibleName(f"Focus down {text}, away from the sample")
        self.up.setToolTip(f"Focus up {text} · toward the sample. Click = one step, hold = keep moving "
                           "(↑, Shift = ×10, wheel)")
        self.down.setToolTip(f"Focus down {text} · away from the sample. Click = one step, hold = keep moving "
                             "(↓, Shift = ×10, wheel)")

    def set_angle_from_z(self, z: float) -> None:
        self.angle = (z % 100.0) / 100.0 * 360.0
        self.update()

    def set_moving(self, direction: str | None) -> None:
        if direction != self.moving:
            self.moving = direction
            self._sync()

    def set_inert(self, on: bool) -> None:
        if on == self.inert:
            return
        self.inert = on
        for h in (self.up, self.down):
            h.setEnabled(not on)
        self._sync()

    def active(self) -> str | None:
        """The direction the button is pushed: while Z moves, or while a half is held under the pointer."""
        if self.moving:
            return self.moving
        for h in (self.up, self.down):
            if h.isDown():
                return h.direction
        return None

    def _target_off(self) -> float:
        a = self.active()
        if a:
            return -18.0 if a == "up" else 18.0
        if self.hover and not self.inert:
            return -2.0 if self.hover == "up" else 2.0
        return 0.0

    def _sync(self) -> None:
        want = self._target_off()
        if abs(want - self._off) < 0.01:
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(self._off)
        self._anim.setEndValue(want)
        self._anim.start()

    def _set_off(self, v) -> None:
        self._off = float(v)
        self.update()

    def _released(self, half: _Half) -> None:
        """Qt emits released during every auto-repeat too (the half is still down then). Released and no longer down
        after it repeated = the operator let go (or slid off the pad): end the hold."""
        if not half.isDown() and half.repeated:
            self.hold_released.emit()

    def _clicked(self, half: _Half, direction: int) -> None:
        """Qt emits clicked for each auto-repeat (the half is still down) and once more on release. A press that
        already repeated is a hold: its release adds no step, so Z stops when the operator lets go."""
        if half.isDown():
            half.repeated = True
            self._click(direction, True)
        elif half.repeated:
            half.repeated = False
        else:
            self._click(direction, False)

    def _click(self, direction: int, held: bool = False) -> None:
        if not self.inert:
            self.stepped.emit(direction, 1, held)

    def set_repeat_interval(self, ms: int) -> None:
        for h in (self.up, self.down):
            h.setAutoRepeatInterval(ms)

    def leaveEvent(self, e):
        self.hover = None
        self._sync()
        super().leaveEvent(e)

    def wheelEvent(self, e):
        if self.inert:
            return
        dy = e.angleDelta().y()
        if dy:
            self.stepped.emit(1 if dy > 0 else -1, 10 if e.modifiers() & Qt.ShiftModifier else 1, False)
        e.accept()

    # ---------------------------------------------------------------- paint
    def _static_layer(self, base_r: QRectF, well_r: QRectF, s: float, light: bool) -> QPixmap:
        """Base shadow, base gradient, well and its inset shadow: cached per (size, theme, device pixel ratio)."""
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), light, dpr)
        if getattr(self, "_cache_key", None) == key:
            return self._cache
        pm = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        _soft_shadow(p, base_r, 100 * s, (6 if light else 8) * s, (18 if light else 24) * s,
                     QColor(80, 90, 100, 64) if light else QColor(0, 0, 0, 115))
        base = QPainterPath()
        base.addRoundedRect(base_r, 100 * s, 100 * s)
        g = QLinearGradient(base_r.topLeft(), base_r.bottomLeft())
        g.setColorAt(0, QColor("#9DA4A8" if light else "#333B48"))
        g.setColorAt(1, QColor("#F5F8FA" if light else "#0F1216"))
        p.fillPath(base, g)
        well = QPainterPath()
        well.addRoundedRect(well_r, 78 * s, 78 * s)
        p.fillPath(well, QColor("#C5D1DA" if light else "#101317"))
        _inset(p, well_r, 78 * s, 22 * s, QColor(0, 0, 0, 110 if light else 230), steps=8)
        p.end()
        self._cache_key, self._cache = key, pm
        return pm

    def paintEvent(self, _e):
        t = theme.current()
        light = _light()
        s = self.scale()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.inert:
            p.setOpacity(0.55)
        active = self.active()
        lit = {d: (not self.inert) and (self.hover == d or active == d or h.hasFocus())
               for d, h in (("up", self.up), ("down", self.down))}
        cap = QFont(self.font())
        cap.setPixelSize(12)
        p.setFont(cap)
        for d, y, text in (("up", 0, "Up · toward sample"), ("down", self.height() - self.CAP, "Down · away from sample")):  # noqa: E501
            p.setPen(QColor(t.acc_tx if lit[d] else t.t3))
            p.drawText(QRectF(0, y, self.width(), self.CAP), Qt.AlignCenter, text)
        # chevrons outside the base
        for d, y in (("up", 2), ("down", 340)):
            c = self._d(170, y + 14)
            col = QColor(t.acc_tx if lit[d] else t.t3)
            sz = 9 * s
            path = QPainterPath()
            if d == "up":
                path.moveTo(c.x() - sz, c.y() + sz / 2)
                path.lineTo(c.x(), c.y() - sz / 2)
                path.lineTo(c.x() + sz, c.y() + sz / 2)
            else:
                path.moveTo(c.x() - sz, c.y() - sz / 2)
                path.lineTo(c.x(), c.y() + sz / 2)
                path.lineTo(c.x() + sz, c.y() - sz / 2)
            if lit[d]:
                g = QColor(t.acc)
                g.setAlpha(80)
                p.setPen(QPen(g, 6 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.drawPath(path)
            p.setPen(QPen(col, 2.6 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        # base (stadium ring) and sunken well: static, painted once per size and theme
        b0 = self._d(70, 35)
        base_r = QRectF(b0.x(), b0.y(), 200 * s, 300 * s)
        well_r = QRectF(0, 0, 155 * s, 255 * s)
        well_r.moveCenter(base_r.center())
        well = QPainterPath()
        well.addRoundedRect(well_r, 78 * s, 78 * s)
        p.drawPixmap(0, 0, self._static_layer(base_r, well_r, s, light))
        # focus ring on the focused half
        for h in (self.up, self.down):
            if h.hasFocus():
                p.save()
                half = QRectF(base_r.left(), base_r.top() if h is self.up else base_r.center().y(),
                              base_r.width(), base_r.height() / 2)
                p.setClipRect(half.adjusted(-4, -4, 4, 4))
                p.setPen(QPen(QColor(t.acc), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(base_r.adjusted(-3, -3, 3, 3), 103 * s, 103 * s)
                p.restore()
        # button: offset, sink and shadow follow the press pose
        pressed = active is not None
        k = 0.97 if pressed else 1.0
        tilt = (-1 if active == "up" else 1) if pressed else 0
        cen = QPointF(well_r.center().x(), well_r.center().y() + self._off * s)
        d_btn = 102 * s * k
        btn_r = QRectF(0, 0, d_btn, d_btn * (0.985 if pressed else 1.0))  # a slight squash reads as the ~10° tilt
        btn_r.moveCenter(cen)
        btn = QPainterPath()
        btn.addEllipse(btn_r)
        p.save()
        p.setClipPath(well)
        p.setPen(Qt.NoPen)
        rad = btn_r.width() / 2
        for dy, blur, a in ((4 if pressed else 9, 14, 0.5), (10 if pressed else 19, 8, 0.2), (16 if pressed else 30, 8, 0.35)):  # noqa: E501
            # a radial falloff is the cheap equivalent of a blurred circular shadow
            c = QPointF(cen.x(), cen.y() + dy * s)
            outer = rad + blur * s
            gr = QRadialGradient(c, outer)
            col = QColor(0, 0, 0, int(255 * a * (0.55 if light else 1)))
            clear = QColor(0, 0, 0, 0)
            gr.setColorAt(0, col)
            gr.setColorAt(max(0.0, (rad - blur * s * 0.5) / outer), col)
            gr.setColorAt(1, clear)
            p.setBrush(QBrush(gr))
            p.drawEllipse(c, outer, outer)
        p.restore()
        g = QLinearGradient(btn_r.topLeft(), btn_r.bottomLeft())
        g.setColorAt(0, QColor("#EFF1F1" if light else "#3A4350"))
        g.setColorAt(1, QColor("#86969C" if light else "#1C2129"))
        p.fillPath(btn, g)
        hi = QColor(255, 255, 255, 150 if light else 30)
        p.setPen(QPen(hi, 1.2))
        p.drawArc(btn_r.adjusted(1.5, 1.5, -1.5, -1.5), 30 * 16, 120 * 16)
        lo = QColor(0, 0, 0, 90 if light else 140)
        p.setPen(QPen(lo, 1.2))
        p.drawArc(btn_r.adjusted(1.5, 1.5, -1.5, -1.5), 210 * 16, 120 * 16)
        # face, turning with Z; under the tilt it shifts a little toward the pressed end
        d_face = 85 * s * k
        face_c = QPointF(cen.x(), cen.y() + tilt * 2.5 * s)
        face_r = QRectF(0, 0, d_face, d_face)
        face_r.moveCenter(face_c)
        face = QPainterPath()
        face.addEllipse(face_r)
        g = QLinearGradient(face_r.topLeft(), face_r.bottomLeft())
        g.setColorAt(0, QColor("#ADB9BF" if light else "#232932"))
        g.setColorAt(1, QColor("#D4DBDD" if light else "#2F3742"))
        p.fillPath(face, g)
        _inset(p, face_r, d_face / 2, 5 * s, QColor(152, 160, 163, 110) if light else QColor(0, 0, 0, 120), steps=3)
        p.save()
        p.translate(face_c)
        p.rotate(self.angle)
        rr = d_face * 0.4
        for i, (dx, dy) in enumerate(((0, -rr), (rr, 0), (0, rr), (-rr, 0))):
            col = QColor(t.acc if i == 0 else ("#E7ECEF" if light else "#4A5462"))
            if i == 0:
                gl = QRadialGradient(QPointF(dx, dy), 7 * s)
                c0 = QColor(t.acc)
                c0.setAlpha(120)
                c1 = QColor(t.acc)
                c1.setAlpha(0)
                gl.setColorAt(0, c0)
                gl.setColorAt(1, c1)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(gl))
                p.drawEllipse(QPointF(dx, dy), 7 * s, 7 * s)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 60))
            p.drawEllipse(QPointF(dx, dy + 1.2 * s), 4 * s, 4 * s)
            p.setBrush(col)
            p.drawEllipse(QPointF(dx, dy), 4 * s, 4 * s)
        p.restore()


# ====================================================================== panel
class ZFocusPanel(QWidget):
    """The whole Z focus block of the Capture page's Light & focus column."""

    #: (signed step in µm, held): the page turns it into a readback-based target, or refuses it
    step_requested = Signal(float, bool)
    #: the pad or arrow key hold was let go (stop Z now)
    hold_released = Signal()
    #: Set limit here / Clear limit for the objective in the light path
    limit_set_requested = Signal()
    limit_clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "ready"
        self.z: float | None = None
        self._z_disp = 0.0
        self.mode = "fine"
        self.step_idx = {"fine": 1, "coarse": 1}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        # header (title, status line, state chip): hidden at the operator's request (2026-11-18, "remove this part").
        # The state still drives the pad (inert when locked / fault / objective change) and the status line is the
        # readout's tooltip.
        self.header = QWidget()
        hdr = QHBoxLayout(self.header)
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.lbl_title = QLabel("Z focus")
        self.lbl_title.setObjectName("section")
        self.lbl_title.setFont(theme.section_font(self.lbl_title.font()))
        self.lbl_status = QLabel("—")
        self.lbl_status.setObjectName("caption")
        self.lbl_status.setMinimumWidth(1)
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        col.addWidget(self.lbl_title)
        col.addWidget(self.lbl_status)
        hdr.addLayout(col, 1)
        self.chip = StateChip()
        hdr.addWidget(self.chip, 0, Qt.AlignVCenter)
        lay.addWidget(self.header)
        self.header.setVisible(False)
        self.readout = ZReadout()
        lay.addWidget(self.readout)
        body = QHBoxLayout()
        body.setSpacing(10)
        self.tape = FocusTape()
        self.pad = FocusPad()
        body.addWidget(self.tape)
        body.addWidget(self.pad, 1)
        lay.addLayout(body, 1)
        self.lbl_legend = QLabel("◆ nominal · ● last capture · 1 turn = 100 µm")
        self.lbl_legend.setToolTip("◆ vessel focus nominal · ● Z of the last capture · one turn of the pad's face "
                                   "= 100 µm · the bar on the scale's right edge is the full Z travel")
        self.lbl_legend.setObjectName("caption")
        self.lbl_legend.setMinimumWidth(1)
        self.lbl_legend.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.lbl_legend)
        # mode + step
        row = QHBoxLayout()
        row.setSpacing(8)
        self.seg_mode = SegmentedControl([("coarse", "Coarse"), ("fine", "Fine")], accessible_name="Focus mode")
        self.seg_mode.set_current(self.mode)
        self.seg_mode.setToolTip("Coarse: 10–500 µm steps, 1,000 µm scale · Fine: 0.5–10 µm steps, 50 µm scale")
        row.addWidget(self.seg_mode, 1)
        # no "Step" caption: the column is ~290 px and the list's values ("1 µm") say what it is
        self.cmb_step = QComboBox()
        self.cmb_step.setAccessibleName("Focus step")
        self.cmb_step.setToolTip(f"Step per click (held: per repeat) · 1 encoder count ≈ {UM_PER_COUNT:g} µm")
        self.cmb_step.setMinimumWidth(80)
        row.addWidget(self.cmb_step)
        self.btn_limit = QToolButton()
        self.btn_limit.setPopupMode(QToolButton.InstantPopup)
        self.btn_limit.setAccessibleName("Objective safe height")
        self.btn_limit.setFixedSize(34, 34)
        self.btn_limit.setIconSize(QSize(18, 18))
        self.btn_limit.setStyleSheet("QToolButton { padding: 0px; } "
                                     "QToolButton::menu-indicator { image: none; width: 0px; }")
        self.limit_menu = QMenu(self.btn_limit)
        self.act_limit_set = self.limit_menu.addAction("Set limit here")
        self.act_limit_clear = self.limit_menu.addAction("Clear limit")
        self.btn_limit.setMenu(self.limit_menu)
        self.act_limit_set.triggered.connect(self.limit_set_requested)
        self.act_limit_clear.triggered.connect(self.limit_clear_requested)
        row.addWidget(self.btn_limit)
        # set/clear lives in Settings › Interface › Objective safe heights (operator 2026-11-18: not on the focus
        # panel); the tape's amber band and the readout still warn above the safe height
        self.btn_limit.hide()
        lay.addLayout(row)
        # autofocus
        af = QHBoxLayout()
        af.setSpacing(6)
        self.btn_find = QPushButton("Find sample")
        self.btn_fine = QPushButton("Auto fine")  # a plain button: Capture is the page's one primary action
        self.btn_snap = QPushButton("Snap to last")
        self.btn_find.setToolTip("Wide search around the vessel focus nominal")
        self.btn_fine.setToolTip("Narrow search around the current focus (Ctrl+F)")
        self.btn_snap.setToolTip("Return focus to the Z of the last capture")
        for b in (self.btn_find, self.btn_fine, self.btn_snap):
            b.setMinimumWidth(1)
            b.setMinimumHeight(34)
            b.setStyleSheet("padding-left: 4px; padding-right: 4px;")  # three labels share a ~290 px column
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            af.addWidget(b, 1)
        lay.addLayout(af)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(450)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._show_z)
        self.seg_mode.activated.connect(self.set_mode)
        self.cmb_step.activated.connect(self._step_picked)
        self.pad.stepped.connect(self._stepped)
        self.pad.hold_released.connect(self.hold_released)
        self._fill_steps()

    # ---------------------------------------------------------------- mode / step
    def steps(self) -> tuple[float, ...]:
        return FINE_STEPS if self.mode == "fine" else COARSE_STEPS

    def step_um(self) -> float:
        return self.steps()[self.step_idx[self.mode]]

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.seg_mode.set_current(mode)
        self.tape.fine = mode == "fine"
        self.pad.set_repeat_interval(100 if mode == "fine" else 150)  # held speed = step / interval
        self._fill_steps()
        self.tape.update()

    def set_step(self, um: float) -> None:
        self.step_idx[self.mode] = self.steps().index(um)
        self._fill_steps()

    def _fill_steps(self) -> None:
        self.cmb_step.blockSignals(True)
        self.cmb_step.clear()
        for v in self.steps():
            self.cmb_step.addItem(f"{v:g} µm", v)
        self.cmb_step.setCurrentIndex(self.step_idx[self.mode])
        self.cmb_step.blockSignals(False)
        self.pad.set_step_label(f"{self.step_um():g} µm")

    def _step_picked(self, i: int) -> None:
        self.step_idx[self.mode] = i
        self.pad.set_step_label(f"{self.step_um():g} µm")

    def _stepped(self, direction: int, mult: int, held: bool = False) -> None:
        if self.state in INERT:
            return
        self.step_requested.emit(direction * mult * self.step_um(), held)

    # ---------------------------------------------------------------- values / state
    def set_values(self, z: float | None, z_max: float | None, nominal: float | None, last: float | None) -> None:
        """`z` is the focus drive's readback (µm from home)."""
        key = (z, z_max, nominal, last, self.state)
        if key == getattr(self, "_values_key", None):
            return  # the page refreshes at the telemetry rate: repaint only on a change
        self._values_key = key
        if z_max:
            self.tape.z_max = z_max
        self.tape.nominal, self.tape.last = nominal, last
        known = z is not None and self.state not in ("locked",)
        self.tape.known = known
        self.readout.set_value(z if known else None, dim=self.state == "simulated")
        if z is not None and (self.z is None or abs(z - self.z) > 1e-6):
            start = self._z_disp if self.z is not None else z
            self.z = z
            self._anim.stop()
            self._anim.setStartValue(start)
            self._anim.setEndValue(z)
            self._anim.start()
        lo_hi = f"0 to {self.tape.z_max:,.0f} µm"
        self.tape.setAccessibleName(f"Focus scale: Z {fmt_um(z)} µm from home, of {lo_hi}" if known
                                    else "Focus scale: Z unknown")
        self.tape.update()

    def _show_z(self, v) -> None:
        self._z_disp = float(v)
        self.tape.z = self._z_disp
        self.pad.set_angle_from_z(self._z_disp)
        self.tape.update()

    def set_limit(self, limit_um: float | None, objective: str, z: float | None) -> None:
        """The objective's safe height: an amber band on the tape above it, and an amber readout while Z is above."""
        above = limit_um is not None and z is not None and z > limit_um + 1e-6
        t = theme.current()
        key = (limit_um, objective, above, z, t.name)
        if key == getattr(self, "_limit_key", None):
            return
        self._limit_key = key
        # amber when a safe height is set for this objective, secondary text colour when none
        self.btn_limit.setIcon(icon("warning", t.warn if limit_um is not None else t.t2))
        self.tape.limit = limit_um
        self.tape.update()
        self.readout.set_warning(f"above {objective} limit" if above else "")
        here = f" ({fmt_um(z)} µm)" if z is not None else ""
        self.act_limit_set.setText(f"Set {objective} limit here{here}")
        self.act_limit_set.setEnabled(z is not None and bool(objective))
        self.act_limit_clear.setText(f"Clear {objective} limit" + (f" ({fmt_um(limit_um)} µm)" if limit_um else ""))
        self.act_limit_clear.setEnabled(limit_um is not None)
        tip = (f"{objective} safe height: {fmt_um(limit_um)} µm. Above it the readout turns amber and holding the "
               "pad stops at the limit; a single click can still go past it." if limit_um is not None else
               f"No safe height set for {objective}. Move Z to where this objective nears the vessel, then "
               "Set limit here.")
        self.btn_limit.setToolTip(tip)

    def set_state(self, state: str, status: str, moving: str | None = None) -> None:
        if (state, status, moving) == getattr(self, "_state_key", None):
            return
        self._state_key = (state, status, moving)
        self.state = state
        self.chip.set_state(state)
        self.lbl_status.setText(status)
        self.lbl_status.setToolTip(status)
        self.readout.setToolTip(f"Focus drive: {status}")
        self.pad.set_inert(state in INERT)
        self.pad.set_moving(moving)
        held = state in ("objective",)
        for w in (self.seg_mode, self.cmb_step, self.btn_find, self.btn_fine, self.btn_snap):
            w.setEnabled(not held and state not in ("locked", "fault"))
