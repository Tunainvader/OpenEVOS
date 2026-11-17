"""Capture-tab controls: Medium Jog joystick, channel button with buffer thumbnail, focus slider
with LAST marker, histogram strip."""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider, QWidget

from openevos.ui import theme
from openevos.ui.widgets.image_view import to_qimage

#: jog pad metal by theme: the design's brushed silver on light; gunmetal on dark so the ring sits in the dark shade
_PAD = {
    "light": {"ring": ("#9DA4A8", "#F5F8FA"), "ring_edge": (0, 0, 0, 40),
              "well": ("#C5D1DA", "#B6C2CB", "#6E777D", "#2E3337"),
              "btn": ("#EFF1F1", "#86969C"), "btn_edge": (255, 255, 255, 150), "inner": ("#ADB9BF", "#D4DBDD"),
              "inner_edge": (152, 160, 163, 110), "dot": "#E7ECEF", "chev": "#B4B9BD", "chev_hi": (255, 255, 255, 120),
              "shadow": 1.0},
    "dark": {"ring": ("#1B2027", "#3A424C"), "ring_edge": (255, 255, 255, 18),
             "well": ("#3B444E", "#323A43", "#1A1F25", "#07090C"),
             "btn": ("#6A747E", "#2B3239"), "btn_edge": (255, 255, 255, 55), "inner": ("#3C454E", "#55606A"),
             "inner_edge": (0, 0, 0, 90), "dot": "#9AA4AD", "chev": "#5C6670", "chev_hi": (0, 0, 0, 120),
             "shadow": 1.25},
}


def _pad_colours() -> dict:
    return _PAD["light" if theme.current().name == "light" else "dark"]


#: arrow keys -> index into Joystick.DIRS (up, right, down, left)
_JOG_KEYS = {Qt.Key_Up: 0, Qt.Key_Right: 1, Qt.Key_Down: 2, Qt.Key_Left: 3}


class Joystick(QWidget):
    """Medium Jog: a machined-metal four-way pad. Hover a side to light its arrow; press and hold to jog the stage
    that way (one axis at a time, ADR-0040). How far from the centre you press sets the pace (fraction 0.35..1).
    Emits `jog(vx, vy)` every 180 ms while held (vy < 0 = up/away) and `released` on release; the page turns them into
    short, verified relative moves. Arrow keys drive it too when it has focus."""

    jog = Signal(float, float)
    released = Signal()

    #: (dx, dy) unit vectors for up, right, down, left
    DIRS = ((0, -1), (1, 0), (0, 1), (-1, 0))
    AMBER, AMBER_GLOW = "#E3A560", "#F1B475"
    DEAD_PX = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(156, 156)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._v = QPointF(0, 0)
        self._held = False
        self._hover: int | None = None  # index into DIRS
        self._timer = QTimer(self)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._tick)
        self.setToolTip("Medium Jog: hover a side to aim, press and hold to move the stage that way "
                        "(further from the centre = faster); arrow keys work too")
        self.setAccessibleName("Stage jog pad")

    # ------------------------------------------------------------------ geometry
    @property
    def _c(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    @property
    def _s(self) -> float:
        """Scale from the 200 px design to this widget (the ring is 200 design px across)."""
        return (min(self.width(), self.height()) - 46) / 200

    def _dir_at(self, pos: QPointF) -> int | None:
        d = pos - self._c
        if max(abs(d.x()), abs(d.y())) < self.DEAD_PX:
            return None
        if abs(d.x()) >= abs(d.y()):
            return 1 if d.x() > 0 else 3
        return 2 if d.y() > 0 else 0

    def _set(self, pos: QPointF) -> None:
        i = self._dir_at(pos)
        if i is None:
            self._v = QPointF(0, 0)
        else:
            d = pos - self._c
            frac = min(1.0, max(0.35, math.hypot(d.x(), d.y()) / (100 * self._s)))
            dx, dy = self.DIRS[i]
            self._v = QPointF(dx * frac, dy * frac)
        self._hover = i
        self.update()

    # ------------------------------------------------------------------ input
    def _tick(self) -> None:
        if self._held and (abs(self._v.x()) > 0.08 or abs(self._v.y()) > 0.08):
            self.jog.emit(self._v.x(), self._v.y())

    def mousePressEvent(self, e):
        if not self.isEnabled():
            return
        self._held = True
        self._set(e.position())
        self._tick()
        self._timer.start()

    def mouseMoveEvent(self, e):
        if not self.isEnabled():
            return
        was = max(abs(self._v.x()), abs(self._v.y()))
        if self._held:
            self._set(e.position())
            if was <= 0.3 < max(abs(self._v.x()), abs(self._v.y())):
                self._tick()  # react the moment the pad is pushed, not on the next timer tick
        else:
            i = self._dir_at(e.position())
            if i != self._hover:
                self._hover = i
                self.update()

    def _end_hold(self, hover: int | None) -> None:
        self._held = False
        self._timer.stop()
        self._v = QPointF(0, 0)
        self.released.emit()
        self._hover = hover
        self.update()

    def mouseReleaseEvent(self, e):
        self._end_hold(self._dir_at(e.position()))

    def leaveEvent(self, e):
        if not self._held:
            self._hover = None
            self.update()

    def keyPressEvent(self, e):
        i = _JOG_KEYS.get(e.key())
        if i is None or not self.isEnabled():
            return super().keyPressEvent(e)
        if not e.isAutoRepeat():
            dx, dy = self.DIRS[i]
            self._held, self._hover, self._v = True, i, QPointF(dx * 0.7, dy * 0.7)
            self._tick()
            self._timer.start()
            self.update()

    def keyReleaseEvent(self, e):
        if e.key() in _JOG_KEYS and not e.isAutoRepeat() and self._held:
            self._end_hold(None)
            return
        super().keyReleaseEvent(e)

    # ------------------------------------------------------------------ painting
    @staticmethod
    def _lin(r: QRectF, top: str, bottom: str):
        g = QLinearGradient(r.center().x(), r.top(), r.center().x(), r.bottom())
        g.setColorAt(0, QColor(top))
        g.setColorAt(1, QColor(bottom))
        return g

    def _shadow(self, p: QPainter, c: QPointF, r: float, dy: float, alpha: int, spread: float) -> None:
        """Soft drop shadow: a few concentric translucent ellipses (QPainter has no blur)."""
        g = QRadialGradient(c + QPointF(0, dy), r + spread)
        g.setColorAt(r / (r + spread) * 0.85, QColor(0, 0, 0, alpha))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c + QPointF(0, dy), r + spread, r + spread)

    def _chevron(self, p: QPainter, i: int, active: bool) -> None:
        s, c = self._s, self._c
        dx, dy = self.DIRS[i]
        tip = c + QPointF(dx, dy) * (100 * s + 20)
        back = c + QPointF(dx, dy) * (100 * s + 11)
        side = QPointF(-dy, dx) * 8
        path = QPainterPath()
        path.moveTo(back + side)
        path.lineTo(tip)
        path.lineTo(back - side)
        k = _pad_colours()
        pen = QPen(QColor(self.AMBER if active else k["chev"]), 4.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        if active:
            glow = QPen(QColor(self.AMBER_GLOW), 8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            col = glow.color()
            col.setAlpha(90)
            glow.setColor(col)
            p.strokePath(path, glow)
        else:
            p.strokePath(path.translated(1, 1), QPen(QColor(*k["chev_hi"]), 4.2, Qt.SolidLine, Qt.RoundCap,
                                                    Qt.RoundJoin))
        p.strokePath(path, pen)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s, c, en = self._s, self._c, self.isEnabled()
        k = _pad_colours()
        lit = self._hover if en else None
        if not en:  # greyed, not hidden: the stage is locked, busy or not under control
            p.setOpacity(0.42)
        # outer ring ("around"): light at the bottom, darker at the top
        ring = QRectF(c.x() - 100 * s, c.y() - 100 * s, 200 * s, 200 * s)
        p.setPen(QPen(QColor(*k["ring_edge"]), 1))
        p.setBrush(self._lin(ring, *k["ring"]))
        p.drawEllipse(ring)
        # recessed well ("handle"): flat grey with a dark inner shadow towards the rim
        hr = 77.5 * s
        well = QRadialGradient(c, hr)
        for stop, col in zip((0.0, 0.70, 0.90, 1.0), k["well"], strict=True):
            well.setColorAt(stop, QColor(col))
        p.setPen(Qt.NoPen)
        p.setBrush(well)
        p.drawEllipse(c, hr, hr)
        # the button tilts towards the aimed side (CSS: translate 5 px + rotate 8 deg)
        shift = QPointF(*self.DIRS[lit]) * (5 * s * (1.4 if self._held else 1.0)) if lit is not None else QPointF()
        bc = c + shift
        br = 51 * s
        self._shadow(p, bc - shift * 0.6, br, 14 * s, min(255, int(120 * k["shadow"])), 12 * s)
        self._shadow(p, bc - shift * 0.6, br, 26 * s, min(255, int(70 * k["shadow"])), 8 * s)
        btn = QRectF(bc.x() - br, bc.y() - br, 2 * br, 2 * br)
        p.setPen(QPen(QColor(*k["btn_edge"]), 1.2))
        p.setBrush(self._lin(btn, *k["btn"]))
        p.drawEllipse(btn)
        ir = 42.5 * s
        inner = QRectF(bc.x() - ir, bc.y() - ir, 2 * ir, 2 * ir)
        p.setPen(QPen(QColor(*k["inner_edge"]), 1))
        p.setBrush(self._lin(inner, *k["inner"]))
        p.drawEllipse(inner)
        for dx, dy in self.DIRS:  # the four grip dots at 10 % / 90 % of the inner disc
            d = bc + QPointF(dx, dy) * (0.8 * ir)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 70))
            p.drawEllipse(d + QPointF(0, 1.4), 4 * s + 0.5, 4 * s + 0.5)
            p.setBrush(QColor(k["dot"]))
            p.drawEllipse(d, 4 * s, 4 * s)
        for i in range(4):
            self._chevron(p, i, lit == i)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.ACCENT), 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, 100 * s + 3, 100 * s + 3)


class ChannelButton(QWidget):
    """Light channel button (blue when selected) with the buffer thumbnail underneath (manual p.8)."""

    clicked = Signal(str)

    def __init__(self, slot: str, label: str, color: str, parent=None):
        super().__init__(parent)
        self.slot, self.label, self.color = slot, label, color
        self.selected = False
        self.thumb: QPixmap | None = None
        self.reason = ""
        self.setFixedSize(68, 92)
        self.setCursor(Qt.PointingHandCursor)
        # keyboard: Tab reaches each channel; Space / Enter selects it (Impeccable audit 2026-11-18)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(f"Channel {label}")

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter) and self.isEnabled():
            self.clicked.emit(self.slot)
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.update()

    def set_thumbnail(self, rgb: np.ndarray | None) -> None:
        if rgb is None:
            self.thumb = None
        else:
            step = max(1, rgb.shape[1] // 128)
            self.thumb = QPixmap.fromImage(to_qimage(np.ascontiguousarray(rgb[::step, ::step])))
        self.update()

    def set_available(self, ok: bool, reason: str = "") -> None:
        self.setEnabled(ok)
        self.reason = reason
        self.setToolTip(reason or f"{self.label}: select channel")
        self.update()

    def mouseReleaseEvent(self, e):
        if self.isEnabled() and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.slot)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        top = QRectF(1, 1, self.width() - 2, 34)
        en = self.isEnabled()
        if not en:
            p.setOpacity(0.5)
        # soft UI: raised at rest, pressed in (inset) when selected, like the stylesheet's checked buttons
        theme.paint_surface(p, top, 10, inset=self.selected)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.ACCENT), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(top.adjusted(1.5, 1.5, -1.5, -1.5), 9, 9)
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))  # outline keeps light pseudo colours visible
        p.setBrush(QColor(self.color))
        p.drawEllipse(QPointF(12, top.center().y()), 5, 5)
        f = QFont(self.font())
        f.setBold(True)
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(QColor(theme.ACCENT_TX if self.selected else theme.TEXT))
        p.drawText(top.adjusted(18, 0, -2, 0), Qt.AlignCenter, self.label)
        th = QRectF(4, 40, self.width() - 8, self.height() - 44)
        if self.thumb is not None:  # the channel's last capture, on the black stage colour
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.setBrush(QColor(theme.STAGE))
            p.drawRoundedRect(th, 6, 6)
            p.drawPixmap(th.adjusted(1, 1, -1, -1).toRect(), self.thumb)
        else:  # no capture yet: a quiet inset well, no "empty" label (Impeccable audit 2026-11-18)
            theme.paint_surface(p, th, 6, inset=True, control=False)


class MarkedSlider(QSlider):
    """Vertical/horizontal slider that can draw a LAST marker (Quick slider, manual p.9)."""

    def __init__(self, orientation=Qt.Vertical, parent=None):
        super().__init__(orientation, parent)
        self.marker: int | None = None
        self.marker_text = "LAST"

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.marker is None or not (self.minimum() <= self.marker <= self.maximum()):
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        span = self.maximum() - self.minimum() or 1
        frac = (self.marker - self.minimum()) / span
        p = QPainter(self)
        p.setPen(QPen(QColor(theme.WARN), 2))
        f = QFont(self.font())
        f.setPointSize(7)
        f.setBold(True)
        p.setFont(f)
        if self.orientation() == Qt.Vertical:
            y = groove.bottom() - frac * groove.height()
            p.drawLine(groove.center().x() - 9, int(y), groove.center().x() + 9, int(y))
            p.drawText(QRectF(groove.center().x() + 10, y - 7, 34, 14), Qt.AlignLeft | Qt.AlignVCenter, self.marker_text)
        else:
            x = groove.left() + frac * groove.width()
            p.drawLine(int(x), groove.center().y() - 9, int(x), groove.center().y() + 9)


class LevelsHistogram(QWidget):
    """Log histogram of the raw frame with draggable black/white point handles (manual levels).
    Double-click returns to auto range. Shows clipped fractions at both ends (text, not only colour)."""

    levelsChanged = Signal(float, float)
    autoRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.counts: np.ndarray | None = None
        self.color = theme.ACCENT
        self.max_value = 4095
        self.lo, self.hi = 0.0, 4095.0
        self.clip_lo = self.clip_hi = 0.0
        self.auto = True
        self.rgb: list[np.ndarray] | None = None  # colour camera: per-channel counts, else None
        self._drag: str | None = None
        self.setMinimumHeight(70)
        self.setToolTip("Drag the triangles to set black and white points · double-click for auto range")
        self.setAccessibleName("Display levels histogram")

    def sizeHint(self) -> QSize:
        return QSize(300, 76)

    #: red, green, blue curve colours for the colour camera; mid-saturation so each reads on both themes
    RGB_COLORS = ("#E5484D", "#30A46C", "#3E8EED")

    def set_data(self, counts, max_value: int, lo: float, hi: float, auto: bool, color: str,
                 clip: tuple[float, float]) -> None:
        self.counts, self.max_value, self.lo, self.hi, self.auto = counts, max_value, lo, hi, auto
        self.rgb = None
        self.color = color
        self.clip_lo, self.clip_hi = clip
        self.update()

    def set_rgb(self, counts_rgb, clip: tuple[float, float]) -> None:
        """Colour camera: three overlaid R, G, B curves of the 8-bit frame. There are no black/white handles here
        (colour adjustments live in Settings › Color camera), so dragging and double-click do nothing."""
        self.rgb = [np.asarray(c) for c in counts_rgb]
        self.counts, self.max_value, self.lo, self.hi, self.auto = None, 255, 0.0, 255.0, True
        self.clip_lo, self.clip_hi = clip
        self.update()

    def _curve(self, r: QRectF, c: np.ndarray, peak: float) -> QPainterPath:
        bw = r.width() / len(c)
        path = QPainterPath(QPointF(r.left(), r.bottom()))
        for i, v in enumerate(c):
            path.lineTo(r.left() + i * bw, r.bottom() - v / peak * (r.height() - 2))
        path.lineTo(r.right(), r.bottom())
        return path

    def _plot(self) -> QRectF:
        return QRectF(self.rect()).adjusted(8, 2, -8, -30)

    def _x(self, v: float) -> float:
        r = self._plot()
        return r.left() + r.width() * min(1.0, max(0.0, v / max(1, self.max_value)))

    def _v(self, x: float) -> float:
        r = self._plot()
        return min(1.0, max(0.0, (x - r.left()) / max(1.0, r.width()))) * self.max_value

    def mousePressEvent(self, e):
        if self.rgb is not None:
            return
        x = e.position().x()
        self._drag = "lo" if abs(x - self._x(self.lo)) <= abs(x - self._x(self.hi)) else "hi"
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        v = self._v(e.position().x())
        if self._drag == "lo":
            self.lo = min(v, self.hi - 1)
        else:
            self.hi = max(v, self.lo + 1)
        self.auto = False
        self.update()
        self.levelsChanged.emit(self.lo, self.hi)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if self.rgb is None:
            self.autoRequested.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot()
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QColor(theme.INPUT))
        p.drawRoundedRect(r, 4, 4)
        f = QFont(self.font())
        f.setPixelSize(10)
        if self.rgb is not None:
            logs = [np.log1p(c.astype(float)) for c in self.rgb]
            peak = max((float(c.max()) for c in logs if c.size), default=0.0) or 1.0  # one scale: heights compare
            for c, hexcol in zip(logs, self.RGB_COLORS, strict=False):
                if not c.size:
                    continue
                path = self._curve(r, c, peak)
                fill = QColor(hexcol)
                fill.setAlpha(55)
                p.fillPath(path, fill)
                p.setPen(QPen(QColor(hexcol), 1.4))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
            p.setFont(f)
            x = r.left()
            for name, hexcol in zip("RGB", self.RGB_COLORS, strict=False):
                p.setPen(QColor(hexcol))
                p.drawText(QRectF(x, r.bottom() + 13, 12, 14), Qt.AlignLeft | Qt.AlignVCenter, name)
                x += 12
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(QRectF(x + 4, r.bottom() + 13, r.width() * 0.5, 14), Qt.AlignLeft | Qt.AlignVCenter, "8-bit")
            warn = self.clip_lo > 0.001 or self.clip_hi > 0.001
            p.setPen(QColor(theme.WARN if warn else theme.TEXT_2))
            p.drawText(QRectF(r.left(), r.bottom() + 13, r.width(), 14), Qt.AlignRight | Qt.AlignVCenter,
                       f"clip ↓{self.clip_lo * 100:.1f}% ↑{self.clip_hi * 100:.1f}%")
            return
        if self.counts is not None and self.counts.size:
            c = np.log1p(self.counts.astype(float))
            c = c / (c.max() or 1)
            bw = r.width() / len(c)
            path = QPainterPath(QPointF(r.left(), r.bottom()))
            for i, v in enumerate(c):
                path.lineTo(r.left() + i * bw, r.bottom() - v * (r.height() - 2))
            path.lineTo(r.right(), r.bottom())
            path.closeSubpath()
            from openevos.ui import tokens as tk

            # a near-white channel colour (TRANS) vanishes on the light plot: fall back to the text colour
            col = QColor(self.color if tk.contrast(self.color, theme.INPUT) >= 1.6 else theme.TEXT_2)
            col.setAlpha(150)
            p.fillPath(path, col)
        xl, xh = self._x(self.lo), self._x(self.hi)
        # outside the black/white points: dim towards the shade colour (a light-theme background wash hid the curve)
        shade = QColor(theme.SH if theme.current().name == "light" else theme.BG)
        shade.setAlpha(90 if theme.current().name == "light" else 140)
        p.fillRect(QRectF(r.left(), r.top(), xl - r.left(), r.height()), shade)
        p.fillRect(QRectF(xh, r.top(), r.right() - xh, r.height()), shade)
        for x, fill in ((xl, "#000000"), (xh, "#FFFFFF")):
            tri = QPainterPath()
            tri.moveTo(x, r.bottom() + 1)
            tri.lineTo(x - 6, r.bottom() + 12)
            tri.lineTo(x + 6, r.bottom() + 12)
            tri.closeSubpath()
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
            p.setBrush(QColor(fill))
            p.drawPath(tri)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(r.left(), r.bottom() + 13, r.width() * 0.6, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{'Auto' if self.auto else 'Manual'} {self.lo:.0f}–{self.hi:.0f}")
        warn = self.clip_lo > 0.001 or self.clip_hi > 0.001
        p.setPen(QColor(theme.WARN if warn else theme.TEXT_2))
        p.drawText(QRectF(r.left(), r.bottom() + 13, r.width(), 14), Qt.AlignRight | Qt.AlignVCenter,
                   f"clip ↓{self.clip_lo * 100:.1f}% ↑{self.clip_hi * 100:.1f}%")
