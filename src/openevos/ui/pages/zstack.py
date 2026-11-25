"""Z-stack page (ADR-0064; design review 2026-11-24): find the ends on live view, see the stack before it runs, run it
with progress and Stop, browse slices and projections, save.

Left, set-up (fits 1440x900 without scrolling):
* Centre on the focus (above / below), or Top and bottom set from live focus (ZEN First/Last, NIS Top/Bottom);
* the focus readout and nudge (Ctrl+Up / Ctrl+Down; down = away from the sample);
* the stack drawn to scale, with Go to buttons ON the planes they move to (ZEN's F/C/L); the safe height is grey until
  the stack crosses it, then the region above is hatched red; while a stack runs the drawing fills in;
* Sampling: Step and Slices edit each other (ZEN Interval/Slices, NIS step/number of steps); the Nyquist step is a
  button that shows its value; heavy oversampling gets a note;
* channels as chips with their display colour; their order;
* More options and Saving as one-line rows that show their values;
* the footer: four numbers (slices x channels, time, size, light on), then errors (red), the safe-height question
  (amber) and notes (grey), then Acquire / Stop.

Right, the result: Live set apart from the six result views (Slices, Max, Min, Mean, Best focus, Extended focus); each
slice shows as it arrives; play; focus curve with its Z scale and the best slice; Go to this Z; Save stack.
"""

from __future__ import annotations

import math
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openevos.acquisition.zstack import (
    MAX_SLICES,
    ChannelOrder,
    fmt_seconds,
    optimal_step_um,
    plan_from_settings,
    plan_positions,
    preflight,
)
from openevos.imaging.composite import auto_range
from openevos.imaging.zproject import DERIVED, METHODS, focus_curve, focus_measure, project
from openevos.imaging.zviews import OrthoLayout, merge_rgb, ortho_composite, sharp_range, z_at
from openevos.ui import theme
from openevos.ui import tokens as tk
from openevos.ui.common import hbox, label, panel, render_frame
from openevos.ui.widgets.image_view import ImageView
from openevos.ui.widgets.segmented import SegmentedControl

TASK = "Z-stack"
NUDGES = (0.5, 1.0, 2.0, 5.0, 10.0, 50.0)
#: a note when the step is this many times finer than the axial Nyquist step (which already oversamples 2x)
OVERSAMPLE_NOTE = 4.0
#: kept free on the save disk beyond the stack and its projections
STORAGE_RESERVE = 512e6
VIEW_TIPS = {"live": "Live view from the camera (paused while a stack runs)", "slice": "Browse the slices",
             "max": "Maximum intensity projection", "min": "Minimum intensity projection",
             "mean": "Mean intensity projection",
             "sum": "Sum of all slices (32-bit; leaves the camera's range)",
             "median": "Median of the slices at each pixel", "std": "Standard deviation through the stack (32-bit)",
             "best": "The single sharpest slice: one whole plane, chosen by variance of the Laplacian",
             "edf": "Extended focus: each pixel from the slice where it is sharpest (experimental)",
             "ortho": "XZ and YZ cross-sections through the stack (Fiji Orthogonal Views). Click to move the cut"}
MERGED = "__merged__"


def default_save_root() -> Path:
    return Path.home() / "Documents" / "OpenEVOS" / "Z-stacks"


def _size(n: float) -> str:
    for unit, k in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= k:
            return f"{n / k:,.1f} {unit}" if n / k < 100 else f"{n / k:,.0f} {unit}"
    return f"{n / 1e3:,.0f} kB"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _caption_font(base: QFont) -> QFont:
    f = QFont(base)
    f.setPixelSize(theme.px("caption"))
    return f


class _NoHeightForWidth(QWidget):
    """A container that does not pass its children's height-for-width (wrapped labels) up. The shell hosts each page in
    a resizable QScrollArea, which sizes a height-for-width page to its preferred height: after a theme switch the page
    stayed 792 px tall in a 760 px window and Acquire went off screen (design review 2026-11-24)."""

    def hasHeightForWidth(self) -> bool:
        return False


class _Elided(QLabel):
    """A one-line label that shortens with an ellipsis instead of pushing its row wider."""

    def __init__(self, text: str = "", kind: str = "caption"):
        super().__init__(text)
        self.setObjectName(kind)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(), self.alignment(), text)


# ====================================================================================== stack picture
class ZRangeView(QWidget):
    """The stack drawn to scale (redesign kit, Stack Plan ruler): a Z axis with the top, focus and bottom heights on the
    left, the slices as stacked planes over the objective (inverted stand: up = toward the sample), the focus as a
    diamond on the centre line, the vessel's focus nominal tagged, the safe height as an amber dashed line (the region
    above hatched red when the stack crosses it) and the travel limits as solid lines. Go to buttons on the right move
    the focus to the top, centre or bottom slice. While a stack runs, the slices taken fill in."""

    go = Signal(str)  # "top" | "centre" | "bottom"

    AXIS_W = 92.0  # left: Z labels
    BTN_W = 96.0  # right: Go to buttons and tags
    TOP_PAD, BOTTOM_PAD = 30.0, 58.0  # room for the safe-height label above, the objective below

    def __init__(self, parent=None):
        super().__init__(parent)
        self.positions: list[float] = []
        self.focus: float | None = None
        self.current: float | None = None
        self.safe: tuple[str, float] | None = None
        self.limits: tuple[float, float] | None = None
        self.taken: int | None = None  # running: slices taken in this pass (None = not running)
        self.nominal: float | None = None  # the vessel's focus nominal (drawn when in view)
        self.objective: str = ""  # e.g. "10× / NA 0.25", written on the objective
        self.run_index: int | None = None
        self.btn_top = QPushButton("Top")
        self.btn_centre = QPushButton("Centre")
        self.btn_bottom = QPushButton("Bottom")
        self._buttons: dict[str, QPushButton] = {"top": self.btn_top, "centre": self.btn_centre,
                                                 "bottom": self.btn_bottom}
        for which, b in self._buttons.items():
            b.setParent(self)
            b.setObjectName("chip")
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            b.clicked.connect(self._make_go(which))
        self.setMinimumHeight(round(236 * min(theme.TEXT_SCALE, 1.3)))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Z-stack diagram")

    def _make_go(self, which: str):
        def fire(_checked: bool = False) -> None:
            self.go.emit(which)
        return fire

    def sizeHint(self) -> QSize:
        return QSize(340, self.minimumHeight())

    # ---------------------------------------------------------------- state
    def set_state(self, positions, focus, safe, limits, current=None) -> None:
        self.positions, self.focus, self.safe, self.limits, self.current = list(positions), focus, safe, limits, current
        self.setAccessibleDescription(self.describe())
        self.setToolTip(self.describe())
        self._place_buttons()
        self.update()

    def set_run(self, taken: int | None, index: int | None = None) -> None:
        self.taken, self.run_index = taken, index
        self.update()

    def crossed(self) -> int:
        """Slices above the safe height."""
        if not self.safe:
            return 0
        return sum(1 for z in self.positions if z > self.safe[1] + 0.5)

    def describe(self) -> str:
        if not self.positions:
            return "No slices"
        n = len(self.positions)
        bits = [f"{n} slice{'s' if n != 1 else ''} from {self.positions[0]:,.1f} to {self.positions[-1]:,.1f} µm"]
        if self.focus is not None:
            bits.append(f"focus {self.focus:,.1f} µm")
        if self.nominal is not None and self.positions and \
                self.positions[0] - 1 <= self.nominal <= self.positions[-1] + 1:
            bits.append(f"focus nominal {self.nominal:,.1f} µm")
        if self.safe:
            k = self.crossed()
            bits.append(f"{k} above the {self.safe[0]} safe height ({self.safe[1]:,.1f} µm)" if k else
                        f"below the {self.safe[0]} safe height ({self.safe[1]:,.1f} µm)")
        return "; ".join(bits)

    # ---------------------------------------------------------------- geometry
    def _span(self) -> tuple[float, float] | None:
        vals = [v for v in (*self.positions, self.focus, self.current) if v is not None]
        if not vals:
            return None
        return min(vals), max(vals)

    def _safe_in_view(self) -> bool:
        """The safe height is drawn to scale when it is near the stack; far above, it is named at the top instead (a
        far line would squash the stack to a sliver)."""
        sp = self._span()
        if not self.safe or sp is None:
            return False
        lo, hi = sp
        return self.safe[1] <= hi + max(10.0, (hi - lo) * 1.5)

    def _range(self) -> tuple[float, float] | None:
        sp = self._span()
        if sp is None:
            return None
        lo, hi = sp
        if self._safe_in_view() and self.safe:
            hi = max(hi, self.safe[1])
            lo = min(lo, self.safe[1])
        pad = max(1.0, (hi - lo) * 0.08)
        return lo - pad, hi + pad

    def _col(self) -> tuple[float, float]:
        """The stack column (x0, x1)."""
        return self.AXIS_W + 14, max(self.AXIS_W + 70, self.width() - getattr(self, "_btn_w", self.BTN_W) - 18)

    def _y(self, z: float, rng: tuple[float, float]) -> float:
        lo, hi = rng
        y_bot = self.height() - self.BOTTOM_PAD
        return y_bot - (z - lo) / ((hi - lo) or 1.0) * (y_bot - self.TOP_PAD)

    def _ends(self) -> dict[str, float]:
        pos = self.positions
        centre = pos[len(pos) // 2] if len(pos) % 2 else min(pos, key=lambda v: abs(v - (pos[0] + pos[-1]) / 2))
        return {"top": pos[-1], "centre": centre, "bottom": pos[0]}

    def _place_buttons(self) -> None:
        rng = self._range()
        pos = self.positions
        if rng is None or not pos:
            for b in self._buttons.values():
                b.setVisible(False)
            return
        want = self._ends()
        show = {"top": len(pos) >= 2, "centre": len(pos) != 2, "bottom": len(pos) >= 2}
        bw = int(self.BTN_W)
        for which, b in self._buttons.items():
            b.setText(f"Go to {which}")
            bw = max(bw, b.sizeHint().width())
        self._btn_w = bw
        x = int(self.width() - bw)
        prev_bottom = 2.0
        placed: list[tuple[QPushButton, float]] = []
        for which in ("top", "centre", "bottom"):
            b = self._buttons[which]
            z = want[which]
            b.setText(f"Go to {which}")
            b.setAccessibleName(f"Go to the {which} slice, {z:,.1f} µm")
            b.setToolTip(f"Move the focus to the {which} slice ({z:,.1f} µm) to check it on live view")
            b.resize(bw, max(24, b.sizeHint().height() - 4))
            b.setVisible(show[which])
            if not show[which]:
                continue
            y = max(self._y(z, rng) - b.height() / 2, prev_bottom)
            placed.append((b, y))
            prev_bottom = y + b.height() + 3
        overflow = prev_bottom - (self.height() - self.BOTTOM_PAD + 10)
        for b, y in placed:
            b.move(x, int(y - max(0.0, overflow)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_buttons()

    # ---------------------------------------------------------------- paint
    @staticmethod
    def _mono(base: QFont, delta: int = 0) -> QFont:
        f = QFont(tk.FONT_MONO)
        f.setPixelSize(theme.px("caption") + delta)
        return f

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        cap = _caption_font(self.font())
        p.setFont(cap)
        rng = self._range()
        if rng is None:
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "Focus unknown")
            return
        x0, x1 = self._col()
        xc = (x0 + x1) / 2
        crossed = self.crossed()
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        # axis title
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(0, 0, self.AXIS_W + 40, 18), left, "Z (µm) ↑")
        # travel limits: solid neutral stops
        if self.limits:
            for zl in self.limits:
                if rng[0] <= zl <= rng[1]:
                    yl = self._y(zl, rng)
                    p.setPen(QPen(QColor(theme.BORDER_STRONG), 2))
                    p.drawLine(QPointF(self.AXIS_W, yl), QPointF(x1 + 8, yl))
                    p.setPen(QColor(theme.TEXT_2))
                    p.drawText(QRectF(x0, yl - 16, x1 - x0, 15), left, "travel limit")
        # safe height
        if self.safe:
            safe_text = f"{self.safe[0]} safe height {self.safe[1]:,.1f}"
            if self._safe_in_view():
                ys = self._y(self.safe[1], rng)
                color = QColor(theme.FAULT if crossed else theme.WARN)
                if crossed:
                    band = QColor(theme.FAULT)
                    band.setAlpha(70)
                    p.fillRect(QRectF(self.AXIS_W, 0, x1 + 8 - self.AXIS_W, max(0.0, ys)),
                               QBrush(band, Qt.BrushStyle.BDiagPattern))
                pen = QPen(color, 2 if crossed else 1.5)
                if not crossed:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(self.AXIS_W, ys), QPointF(w - 2, ys))
                p.setPen(color)
                p.drawText(QRectF(x0, ys - 17, w - x0 - 2, 15), right, safe_text)
            else:
                p.setPen(QColor(theme.TEXT_2))
                p.drawText(QRectF(x0, 0, w - x0 - 2, 18), right, f"{safe_text} ↑")
        # the objective under the stack, with its name
        oy = h - self.BOTTOM_PAD + 10
        body = QRectF(xc - 52, oy + 12, 104, h - oy - 14)
        cone = QPainterPath()
        cone.moveTo(xc - 16, oy)
        cone.lineTo(xc + 16, oy)
        cone.lineTo(xc + 40, oy + 12)
        cone.lineTo(xc - 40, oy + 12)
        cone.closeSubpath()
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.setBrush(QColor(theme.PANEL_2))
        p.drawPath(cone)
        p.drawRoundedRect(body, 4, 4)
        if self.objective:
            p.setPen(QColor(theme.TEXT_2))
            p.setFont(self._mono(cap, -1))
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, self.objective)
            p.setFont(cap)
        if not self.positions:
            return
        # the centre line, from the top slice to the objective
        ytop = self._y(self.positions[-1], rng)
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.drawLine(QPointF(xc, ytop - 6), QPointF(xc, oy))
        # slices as stacked planes (a slight perspective, as the kit's ruler), every slice up to 60
        step = max(1, len(self.positions) // 60)
        sk = 10.0
        ph = max(1.5, min(5.0, (self._y(self.positions[0], rng) - ytop) / max(1, len(self.positions)) * 0.45))
        pending = QColor(theme.BORDER_STRONG)
        for i, z in enumerate(self.positions):
            if i % step and i != self.run_index:
                continue
            y = self._y(z, rng)
            if self.safe and z > self.safe[1] + 0.5:
                edge = QColor(theme.FAULT)
            elif self.taken is not None and i == self.run_index:
                edge = QColor(theme.WARN)
            elif self.taken is not None and i >= self.taken:
                edge = pending
            else:
                edge = QColor(theme.ACCENT)
            fill = QColor(edge)
            fill.setAlpha(60 if edge is not pending else 25)
            plate = QPainterPath()
            plate.moveTo(x0 + sk, y - ph)
            plate.lineTo(x1 + sk, y - ph)
            plate.lineTo(x1 - sk, y + ph)
            plate.lineTo(x0 - sk, y + ph)
            plate.closeSubpath()
            edge.setAlpha(170)
            p.setPen(QPen(edge, 1))
            p.setBrush(fill)
            p.drawPath(plate)
        # Z labels on the axis: top, the focus (or centre), bottom
        ends = self._ends()
        axis_x = self.AXIS_W - 4
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.drawLine(QPointF(axis_x, self._y(ends["top"], rng)), QPointF(axis_x, self._y(ends["bottom"], rng)))
        marks = [(ends["top"], "Top"), (ends["bottom"], "Bottom")]
        mid = self.focus if self.focus is not None else ends["centre"]
        marks.insert(1, (mid, "Focus" if self.focus is not None else "Centre"))
        used: list[float] = []
        for z, word in marks:
            y = self._y(z, rng)
            if any(abs(y - u) < 26 for u in used):
                continue
            used.append(y)
            p.drawLine(QPointF(axis_x - 4, y), QPointF(axis_x + 4, y))
            p.setPen(QColor(theme.TEXT))
            p.setFont(self._mono(cap))
            p.drawText(QRectF(0, y - 15, axis_x - 8, 15), right, f"{z:,.2f}")
            p.setFont(cap)
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(QRectF(0, y, axis_x - 8, 14), right, f"({word})")
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        # focus nominal: a tag at its height
        if self.nominal is not None and rng[0] <= self.nominal <= rng[1]:
            yn = self._y(self.nominal, rng)
            d = QPainterPath()
            d.moveTo(x1 + 4, yn - 4)
            d.lineTo(x1 + 8, yn)
            d.lineTo(x1 + 4, yn + 4)
            d.lineTo(x1, yn)
            d.closeSubpath()
            p.setPen(QPen(QColor(theme.VIOLET), 1.4))
            p.setBrush(QColor(theme.INPUT))
            p.drawPath(d)
        # the focus (or, while running, where the drive is now): a line across with a diamond on the centre line
        marker = self.focus
        mcolor = QColor(theme.TEXT)
        if self.taken is not None and self.run_index is not None:
            marker, mcolor = self.positions[min(self.run_index, len(self.positions) - 1)], QColor(theme.WARN)
        if marker is not None and rng[0] <= marker <= rng[1]:
            yf = self._y(marker, rng)
            p.setPen(QPen(mcolor, 2))
            p.drawLine(QPointF(x0 - sk, yf), QPointF(x1 + sk, yf))
            dm = QPainterPath()
            dm.moveTo(xc, yf - 6)
            dm.lineTo(xc + 6, yf)
            dm.lineTo(xc, yf + 6)
            dm.lineTo(xc - 6, yf)
            dm.closeSubpath()
            p.setBrush(QColor(theme.PANEL))
            p.drawPath(dm)
        if self.current is not None and self.taken is None and rng[0] <= self.current <= rng[1]:
            yc = self._y(self.current, rng)
            pen = QPen(QColor(theme.WARN), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(x0 - sk, yc), QPointF(x1 + sk, yc))


# ====================================================================================== focus curve
class FocusCurve(QWidget):
    """Sharpness of every slice against Z (redesign kit's focus curve): a titled card with a legend, a sharpness axis, Z
    ticks with grid lines, the curve with its area and points, the best focus as a filled dot, the focus nominal as a
    diamond, the sharp range (above half height) shaded, and the slice shown as a dashed line. Click to show a slice."""

    picked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = np.zeros(0)
        self.z: list[float] = []
        self.current = 0
        self.live = False
        self.sharp: tuple[float, float] | None = None
        self.nominal: float | None = None
        self.setMinimumHeight(round(120 * min(theme.TEXT_SCALE, 1.4)))
        self.setMaximumHeight(round(200 * min(theme.TEXT_SCALE, 1.4)))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName("Focus curve")
        self.setToolTip("Sharpness of each slice (variance of the Laplacian, relative). Click to show that slice.")

    def set_values(self, values, current: int, z=None, *, live: bool = False) -> None:
        self.values, self.current, self.live = np.asarray(values, float), current, live
        self.sharp = None if live or len(self.values) < 3 else sharp_range(self.values)
        self.z = list(z) if z is not None else []
        if len(self.values):
            b = int(np.argmax(self.values))
            where = f" at {self.z[b]:,.1f} µm" if b < len(self.z) else ""
            self.setAccessibleDescription(f"Sharpness of {len(self.values)} slices; best slice {b + 1}{where}")
        else:
            self.setAccessibleDescription("No stack yet")
        self.update()

    def _plot(self) -> QRectF:
        fh = theme.px("caption") + 6
        return QRectF(self.rect()).adjusted(44, 2 * fh + 10, -16, -(2 * fh + 4))

    def sharp_um(self) -> tuple[float, float] | None:
        """The sharp range (half height) in µm, when the curve crosses half height on both sides."""
        if self.sharp is None or len(self.z) != len(self.values):
            return None
        return z_at(self.sharp[0], self.z), z_at(self.sharp[1], self.z)

    def summary(self) -> str:
        if not len(self.values) or self.live:
            return ""
        b = int(np.argmax(self.values))
        bits = [f"sharpest {self.z[b]:,.1f} µm" if b < len(self.z) else f"sharpest: slice {b + 1}"]
        su = self.sharp_um()
        if su:
            bits.append(f"sharp over ≈ {abs(su[1] - su[0]):.1f} µm (half height)")
        else:
            bits.append("the peak's half height is not reached on both sides")
        return ", ".join(bits)

    def _n(self) -> int:
        return max(len(self.values), len(self.z)) if self.live else len(self.values)

    def _x(self, i: float, r: QRectF) -> float:
        n = self._n()
        return r.left() + (r.width() * i / (n - 1) if n > 1 else r.width() / 2)

    def mousePressEvent(self, e):
        n = len(self.values)
        if n and not self.live:
            r = self._plot()
            i = round((e.position().x() - r.left()) / max(1.0, r.width()) * (n - 1))
            self.picked.emit(int(min(max(i, 0), n - 1)))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cap = _caption_font(self.font())
        fh = theme.px("caption") + 6
        theme.paint_surface(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, inset=True, control=False)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        # title and legend
        tf = QFont(self.font())
        tf.setPixelSize(theme.px("body") + 1)
        tf.setBold(True)
        p.setFont(tf)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(14, 6, 200, fh + 4), left, "Focus curve")
        p.setFont(cap)
        lx = self.width() - 14.0
        for text, kind in (("Nominal plane", "diamond"), ("Best focus", "dot")):
            tw = p.fontMetrics().horizontalAdvance(text)
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(QRectF(lx - tw, 6, tw, fh + 4), right, text)
            cx, cy = lx - tw - 12, 6 + (fh + 4) / 2
            if kind == "dot":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.ACCENT))
                p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
            else:
                self._diamond(p, cx, cy, 5)
            lx = cx - 16
        sub = ("Sharpness, filling in as slices arrive" if self.live else self.summary())
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(14, 6 + fh, self.width() - 28, fh), left, _cap(sub))
        r = self._plot()
        if not len(self.values):
            p.drawText(QRectF(self.rect()).adjusted(0, fh, 0, 0), Qt.AlignmentFlag.AlignCenter,
                       "The focus curve appears after a stack")
            return
        # axes, Z ticks and grid
        grid = QColor(theme.BORDER)
        p.setPen(QPen(grid, 1))
        for k in range(1, 4):
            y = r.bottom() - r.height() * k / 4
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.drawLine(QPointF(r.left(), r.top()), QPointF(r.left(), r.bottom()))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        p.save()
        if p.fontMetrics().horizontalAdvance("Sharpness") + 8 > r.height():
            p.restore()
            p.save()
            p.setOpacity(0.0)
        p.translate(14, r.center().y())
        p.rotate(-90)
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(-r.height() / 2, -2, r.height(), fh), Qt.AlignmentFlag.AlignCenter, "Sharpness")
        p.restore()
        n = self._n()
        if self.z and n > 1:
            mono = QFont(tk.FONT_MONO)
            mono.setPixelSize(theme.px("caption") - 1)
            p.setFont(mono)
            mfm = p.fontMetrics()
            lab_w = mfm.horizontalAdvance(f"{self.z[-1]:,.2f}") + 28
            ticks = max(2, min(n, int(r.width() // lab_w) + 1))
            stride = max(1, math.ceil((n - 1) / (ticks - 1)))
            idxs = list(range(0, n, stride))
            for i in idxs:
                if i >= len(self.z):
                    continue
                x = self._x(i, r)
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
                p.drawLine(QPointF(x, r.bottom()), QPointF(x, r.bottom() + 4))
                p.setPen(QColor(theme.TEXT_2))
                t = f"{self.z[i]:,.2f}"
                tw = mfm.horizontalAdvance(t)
                p.drawText(QRectF(min(max(x - tw / 2, r.left() - 20), r.right() - tw + 10), r.bottom() + 5, tw, fh),
                           Qt.AlignmentFlag.AlignCenter, t)
            p.setFont(cap)
            p.drawText(QRectF(r.left(), r.bottom() + fh + 2, r.width(), fh), Qt.AlignmentFlag.AlignCenter,
                       "Z position (µm)")
        v = self.values
        lo = float(v.min())
        span = float(v.max() - lo) or 1.0

        def pt(i: int) -> QPointF:
            return QPointF(self._x(i, r), r.bottom() - 4 - (v[i] - lo) / span * (r.height() - 10))

        if self.sharp is not None:  # where it stays above half height: a rough depth in focus
            band = QColor(theme.OK)
            band.setAlpha(36)
            xa, xb2 = self._x(self.sharp[0], r), self._x(self.sharp[1], r)
            p.fillRect(QRectF(xa, r.top(), max(1.0, xb2 - xa), r.height()), band)
        path = QPainterPath()
        for i in range(len(v)):
            if i == 0:
                path.moveTo(pt(i))
            else:
                path.lineTo(pt(i))
        area = QPainterPath(path)
        area.lineTo(QPointF(self._x(len(v) - 1, r), r.bottom()))
        area.lineTo(QPointF(self._x(0, r), r.bottom()))
        area.closeSubpath()
        fill = QColor(theme.ACCENT)
        fill.setAlpha(46)
        p.fillPath(area, fill)
        p.strokePath(path, QPen(QColor(theme.ACCENT), 2))
        if len(v) <= 80:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            for i in range(len(v)):
                p.drawEllipse(pt(i), 2.6, 2.6)
        if self.live:
            return
        # the slice shown
        xc = self._x(self.current, r)
        pen = QPen(QColor(theme.TEXT_2), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(xc, r.top()), QPointF(xc, r.bottom()))
        # nominal plane
        if self.nominal is not None and self.z and min(self.z) <= self.nominal <= max(self.z):
            xn = self._x(z_at_index(self.nominal, self.z), r)
            self._diamond(p, xn, r.top() + 8, 5)
        # best focus
        best = int(np.argmax(v))
        b = pt(best)
        p.setPen(QPen(QColor(theme.PANEL), 2))
        p.setBrush(QColor(theme.ACCENT))
        p.drawEllipse(b, 6, 6)
        text = f"{self.z[best]:,.2f} µm" if best < len(self.z) else f"slice {best + 1}"
        tw = p.fontMetrics().horizontalAdvance(text) + 8
        p.setPen(QColor(theme.ACCENT_TX or theme.ACCENT))
        bx = b.x() + 9 if b.x() + 9 + tw < r.right() else b.x() - 9 - tw
        p.drawText(QRectF(bx, b.y() - fh, tw, fh), left, text)

    @staticmethod
    def _diamond(p: QPainter, cx: float, cy: float, s: float) -> None:
        d = QPainterPath()
        d.moveTo(cx, cy - s)
        d.lineTo(cx + s, cy)
        d.lineTo(cx, cy + s)
        d.lineTo(cx - s, cy)
        d.closeSubpath()
        p.setPen(QPen(QColor(theme.VIOLET), 1.5))
        p.setBrush(QColor(theme.INPUT))
        p.drawPath(d)


def z_at_index(z: float, zs: list[float]) -> float:
    """Fractional slice index of height `z` in increasing `zs` (inverse of zviews.z_at)."""
    if len(zs) < 2:
        return 0.0
    for i in range(len(zs) - 1):
        if zs[i] <= z <= zs[i + 1]:
            return i + (z - zs[i]) / ((zs[i + 1] - zs[i]) or 1.0)
    return 0.0 if z < zs[0] else float(len(zs) - 1)


# ====================================================================================== channel rows
class _ChannelRow(QPushButton):
    """One channel as a full-width row: a check box, its display colour, its name, and its filter (Ex / Em) on the
    right (redesign kit's channel list). Checkable; the text keeps a ✓ when on, for screen readers and tests, while
    the row is painted."""

    def __init__(self, name: str, color: str, detail: str, parent=None):
        super().__init__(parent)
        self.name, self.color, self.detail = name, color, detail
        self.setCheckable(True)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(round(30 * min(theme.TEXT_SCALE, 1.5)))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        on = self.isChecked()
        if self.underMouse() or self.hasFocus():
            hov = QColor(theme.ACCENT)
            hov.setAlpha(22)
            p.fillRect(r, hov)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.ACCENT), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)
        cy = r.center().y()
        box = QRectF(10, cy - 8, 16, 16)
        p.setPen(QPen(QColor(theme.ACCENT if on else theme.BORDER_STRONG), 1.5))
        p.setBrush(QColor(theme.ACCENT) if on else QColor(theme.INPUT))
        p.drawRoundedRect(box, 3, 3)
        if on:
            tick = QPainterPath()
            tick.moveTo(box.left() + 3.5, cy)
            tick.lineTo(box.left() + 7, cy + 3.5)
            tick.lineTo(box.right() - 3, cy - 4)
            p.setPen(QPen(QColor(theme.ACCENT_ON or "#ffffff"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(tick)
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.setBrush(QColor(self.color))
        p.drawEllipse(QPointF(42, cy), 5.5, 5.5)
        f = QFont(self.font())
        f.setBold(on)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT if on else theme.TEXT_2))
        p.drawText(QRectF(56, 0, r.width() - 60, r.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.name)
        p.setFont(_caption_font(self.font()))
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(56, 0, r.width() - 66, r.height()),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.detail)


class ChannelChips(QFrame):
    """The channels as a list of rows (kit's channel list: check box, colour, name, Ex / Em); state is the check box and
    weight, never colour alone. The name is kept for the page API (buttons, checked, set_checked)."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("zchannels")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 2, 0, 2)
        self._lay.setSpacing(0)
        self.buttons: dict[str, QPushButton] = {}
        self._items: list[tuple[str, str, str]] = []
        self._details: dict[str, str] = {}
        self.setAccessibleName("Z-stack channels")
        self._restyle()

    def _restyle(self) -> None:
        self.setStyleSheet(f"QFrame#zchannels {{ border: 1px solid {theme.BORDER}; border-radius: 8px; "
                           f"background: {theme.INPUT}; }}")

    def items(self) -> list[tuple[str, str, str]]:
        return list(self._items)

    def set_channels(self, items: list[tuple[str, str, str]], checked: set[str],
                     details: dict[str, str] | None = None) -> None:
        """`items` = [(text, slot, colour)], `details` = {slot: "Ex 385 / Em 445"}; rebuilt only when they change."""
        if items == self._items:
            return
        for b in self.buttons.values():
            self._lay.removeWidget(b)
            b.deleteLater()
        self.buttons.clear()
        self._items = list(items)
        self._details = dict(details or {})
        for i, (text, slot, color) in enumerate(items):
            b = _ChannelRow(text, color, self._details.get(slot, ""))
            b.setChecked(slot in checked)
            b.setAccessibleName(f"{text} channel")
            b.toggled.connect(self._make_toggled(b, text))
            self._label(b, text)
            self.buttons[slot] = b
            if i:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"background: {theme.BORDER}; border: none;")
                self._lay.addWidget(sep)
            self._lay.addWidget(b)

    def _make_toggled(self, b: QPushButton, text: str):
        def toggled(_on: bool) -> None:
            self._label(b, text)
            self.changed.emit()
        return toggled

    @staticmethod
    def _label(b: QPushButton, text: str) -> None:
        b.setText(f"✓ {text}" if b.isChecked() else text)  # state not by colour alone (read by screen readers)
        b.update()

    def slots(self) -> list[str]:
        return [slot for _t, slot, _c in self._items]

    def checked(self) -> list[str]:
        return [slot for _t, slot, _c in self._items if self.buttons[slot].isChecked()]

    def set_checked(self, slot: str, on: bool = True) -> None:
        self.buttons[slot].setChecked(on)


# ====================================================================================== view picker
class ViewPicker(QWidget):
    """Live (the camera) | Slices | Projection, the projection method in a list beside it (the kit's Live / Stack /
    Projection; eight methods would not fit as buttons). One exclusive choice: `current()` is "live", "slice" or a
    projection key from `METHODS`; `pick(key)` chooses it as the operator would."""

    activated = Signal(str)

    def __init__(self, *, accessible_name: str = "", parent=None):
        super().__init__(parent)
        self.live = SegmentedControl([("live", "Live")], accessible_name=f"{accessible_name}: camera")
        self.results = SegmentedControl([("slice", "Slices"), ("proj", "Projection"), ("ortho", "Sections")],
                                        accessible_name=f"{accessible_name}: result")
        self.method = QComboBox()
        self.method.setAccessibleName("Projection method")
        for key, name in METHODS.items():
            self.method.addItem(name, key)
            self.method.setItemData(self.method.count() - 1, VIEW_TIPS.get(key, name), Qt.ItemDataRole.ToolTipRole)
        self.method.setCurrentIndex(max(0, self.method.findData("max")))
        self.method.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.method.setMinimumContentsLength(6)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.live)
        lay.addSpacing(4)
        lay.addWidget(self.results)
        lay.addWidget(self.method)
        lay.addStretch(1)
        self.live.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.results.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.live.buttons["live"].setToolTip(VIEW_TIPS["live"])
        self.results.buttons["slice"].setToolTip(VIEW_TIPS["slice"])
        self.results.buttons["proj"].setToolTip("A projection of the stack; choose the method in the list")
        self.results.buttons["ortho"].setToolTip(VIEW_TIPS["ortho"])
        self.live.activated.connect(self._on_live)
        self.results.activated.connect(self._on_result)
        self.method.currentIndexChanged.connect(self._on_method)

    def _sync_method(self) -> None:
        self.method.setVisible(self.results.current() == "proj")

    def _on_live(self, _key: str) -> None:
        self.results.set_current(None)
        self._sync_method()
        self.activated.emit("live")

    def _on_result(self, key: str) -> None:
        self.live.set_current(None)
        self._sync_method()
        self.activated.emit(self.current() or key)

    def _on_method(self, _i: int) -> None:
        if self.results.current() != "proj":
            self.live.set_current(None)
            self.results.set_current("proj")
        self._sync_method()
        self.activated.emit(self.current() or "max")

    def current(self) -> str | None:
        if self.live.current():
            return "live"
        r = self.results.current()
        if r == "proj":
            return str(self.method.currentData())
        return r

    def set_current(self, key: str | None) -> None:
        if key in METHODS:
            self.method.blockSignals(True)
            self.method.setCurrentIndex(max(0, self.method.findData(key)))
            self.method.blockSignals(False)
            key_r: str | None = "proj"
        else:
            key_r = key if key in ("slice", "ortho") else None
        self.live.set_current("live" if key == "live" else None)
        self.results.set_current(key_r)
        self._sync_method()

    def pick(self, key: str) -> None:
        """Choose a view as the operator would (emits `activated`)."""
        self.set_current(key)
        self.activated.emit(key)

    def set_results_enabled(self, on: bool, reason: str = "") -> None:
        for k in self.results.buttons:
            self.results.set_option_enabled(k, on, reason if not on else "")
        self.method.setEnabled(on)


# ====================================================================================== filmstrip
class Filmstrip(QListWidget):
    """Every planned slice as a thumbnail (the kit's filmstrip). A slice not taken (a stopped stack, or one still
    running) stays an empty frame, so gaps are seen, not compressed away. Clicking a frame shows that slice."""

    picked = Signal(int)
    THUMB = QSize(64, 48)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListWidget.Movement.Static)
        self.setIconSize(self.THUMB)
        self.setSpacing(2)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(self.THUMB.height() + 26)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAccessibleName("Slices filmstrip")
        self.key: tuple | None = None
        self.currentRowChanged.connect(self._row)
        self._quiet = False

    def _row(self, row: int) -> None:
        if not self._quiet and row >= 0 and self.item(row).data(Qt.ItemDataRole.UserRole + 1):
            self.picked.emit(row)

    def _empty(self) -> QIcon:
        pm = QPixmap(self.THUMB)
        pm.fill(QColor(theme.INPUT))
        p = QPainter(pm)
        p.setPen(QPen(QColor(theme.BORDER), 1, Qt.PenStyle.DashLine))
        p.drawRect(0, 0, pm.width() - 1, pm.height() - 1)
        p.end()
        return QIcon(pm)

    def load(self, key: tuple, planned: list[float]) -> None:
        """One empty frame per planned Z."""
        self._quiet = True
        self.clear()
        self.key = key
        empty = self._empty()
        for i, z in enumerate(planned):
            it = QListWidgetItem(empty, f"{i + 1}")
            it.setToolTip(f"Slice {i + 1}, z {z:,.2f} µm: not taken")
            it.setData(Qt.ItemDataRole.UserRole + 1, False)
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.addItem(it)
        self._quiet = False

    def set_thumb(self, i: int, rgb: np.ndarray, z: float) -> None:
        if not 0 <= i < self.count():
            return
        k = max(1, int(np.ceil(max(rgb.shape[0] / self.THUMB.height(), rgb.shape[1] / self.THUMB.width()))))
        small = np.ascontiguousarray(rgb[::k, ::k, :3])
        qi = QImage(small.data, small.shape[1], small.shape[0], small.strides[0], QImage.Format.Format_RGB888).copy()
        it = self.item(i)
        it.setIcon(QIcon(QPixmap.fromImage(qi)))
        it.setToolTip(f"Slice {i + 1}, z {z:,.2f} µm")
        it.setData(Qt.ItemDataRole.UserRole + 1, True)

    def show_row(self, i: int) -> None:
        self._quiet = True
        if 0 <= i < self.count():
            self.setCurrentRow(i)
            self.scrollToItem(self.item(i))
        else:
            self.clearSelection()
        self._quiet = False


# ====================================================================================== page
class ZStackPage(QWidget):
    VIEWS: ClassVar[list[str]] = ["live", "slice", *METHODS]

    def __init__(self, model, capture_page, parent=None):
        super().__init__(parent)
        self.model, self.cap = model, capture_page
        self.session = model.session
        self.zs_settings = self.session.settings.zstack
        self.bottom_um: float | None = None
        self.top_um: float | None = None
        self._proj_cache: dict[tuple, np.ndarray] = {}
        self._curve_cache: dict[tuple, np.ndarray] = {}
        self._run_started: float | None = None
        self._run: dict | None = None  # the running plan: positions, channels (slot, label), order, total
        self._run_curve: list[float] = []
        self._ortho_cache: dict = {}
        self._ortho_layout: OrthoLayout | None = None
        self._ortho_at: tuple[float, float] = (0.5, 0.5)  # the cut, as fractions of width and height
        self._range_key: int | None = None
        self._last_preflight = None
        self._loaded = False  # the widgets hold the saved settings (nothing is saved back before that)
        self._keep_n: int | None = None  # Keep slices: the number asked for
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(self._build_setup())
        viewer = _NoHeightForWidth()
        vl = QVBoxLayout(viewer)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(12)
        vl.addWidget(self._build_viewer(), 1)
        vl.addWidget(self._build_footer())
        root.addWidget(viewer, 1)
        self._style_readouts()

        self._play = QTimer(self)
        self._play.setInterval(125)
        self._play.timeout.connect(self._step_play)
        self._focus_timer = QTimer(self)  # focus readout and the picture follow the drive
        self._focus_timer.setInterval(400)
        self._focus_timer.timeout.connect(self._update_setup)
        for keys, sign in (("Ctrl+Up", +1), ("Ctrl+Down", -1)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self._make_nudge(sign))

        model.buffers_changed.connect(self._refresh)
        model.settings_changed.connect(self._fill_channels)  # the camera may have changed
        model.tasks.busy_changed.connect(self._on_busy)
        model.tasks.progress.connect(self._on_progress)
        model.frame.connect(self._on_frame)
        self._load_settings()
        self._fill_channels()
        self._update_setup()

    # ------------------------------------------------------------------ build: set-up
    def _field(self, text: str, w: QWidget, name: str) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        lab = label(text, "caption")
        lab.setBuddy(w)
        v.addWidget(lab)
        v.addWidget(w)
        w.setAccessibleName(name)
        return box

    def _inline(self, text: str, w: QWidget, name: str) -> QWidget:
        """A label and its field on one line (the kit's "Below [10.00 µm]")."""
        lab = label(text, "caption")
        lab.setBuddy(w)
        w.setAccessibleName(name)
        return hbox(lab, w, spacing=8)

    def _row_button(self, title: str, name: str) -> tuple[QPushButton, QLabel]:
        """A one-line disclosure row: chevron and title left, its current values right."""
        b = QPushButton()
        b.setCheckable(True)
        b.setAccessibleName(name)
        lay = QHBoxLayout(b)
        lay.setContentsMargins(10, 0, 10, 0)
        chev = QLabel("›")
        chev.setStyleSheet("background: transparent; font-weight: 700;")
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; background: transparent;")
        t.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        val = _Elided("", "caption")
        val.setStyleSheet("background: transparent;")
        for x in (chev, t, val):
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(chev)
        lay.addWidget(t)
        lay.addSpacing(6)
        lay.addWidget(val, 1)
        b.toggled.connect(self._make_chevron(chev))
        b.setMinimumHeight(34)
        return b, val

    @staticmethod
    def _make_chevron(chev: QLabel):
        def turn(on: bool) -> None:
            chev.setText("⌄" if on else "›")
        return turn

    def _build_setup(self) -> QWidget:
        f, lay = panel("Stack plan", spacing=10)
        self._setup_panel = f
        width = round(396 * min(theme.TEXT_SCALE, 1.5))
        f.setFixedWidth(width)
        self.mode = SegmentedControl([("centre", "Centre on focus"), ("range", "First and last")],
                                     accessible_name="How to set the range")
        self.mode.activated.connect(self._mode_changed)
        lay.addWidget(self.mode)

        # current focus: readout, nudge down / up, step per press
        lay.addWidget(label("Current focus (Z)", "caption"))
        self.lbl_focus = QLabel("—")
        self.lbl_focus.setObjectName("zfocus")
        self.lbl_focus.setToolTip("Focus position now (µm from home)")
        self.lbl_focus.setAccessibleName("Focus position")
        self.lbl_focus.setMinimumWidth(150)
        self.btn_down = QToolButton()
        self.btn_down.setText("−")
        self.btn_up = QToolButton()
        self.btn_up.setText("+")
        self.nudge = QComboBox()
        for um in NUDGES:
            self.nudge.addItem(f"{um:g} µm", um)
        self.nudge.setCurrentIndex(2)
        self.nudge.setToolTip("How far one − / + press (or Ctrl+Down / Ctrl+Up) moves the focus")
        self.nudge.setAccessibleName("Focus step per press")
        self.nudge.currentIndexChanged.connect(self._name_nudges)
        for b in (self.btn_down, self.btn_up):
            b.setMinimumSize(36, 34)
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.btn_down.clicked.connect(self._make_nudge(-1))
        self.btn_up.clicked.connect(self._make_nudge(+1))
        self._name_nudges()
        lay.addWidget(hbox(self.lbl_focus, None, self.btn_down, self.btn_up, self.nudge, spacing=6))

        # range: centre mode (below / above), or first and last from live focus
        self.centre_box = QWidget()
        cv = QVBoxLayout(self.centre_box)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(4)
        self.above = QDoubleSpinBox(minimum=0, maximum=2000, decimals=2, suffix=" µm")
        self.below = QDoubleSpinBox(minimum=0, maximum=2000, decimals=2, suffix=" µm")
        self.above.setToolTip("Range above the focus (toward the sample)")
        self.below.setToolTip("Range below the focus (away from the sample)")
        cv.addWidget(label("Range around the focus", "caption"))
        cv.addWidget(hbox(self._inline("Below", self.below, "Range below focus"), None,
                          self._inline("Above", self.above, "Range above focus"), spacing=12))
        lay.addWidget(self.centre_box)
        self.range_box = QWidget()
        rg = QGridLayout(self.range_box)
        rg.setContentsMargins(0, 0, 0, 0)
        rg.setHorizontalSpacing(8)
        rg.setVerticalSpacing(6)
        self.lbl_top = label("Not set", "mono")
        self.lbl_bottom = label("Not set", "mono")
        self.btn_set_top = QPushButton("Set &top")
        self.btn_set_top.setToolTip("Focus on the top of the sample on live view, then press (Alt+T)")
        self.btn_set_top.setAccessibleName("Set the last slice (top) from the focus now")
        self.btn_set_bottom = QPushButton("Set &bottom")
        self.btn_set_bottom.setToolTip("Focus on the bottom of the sample on live view, then press (Alt+B)")
        self.btn_set_bottom.setAccessibleName("Set the first slice (bottom) from the focus now")
        self.btn_set_top.clicked.connect(self._set_top)
        self.btn_set_bottom.clicked.connect(self._set_bottom)
        rg.addWidget(label("Last (top)", "caption"), 0, 0)
        rg.addWidget(self.lbl_top, 0, 1)
        rg.addWidget(self.btn_set_top, 0, 2)
        rg.addWidget(label("First (bottom)", "caption"), 1, 0)
        rg.addWidget(self.lbl_bottom, 1, 1)
        rg.addWidget(self.btn_set_bottom, 1, 2)
        rg.setColumnStretch(1, 1)
        lay.addWidget(self.range_box)
        self.lbl_range_hint = label("", "caption", wrap=True)  # kept for older callers; not shown
        self.lbl_range_hint.setVisible(False)

        # sampling: Z interval with the recommended (Nyquist) step; keep interval / slices; the slice count
        self.step = QDoubleSpinBox(minimum=0.1, maximum=500, decimals=2, suffix=" µm")
        self.step.setToolTip("Distance between slices")
        self.btn_optimal = QPushButton("Use Nyquist")
        self.btn_optimal.clicked.connect(self._use_optimal)
        lay.addWidget(hbox(self._inline("Z interval", self.step, "Step between slices"), None, self.btn_optimal,
                           spacing=8))
        self.lbl_optimal = label("", "caption", wrap=True)
        lay.addWidget(self.lbl_optimal)
        self.keep = SegmentedControl([("interval", "Interval"), ("slices", "Slices")],
                                     accessible_name="When the range changes, keep")
        self.keep.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.keep.buttons["interval"].setToolTip("A range change keeps the step and changes the number of slices")
        self.keep.buttons["slices"].setToolTip("A range change keeps the number of slices and changes the step")
        self.keep.activated.connect(self._keep_changed)
        self.slices = QSpinBox(minimum=1, maximum=9999)
        self.slices.setToolTip("Number of slices: changes the step, keeps the range. Centre on focus always includes "
                               "the focus plane, so the count can come out one more or less.")
        self.slices.valueChanged.connect(self._slices_changed)
        lay.addWidget(hbox(label("Keep", "caption"), self.keep, None,
                           self._inline("Slices", self.slices, "Number of slices"), spacing=8))

        # the stack drawn to scale
        self.zview = ZRangeView()
        self.zview.go.connect(self._go)
        self.btn_go_top, self.btn_go_centre, self.btn_go_bottom = (self.zview.btn_top, self.zview.btn_centre,
                                                                    self.zview.btn_bottom)
        lay.addWidget(self.zview)

        # channels
        lay.addWidget(label("Channels", "section"))
        self.chips = ChannelChips()
        self.chips.changed.connect(self._update_setup)
        lay.addWidget(self.chips)
        self.order = QComboBox()
        self.order.addItem("Channel by channel (fewer cube changes)", "channel_first")
        self.order.addItem("All channels at each Z (fewer Z moves)", "z_first")
        self.order.setAccessibleName("Acquisition order")
        self.order.currentIndexChanged.connect(self._on_index_changed)
        self.order_box = self._field("Acquisition order", self.order, "Acquisition order")
        lay.addWidget(self.order_box)

        # advanced acquisition and saving: disclosure rows showing their values
        self.btn_more, self.lbl_more = self._row_button("Advanced acquisition", "Advanced acquisition")
        lay.addWidget(self.btn_more)
        self.more = QWidget()
        mv = QVBoxLayout(self.more)
        mv.setContentsMargins(4, 0, 4, 4)
        mv.setSpacing(8)
        self.chk_light = QCheckBox("Light off between slices")
        self.chk_light.setToolTip("Less light on the sample (bleaching, phototoxicity); each slice takes longer")
        self.settle = QDoubleSpinBox(minimum=0, maximum=2000, decimals=0, suffix=" ms")
        self.settle.setToolTip("Wait after each Z move before exposing (vibration)")
        self.approach = QDoubleSpinBox(minimum=0, maximum=50, decimals=1, suffix=" µm")
        self.approach.setToolTip("The first slice is approached from this far below, like every other slice, so the "
                                 "focus drive's backlash is the same for all slices")
        mv.addWidget(self.chk_light)
        mv.addWidget(hbox(self._field("Settle after each move", self.settle, "Settle after each move"),
                          self._field("Backlash approach", self.approach, "Backlash approach distance"), spacing=8))
        self.more.setVisible(False)
        self.btn_more.toggled.connect(self.more.setVisible)
        lay.addWidget(self.more)
        self.btn_saving, self.lbl_saving = self._row_button("Saving", "Saving options")
        lay.addWidget(self.btn_saving)
        self.saving = QWidget()
        svv = QVBoxLayout(self.saving)
        svv.setContentsMargins(4, 0, 4, 4)
        svv.setSpacing(6)
        self.chk_autosave = QCheckBox("Save each stack automatically")
        self.lbl_folder = label("", "caption", wrap=True)
        self.btn_folder = QPushButton("Change folder…")
        self.btn_folder.clicked.connect(self._choose_folder)
        self.chk_autosave.toggled.connect(self._on_toggled)
        svv.addWidget(self.chk_autosave)
        svv.addWidget(hbox(self.lbl_folder, self.btn_folder, stretch_last=False))
        svv.addWidget(label("Projections to save", "caption"))
        self.proj_checks: dict[str, QCheckBox] = {}
        for k in ("max", "best", "edf"):  # stacked: three in a row clipped at 150 % text size
            chk = QCheckBox(METHODS[k])
            chk.toggled.connect(self._on_toggled)
            self.proj_checks[k] = chk
            svv.addWidget(chk)
        self.saving.setVisible(False)
        self.btn_saving.toggled.connect(self.saving.setVisible)
        lay.addWidget(self.saving)
        lay.addStretch(1)

        for spin in (self.step, self.settle, self.approach):
            spin.valueChanged.connect(self._on_value_changed)
        for spin in (self.below, self.above):
            spin.valueChanged.connect(self._range_changed)
        self.chk_light.toggled.connect(self._on_toggled)

        sa = QScrollArea()
        sa.setWidget(f)
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        col = _NoHeightForWidth()
        cl = QVBoxLayout(col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(sa, 1)
        col.setFixedWidth(width + 14)
        return col

    # ------------------------------------------------------------------ build: pre-flight footer
    def _build_footer(self) -> QWidget:
        """Pre-flight card under the viewer (kit Screen A footer): the verdict and the four numbers, the checks, the
        messages by kind; while running, progress and time left; Pause and Acquire / Stop on the right."""
        foot, fl = panel(None, spacing=6)
        foot.setObjectName("panel")
        top = QHBoxLayout()
        top.setSpacing(14)
        self.lbl_pf_icon = QLabel()
        self.lbl_pf_icon.setFixedSize(30, 30)
        self.lbl_pf_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pf_title = QLabel("Pre-flight")
        self.lbl_pf_title.setObjectName("zpftitle")
        self.lbl_pf_title.setAccessibleName("Pre-flight result")
        top.addWidget(self.lbl_pf_icon)
        top.addWidget(self.lbl_pf_title)
        top.addSpacing(10)
        self.summary = QWidget()
        sgl = QHBoxLayout(self.summary)
        sgl.setContentsMargins(0, 0, 0, 0)
        sgl.setSpacing(18)
        self.fig: dict[str, QLabel] = {}
        for key, title in (("slices", "Slices"), ("time", "Time"), ("size", "Size"), ("light", "Light on")):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(0)
            bl.addWidget(label(title, "caption"))
            val = QLabel("—")
            val.setObjectName("zfig")
            val.setAccessibleName(title)
            self.fig[key] = val
            bl.addWidget(val)
            sgl.addWidget(box)
        top.addWidget(self.summary)
        top.addStretch(1)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setMinimumHeight(44)
        self.btn_pause.setToolTip("Pause after the current slice; the light is off while paused")
        self.btn_pause.clicked.connect(self._pause_or_resume)
        self.btn_pause.setVisible(False)
        self.btn = QPushButton("Acquire Z-stack")
        self.btn.setObjectName("primary")
        self.btn.setMinimumHeight(44)
        self.btn.setMinimumWidth(round(200 * min(theme.TEXT_SCALE, 1.4)))
        self.btn.clicked.connect(self._acquire_or_stop)
        top.addWidget(self.btn_pause)
        top.addWidget(self.btn)
        fl.addLayout(top)
        self._paused = False
        self._paused_since: float | None = None
        self._paused_total = 0.0
        self.lbl_checks = label("", "caption", wrap=True)
        self.lbl_checks.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_checks.setAccessibleName("Pre-flight checks")
        self.lbl_error = label("", "fault", wrap=True)
        self.lbl_warn = label("", "warn", wrap=True)
        self.lbl_note = label("", "muted", wrap=True)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        self.progress.setVisible(False)
        self.progress.setAccessibleName("Images taken")
        self.lbl_progress = label("", "", wrap=True)
        self.lbl_left = label("", "muted", wrap=True)
        self.lbl_after = label("", "muted", wrap=True)
        for fw in (self.lbl_checks, self.lbl_error, self.lbl_warn, self.lbl_note, self.progress, self.lbl_progress,
                   self.lbl_left, self.lbl_after):
            fl.addWidget(fw)
        self.lbl_left.setVisible(False)
        self.lbl_after.setVisible(False)
        holder = _NoHeightForWidth()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(foot)
        return holder

    def _set_verdict(self, state: str, text: str) -> None:
        """The footer's verdict: "ok" (✓ green), "warn" (! amber), "bad" (✗ red), "run" (● accent), "off" (– grey)."""
        mark, color = {"ok": ("✓", theme.OK), "warn": ("!", theme.WARN), "bad": ("✗", theme.FAULT),
                       "run": ("●", theme.ACCENT), "off": ("–", theme.TEXT_2)}[state]
        self.lbl_pf_icon.setText(mark)
        self.lbl_pf_icon.setStyleSheet(f"border-radius: 15px; background: {color}; color: {theme.PANEL};"
                                       f" font-weight: 800; font-size: {theme.px('body') + 3}px;")
        self.lbl_pf_title.setText(text)
        self.lbl_pf_title.setStyleSheet(f"font-size: {theme.px('body') + 3}px; font-weight: 700;")

    # ------------------------------------------------------------------ build: viewer
    def _build_viewer(self) -> QWidget:
        g, gl = panel(None, spacing=8)
        self.view_mode = ViewPicker(accessible_name="Z-stack view")
        self.view_mode.activated.connect(self._view_changed)
        self.view_channel = QComboBox()
        self.view_channel.setToolTip("Channel shown")
        self.view_channel.setAccessibleName("Channel shown")
        self.view_channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.view_channel.setMinimumContentsLength(5)
        self.view_channel.currentIndexChanged.connect(self._on_channel_changed)
        self.btn_save = QPushButton("Save stack…")
        self.btn_save.setToolTip("Save this stack: a folder with an OME-TIFF (every channel, pixel size, each plane's "
                                 "Z), the projections chosen under Saving, and a JSON record")
        self.btn_save.clicked.connect(self._save_ask)
        self.btn_details = QPushButton("Details")
        self.btn_details.setCheckable(True)
        self.btn_details.setToolTip("This stack's settings: range, step, channels, exposure, objective, pixel size, "
                                    "times")
        self.btn_details.toggled.connect(self._on_toggled_details)
        bar = hbox(self.view_mode, None, self.view_channel, self.btn_details, self.btn_save, spacing=6)
        self.view_mode.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        gl.addWidget(bar)
        self.lbl_status = label("", "warn", wrap=True)
        self.lbl_status.setVisible(False)
        gl.addWidget(self.lbl_status)
        self.details = QLabel()
        self.details.setObjectName("caption")
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setAccessibleName("Stack details")
        self.details.setVisible(False)
        gl.addWidget(self.details)
        self.view = ImageView()
        self.view.setAccessibleName("Z-stack image")
        self.view.placeholder = "No stack yet. Set the range on the left, then Acquire Z-stack."
        self.view.clicked.connect(self._on_view_click)
        gl.addWidget(self.view, 1)

        # transport: play, previous / next, the filmstrip, Go to this Z
        self.btn_play = QToolButton()
        self.btn_play.setText("▶")
        self.btn_play.setToolTip("Play through the slices")
        self.btn_play.setAccessibleName("Play through the slices")
        self.btn_play.setCheckable(True)
        self.btn_play.setMinimumSize(40, 36)
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.btn_play.toggled.connect(self._toggle_play)
        self.btn_prev = QToolButton()
        self.btn_prev.setText("‹")
        self.btn_prev.setToolTip("Previous slice")
        self.btn_prev.setAccessibleName("Previous slice")
        self.btn_next = QToolButton()
        self.btn_next.setText("›")
        self.btn_next.setToolTip("Next slice")
        self.btn_next.setAccessibleName("Next slice")
        for b in (self.btn_prev, self.btn_next):
            b.setMinimumSize(28, 36)
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.btn_prev.clicked.connect(self._make_step(-1))
        self.btn_next.clicked.connect(self._make_step(+1))
        self.strip = Filmstrip()
        self.strip.picked.connect(self._pick_slice)
        self.btn_goto = QPushButton("Go to this Z")
        self.btn_goto.setToolTip("Move the focus to this slice's Z")
        self.btn_goto.clicked.connect(self._goto_slice)
        self.transport = QWidget()
        tl = QHBoxLayout(self.transport)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        for tw in (self.btn_play, self.btn_prev, self.strip, self.btn_next, self.btn_goto):
            tl.addWidget(tw, 1 if tw is self.strip else 0)
        self.transport.setVisible(False)
        gl.addWidget(self.transport)

        # the Z ruler: bottom Z, the slider, top Z; what is shown under it (accent)
        self.slice = QSlider(Qt.Orientation.Horizontal)
        self.slice.setRange(0, 0)
        self.slice.setToolTip("Slice (← → or Page Up / Page Down when focused)")
        self.slice.setAccessibleName("Slice")
        self.slice.setMinimumHeight(24)
        self.slice.valueChanged.connect(self._on_slice_changed)
        self.lbl_zlo = label("", "mono")
        self.lbl_zhi = label("", "mono")
        # projection range (Fiji Z Project start / stop slice): view only; Save stack writes whole-stack projections
        self.proj_from = QSpinBox(minimum=1, maximum=1)
        self.proj_to = QSpinBox(minimum=1, maximum=1)
        self.proj_from.setAccessibleName("Project from slice")
        self.proj_to.setAccessibleName("Project to slice")
        for sb in (self.proj_from, self.proj_to):
            sb.setFixedWidth(round(76 * min(theme.TEXT_SCALE, 1.5)))
            sb.setToolTip("Slices included in the projection shown (Save stack… writes whole-stack projections)")
            sb.valueChanged.connect(self._on_range_spin)
        self.lbl_range_to = label("to", "caption")
        self.btn_sharp = QPushButton("Sharp slices")
        self.btn_sharp.setToolTip("Project only the slices where the focus curve stays above half its height")
        self.btn_sharp.clicked.connect(self._use_sharp_range)
        self.btn_all = QPushButton("All")
        self.btn_all.setToolTip("Project every slice")
        self.btn_all.clicked.connect(self._use_all_range)
        self._range_widgets: tuple[QWidget, ...] = (label("Project slices", "caption"), self.proj_from,
                                                    self.lbl_range_to, self.proj_to, self.btn_sharp, self.btn_all)
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(self.lbl_zlo)
        rl.addWidget(self.slice, 1)
        rl.addWidget(self.lbl_zhi)
        self._range_gap = QWidget()
        self._range_gap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._range_gap.setVisible(False)
        rl.addWidget(self._range_gap)
        for w in self._range_widgets:
            rl.addWidget(w)
            w.setVisible(False)
        self._slice_layout = rl
        gl.addWidget(row)
        self.lbl_slice = label("", "zslice")
        self.lbl_slice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_slice.setWordWrap(True)
        gl.addWidget(self.lbl_slice)
        self.curve = FocusCurve()
        self.curve.picked.connect(self._pick_slice)
        gl.addWidget(self.curve)
        return g

    def _make_step(self, d: int):
        def step(_checked: bool = False) -> None:
            if self.slice.isEnabled() and self.slice.maximum() > 0:
                self.slice.setValue(min(max(self.slice.value() + d, 0), self.slice.maximum()))
        return step

    def retint(self) -> None:
        """Colours painted in code follow a theme change."""
        self.chips._restyle()
        self._style_readouts()
        self._update_setup()
        self.update()

    def _style_readouts(self) -> None:
        mono = f"font-family: '{tk.FONT_MONO}', monospace;"
        self.lbl_focus.setStyleSheet(f"{mono} font-size: {theme.px('title') + 5}px; font-weight: 500; "
                                     f"background: {theme.INPUT}; border: 1px solid {theme.BORDER}; "
                                     "border-radius: 6px; padding: 2px 10px;")
        for v in self.fig.values():
            v.setStyleSheet(f"{mono} font-size: {theme.px('body') + 2}px; font-weight: 600;")
        self.lbl_slice.setStyleSheet(f"color: {theme.ACCENT_TX or theme.ACCENT}; font-weight: 600;")

    # ------------------------------------------------------------------ small slots (no tuple lambdas: mypy gate)
    def _make_nudge(self, sign: int):
        def nudge(_checked: bool = False) -> None:
            self._nudge(sign)
        return nudge

    def _name_nudges(self, _i: int = 0) -> None:
        d = self.nudge.currentText()
        self.btn_down.setToolTip(f"Focus down {d}, away from the sample (Ctrl+Down)")
        self.btn_down.setAccessibleName(f"Focus down {d}, away from the sample")
        self.btn_up.setToolTip(f"Focus up {d}, toward the sample (Ctrl+Up)")
        self.btn_up.setAccessibleName(f"Focus up {d}, toward the sample")

    def _on_value_changed(self, _v: float = 0.0) -> None:
        self._update_setup()

    def _on_index_changed(self, _i: int = 0) -> None:
        self._update_setup()

    def _on_toggled(self, _on: bool = False) -> None:
        self._update_setup()

    def _on_channel_changed(self, _i: int = 0) -> None:
        self._show()

    def _on_slice_changed(self, _v: int = 0) -> None:
        if self._run is None:
            self._show()

    def _save_ask(self, _checked: bool = False) -> None:
        self._save(ask=True)

    def _set_top(self, _checked: bool = False) -> None:
        self._set_end("top")

    def _set_bottom(self, _checked: bool = False) -> None:
        self._set_end("bottom")

    def _pick_slice(self, i: int) -> None:
        self.view_mode.set_current("slice")
        self.slice.setValue(i)
        self._show()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> None:
        z = self.zs_settings
        for spin, v in ((self.below, z.below_um), (self.above, z.above_um), (self.step, z.step_um),
                        (self.settle, z.settle_ms), (self.approach, z.approach_um)):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
        self.mode.set_current(z.mode)
        self.keep.set_current(z.keep)
        quiet: tuple[QWidget, ...] = (self.order, self.chk_light, self.chk_autosave)
        for qw in quiet:
            qw.blockSignals(True)
        self.order.setCurrentIndex(max(0, self.order.findData(z.order)))
        self.chk_light.setChecked(z.light_between_slices)
        self.chk_autosave.setChecked(z.auto_save)
        for qw in quiet:
            qw.blockSignals(False)
        for k, c in self.proj_checks.items():
            c.blockSignals(True)
            c.setChecked(k in z.projections)
            c.blockSignals(False)
        self.view_mode.set_current("live")
        self._loaded = True
        self._mode_changed(z.mode, save=False)

    def _save_settings(self) -> None:
        if not self._loaded:
            return
        z = self.zs_settings
        z.mode = self.mode.current() or "centre"
        z.below_um, z.above_um, z.step_um = self.below.value(), self.above.value(), self.step.value()
        z.keep = self.keep.current() or "interval"
        z.settle_ms, z.approach_um = self.settle.value(), self.approach.value()
        z.order = self.order.currentData() or "channel_first"
        z.light_between_slices = self.chk_light.isChecked()
        z.auto_save = self.chk_autosave.isChecked()
        z.projections = [k for k, c in self.proj_checks.items() if c.isChecked()]
        z.channels = self.checked_channels()
        self.lbl_folder.setText(f"Saves to {z.save_folder or default_save_root()}")
        bits = []
        if len(z.channels) > 1:
            bits.append("by channel" if z.order == "channel_first" else "by Z")
        if z.light_between_slices:
            bits.append("light off")
        if z.settle_ms > 0:
            bits.append(f"settle {z.settle_ms:.0f} ms")
        self.lbl_more.setText(", ".join(bits) or "defaults")
        names = [{"max": "Max", "best": "Sharpest", "edf": "EDF"}.get(k, METHODS.get(k, k)) for k in z.projections]
        proj = " + ".join(names) if len(names) < 3 else f"{len(names)} projections"
        self.lbl_saving.setText((f"on, {proj}" if proj else "on") if z.auto_save else "auto-save off")

    # ------------------------------------------------------------------ channels
    def channel_slots(self) -> list[str]:
        """The channels offered (the active camera's installed ones)."""
        return self.chips.slots()

    def checked_channels(self) -> list[str]:
        return self.chips.checked()

    def _fill_channels(self) -> None:
        """Only channels the active camera can image (audit APP-13)."""
        role = self.session.settings.active_camera
        chans = [c for c in self.session.profile.channels_for(role) if c.installed]
        want = [(c.display, c.slot, c.pseudocolor) for c in chans]
        details = {c.slot: (f"Ex {c.excitation_nm} / Em {c.emission_nm}" if c.excitation_nm and c.emission_nm else
                            f"Em {c.emission_nm}" if c.emission_nm else c.kind.value.replace("_", " ").capitalize())
                   for c in chans}
        if want != self.chips.items():
            keep = set(self.checked_channels()) or set(self.zs_settings.channels)
            capture_slot = getattr(self.cap, "channel", None)
            self.chips.blockSignals(True)
            self.chips.set_channels(want, keep, details)
            if not self.checked_channels() and want:
                slots = [s for _t, s, _c in want]
                self.chips.set_checked(capture_slot if isinstance(capture_slot, str) and capture_slot in slots
                                       else slots[0])
            self.chips.blockSignals(False)
            self._chain_chips()
        self._update_setup()

    def _chain_chips(self) -> None:
        """Rebuilt channel rows join the end of the focus chain; put them back in reading order (after the
        drawing)."""
        prev: QWidget = self.btn_go_bottom
        for b in self.chips.buttons.values():
            QWidget.setTabOrder(prev, b)
            prev = b
        QWidget.setTabOrder(prev, self.order)
        QWidget.setTabOrder(self.order, self.btn_more)

    # ------------------------------------------------------------------ set-up state
    def _focus(self) -> float | None:
        return self.session.controller.focus_z()

    def _safe(self):
        return self.session.capture.safe_height_for()

    def _limits(self):
        p = self.session.profile
        return self.session.controller.axis_limits(p.focus_device, "z") if p.focus_device else None

    def _mode_changed(self, key: str, *, save: bool = True) -> None:
        self.centre_box.setVisible(key == "centre")
        self.range_box.setVisible(key == "range")
        if save:
            self._save_settings()
        self._update_setup()

    def _set_end(self, which: str) -> None:
        z = self._focus()
        if z is None:
            self.model.add_log("warn", "Z-stack: focus position unknown")
            return
        if which == "top":
            self.top_um = z
        else:
            self.bottom_um = z
        self._range_changed()

    def plan(self, *, confirmed: bool = False):
        """The plan the page shows now, or raises CaptureRejected with what is missing."""
        from openevos.acquisition.capture import CaptureRejected

        z = self._focus()
        if z is None:
            raise CaptureRejected("focus position unknown, so the stack can't be placed")
        chans = tuple(self.checked_channels())
        return plan_from_settings(self.zs_settings, self.session.settings.active_camera, chans, z,
                                  bottom_um=self.bottom_um, top_um=self.top_um, confirmed_above_safe=confirmed)

    def _span(self) -> float | None:
        """The range the step divides: above + below, or top - bottom."""
        if self.zs_settings.mode == "range":
            if self.top_um is None or self.bottom_um is None:
                return None
            return abs(self.top_um - self.bottom_um)
        return self.above.value() + self.below.value()

    def _keep_changed(self, key: str) -> None:
        self._keep_n = self.slices.value() if key == "slices" else None
        self._save_settings()

    def _range_changed(self, _v: float = 0.0) -> None:
        """Keep slices: a new range re-divides into the same number of slices (ZEN Keep Slice). Keep interval: the
        step stays and the slice count follows (the default)."""
        if (self.keep.current() or "interval") == "slices":
            if self._keep_n is None:
                self._keep_n = self.slices.value()
            # the number asked for, not the count shown: centre mode rounds each side to whole steps, so an
            # asymmetric moment must not wear the target down
            n, span = self._keep_n, self._span()
            if span and n >= 2:
                self.step.blockSignals(True)
                self.step.setValue(max(self.step.minimum(), span / (n - 1)))
                self.step.blockSignals(False)
        self._update_setup()

    def _slices_changed(self, n: int) -> None:
        self._keep_n = n
        span = self._span()
        if not span or n < 2:
            return
        self.step.setValue(max(self.step.minimum(), span / (n - 1)))

    def _update_setup(self) -> None:
        from openevos.acquisition.capture import CaptureRejected

        self._save_settings()
        z = self._focus()
        self.lbl_focus.setText(f"{z:,.2f} µm" if z is not None else "—")
        self.lbl_top.setText(f"{self.top_um:,.2f} µm" if self.top_um is not None else "Not set")
        self.lbl_bottom.setText(f"{self.bottom_um:,.2f} µm" if self.bottom_um is not None else "Not set")
        busy = self.model.tasks.busy
        for b in (self.btn_set_top, self.btn_set_bottom, self.btn_up, self.btn_down):
            b.setEnabled(z is not None and not busy)
        self.zview.nominal = getattr(self.session.vessel, "focus_nominal_um", None)
        if self._run is not None:  # the running plan stays drawn; the focus moves under it
            self.zview.set_state(self._run["positions"], z, self._safe(), self._limits())
            for lab in (self.lbl_error, self.lbl_warn, self.lbl_note, self.lbl_checks):
                lab.setVisible(False)
            return
        opt = self._optimal()
        self.btn_optimal.setEnabled(opt is not None)
        self.btn_optimal.setText(f"Use Nyquist {opt:.2f} µm" if opt else "Use Nyquist")
        self.lbl_optimal.setText("Recommended step from the objective's NA and the shortest emission ticked."
                                 if opt else "The recommended step needs the objective's NA in the profile.")
        obj = self._objective()
        self.zview.objective = ((f"{obj.magnification:g}× / NA {obj.numerical_aperture:g}" if obj.numerical_aperture
                                 else f"{obj.magnification:g}×") if obj is not None and obj.magnification else "")
        self.curve.nominal = self.zview.nominal
        self.btn_optimal.setAccessibleName(f"Use the Nyquist step, {opt:.1f} µm" if opt else "Nyquist step unknown")
        self.btn_optimal.setToolTip(self._optimal_why() if opt else
                                    "The Nyquist step needs the objective's numerical aperture in the instrument "
                                    "profile")
        self.order_box.setVisible(len(self.checked_channels()) > 1)
        errors: list[str] = []
        warn = ""
        notes: list[str] = []
        positions: list[float] = []
        try:
            plan = self.plan()
            pf = preflight(self.session.capture, plan)
            self._last_preflight = pf
            positions = list(pf.positions)
            self.zview.set_state(positions, z, self._safe(), self._limits())
            for key, text in pf.figures(len(plan.channels)).items():
                self.fig[key].setText(text if positions else "—")
            errors += [_cap(e) for e in pf.errors]
            if pf.confirm:
                safe = self._safe()
                k = self.zview.crossed()
                warn = (f"{k} slice{'s' if k != 1 else ''} above the {safe[0]} safe height ({safe[1]:,.1f} µm), where "
                        "the objective nears the vessel. You'll be asked before it runs.")
            notes += [_cap(n) for n in pf.notes]
            storage_ok, storage_text = self._storage_check(pf)
            if not storage_ok:
                errors.append(storage_text)
            self.lbl_checks.setText(self._checklist(pf, plan, storage_ok, storage_text))
            if opt and positions and self.step.value() * OVERSAMPLE_NOTE <= opt:
                span = positions[-1] - positions[0]
                need = math.ceil(span / opt) + 1 if span > 0 else 1
                notes.append(f"Step {self.step.value():.2f} µm is {opt / self.step.value():.0f}× finer than Nyquist "
                             f"for this objective: {len(positions)} slices where {need} would do.")
            elif opt and len(positions) > 1 and self.step.value() > opt * 1.05:  # undersampling (kit ZP-007)
                notes.append(f"Step {self.step.value():.2f} µm is coarser than Nyquist ({opt:.1f} µm) for this "
                             "objective: detail between slices can be missed.")
            ok = pf.ok and storage_ok
        except CaptureRejected as exc:
            self._last_preflight = None
            if self.zs_settings.mode == "centre" and z is not None:
                positions = plan_positions(z - self.below.value(), z + self.above.value(), self.step.value(), z)
            self.zview.set_state(positions, z, self._safe(), self._limits())
            for v in self.fig.values():
                v.setText("—")
            errors.append(_cap(str(exc)))
            self.lbl_checks.setText("")
            ok = False
        if positions:
            self.slices.blockSignals(True)
            self.slices.setValue(len(positions))
            self.slices.blockSignals(False)
        self.lbl_error.setText("\n".join(e if e.endswith(".") else e + "." for e in errors))
        self.lbl_warn.setText(warn)
        self.lbl_note.setText("\n".join(notes))
        running = self._running()
        self.lbl_error.setVisible(bool(errors) and not running)
        self.lbl_warn.setVisible(bool(warn) and not running)
        self.lbl_note.setVisible(bool(notes) and not running)
        self.lbl_checks.setVisible(bool(self.lbl_checks.text()) and not running)
        if not running:
            self._set_verdict(*(("bad", "Check the plan") if errors else
                                ("warn", "Pre-flight: asks first") if warn else ("ok", "Pre-flight passed")))
            self.btn.setEnabled(ok and not busy)
            self.btn.setToolTip("" if ok and not busy else (f"Instrument busy: {self.model.tasks.current}" if busy
                                                             else " ".join(errors)))
        for b in (self.btn_go_top, self.btn_go_centre, self.btn_go_bottom):
            b.setEnabled(bool(self.zview.positions) and not busy)

    def _storage_check(self, pf) -> tuple[bool, str]:
        """Auto-save: is there room in the save folder for the stack, its projections and a reserve (kit ZP-014)?"""
        z = self.zs_settings
        if not z.auto_save:
            return True, "Auto-save off"
        folder = Path(z.save_folder or default_save_root())
        probe = folder
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            return True, "free space unknown"
        per_image = pf.megabytes * 1e6 / max(1, pf.images)
        need = pf.megabytes * 1e6 * 1.05 + per_image * len(z.projections) * 2 + STORAGE_RESERVE
        if free < need:
            return False, (f"Not enough space in {folder}: needs about {_size(need)}, {_size(free)} free. "
                           "Choose another folder under Saving")
        return True, f"{_size(free)} free"

    def _checklist(self, pf, plan, storage_ok: bool, storage_text: str) -> str:
        """The pre-flight checks as one line, each with a mark and a word (not colour alone)."""
        def item(state: str, text: str) -> str:
            mark, color = {"ok": ("✓", theme.OK), "warn": ("!", theme.WARN), "bad": ("✗", theme.FAULT),
                           "off": ("–", theme.TEXT_2)}[state]
            return f'<span style="color:{color}">{mark}</span>&nbsp;{text}'
        limit_err = any("focus limits" in e for e in pf.errors)
        n = len(plan.channels)
        bits = [item("bad" if limit_err else "ok", "Travel"),
                item("warn" if pf.confirm else "ok", "Above safe height" if pf.confirm else "Safe height"),
                item("ok" if n else "bad", f"{n} channel{'s' if n != 1 else ''}" if n else "No channel"),
                item("off" if not self.zs_settings.auto_save else ("ok" if storage_ok else "bad"),
                     storage_text if storage_ok else "Not enough space")]
        return "&nbsp;&nbsp;&nbsp;".join(bits)

    def _objective(self):
        p = self.session.profile
        slot_obj = self.session.controller.objective_slot()
        return p.objective(slot_obj) if slot_obj else None

    def _optimal_parts(self) -> tuple[float, str, int | None] | None:
        """(step, channel label, emission) of the shortest emission among the channels ticked (as ZEN)."""
        obj = self._objective()
        if obj is None:
            return None
        n = {"oil": 1.515, "water": 1.33}.get((obj.immersion or "air").lower(), 1.0)
        best: tuple[float, str, int | None] | None = None
        slots: list[str | None] = list(self.checked_channels()) or [None]
        for slot in slots:
            ch = self.session.profile.channel(slot) if slot else None
            s = optimal_step_um(obj.numerical_aperture, ch.emission_nm if ch else None, n)
            if s and (best is None or s < best[0]):
                best = (s, ch.display if ch else "transmitted light", ch.emission_nm if ch else None)
        return best

    def _optimal(self) -> float | None:
        parts = self._optimal_parts()
        return parts[0] if parts else None

    def _optimal_why(self) -> str:
        parts, obj = self._optimal_parts(), self._objective()
        if not parts or obj is None:
            return ""
        nm = f"{parts[2]} nm" if parts[2] else "550 nm"
        return (f"Axial Nyquist step for {obj.label} (NA {obj.numerical_aperture:g}) at {nm} ({parts[1]}, the shortest "
                "emission ticked), 2× oversampled. Click to use it.")

    def _use_optimal(self, _checked: bool = False) -> None:
        opt = self._optimal()
        if opt:
            self.step.setValue(round(opt, 2))

    # ------------------------------------------------------------------ focus moves (through the safety checks)
    def _nudge(self, sign: int) -> None:
        z = self._focus()
        if z is None or self.model.tasks.busy:
            return
        target = z + sign * float(self.nudge.currentData())
        ctl = self.session.controller
        self.model.submit(lambda: ctl.move_focus(target), "focus")
        QTimer.singleShot(250, self._update_setup)

    def _go(self, which: str) -> None:
        pos = self.zview.positions
        if not pos:
            return
        target = {"top": pos[-1], "bottom": pos[0], "centre": pos[len(pos) // 2]}[which]
        if which == "centre" and len(pos) % 2 == 0:
            target = min(pos, key=lambda v: abs(v - (pos[0] + pos[-1]) / 2))
        ctl = self.session.controller
        self.model.submit(lambda: ctl.move_focus(target), f"go to {which}")
        self.view_mode.set_current("live")
        self._view_changed("live")

    def _goto_slice(self, _checked: bool = False) -> None:
        zs = self._set()
        st = self._lead(zs)
        if st is None:
            return
        i = min(self.slice.value(), len(st.images) - 1)
        if self.view_mode.current() == "best":  # the sharpest within the projection range
            a, b = self._proj_range(len(st.images))
            i = a + int(np.argmax(self._curve_for(zs, st)[a:b + 1]))
        target = st.z_um[i]
        ctl = self.session.controller
        self.model.submit(lambda: ctl.move_focus(target), "go to slice")

    # ------------------------------------------------------------------ run
    def _running(self) -> bool:
        return self.model.tasks.busy and self.model.tasks.current == TASK

    def _acquire_or_stop(self, _checked: bool = False) -> None:
        if self._running():
            if self.session.capture.stop_zstack():
                self.lbl_progress.setText("Stopping after the current slice…")
            return
        self.acquire()

    def _confirm_above_safe(self, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, "Run above the safe height?", text, parent=self)
        run = box.addButton("Run stack", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setInformativeText("Cancel to lower the top instead.")
        box.exec()
        return box.clickedButton() is run

    def acquire(self, *, ask=None) -> bool:
        """Start a stack; `ask(question) -> bool` replaces the confirmation box (tests)."""
        from openevos.acquisition.capture import CaptureRejected

        try:
            plan = self.plan()
        except CaptureRejected as exc:
            self.model.add_log("warn", f"Z-stack: {exc}")
            return False
        pf = preflight(self.session.capture, plan)
        if pf.errors:
            self.model.add_log("warn", "Z-stack: " + "; ".join(pf.errors))
            return False
        if pf.confirm:
            yes = ask(pf.confirm) if ask else self._confirm_above_safe(pf.confirm)
            if not yes:
                return False
            plan = replace(plan, confirmed_above_safe=True)
        cap = self.session.capture
        prof = self.session.profile
        labels = []
        for slot in plan.channels:
            ch = prof.channel(slot)
            labels.append(ch.display if ch else slot)
        self._run = {"positions": list(pf.positions), "channels": list(zip(plan.channels, labels, strict=True)),
                     "order": plan.order, "total": pf.images,
                     "live": self.session.controller.is_live(plan.role)}
        self._run_curve = []
        self._run_started = time.monotonic()
        self._paused, self._paused_since, self._paused_total = False, None, 0.0
        self.progress.setRange(0, max(1, pf.images))
        self.progress.setValue(0)
        disp = self.model.dispatcher

        def on_image(slot, index, done, img) -> None:  # worker thread -> UI thread
            disp(lambda: self._arrived(slot, index, done, img))

        return bool(self.model.submit_when_idle(TASK, lambda p: cap.run_zstack(plan, p, on_image=on_image),
                                                with_progress=True, on_done=self._done, on_error=self._error))

    def _on_busy(self, busy: bool) -> None:
        running = self._running()
        self.btn.setText("Stop (keep slices)" if running else "Acquire Z-stack")
        self.btn.setObjectName("danger" if running else "primary")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.progress.setVisible(running)
        self.btn_pause.setVisible(running and self._run is not None)
        if not running:
            self._paused, self._paused_since, self._paused_total = False, None, 0.0
            self.btn_pause.setText("Pause")
        self.summary.setVisible(not running)
        if running:
            self._set_verdict("run", "Acquiring Z-stack")
        self.lbl_left.setVisible(running)
        self.lbl_after.setVisible(running)
        self._setup_panel.setEnabled(not running)
        self.view_mode.live.set_option_enabled("live", not running, "Live view is paused while the stack runs"
                                               if running else VIEW_TIPS["live"])
        if running:
            self.btn.setEnabled(True)
            self.btn.setToolTip("Stop after the current move or snap; the slices taken are kept")
            self.lbl_progress.setText("Starting…")
            if self._run is not None:  # this page's stack (the Capture page's hot button runs one too)
                self.view_mode.set_results_enabled(True)
                self.view_mode.set_current("slice")
                self.slice.setEnabled(False)
                self.btn_play.setChecked(False)
        self._update_setup()

    def _run_slice_index(self, i: int) -> tuple[int, int, int]:
        """(pass, slice index, slices taken in this pass) for image i of the running plan."""
        run = self._run or {}
        npos = max(1, len(run.get("positions", [])))
        nch = max(1, len(run.get("channels", [])))
        if run.get("order") is ChannelOrder.Z_FIRST:
            idx = min(i // nch, npos - 1)
            return 0, idx, idx
        k = min(i // npos, nch - 1)
        idx = i % npos if i < npos * nch else npos - 1
        return k, idx, (idx if k == 0 else npos)

    def _on_progress(self, name: str, i: int, n: int, msg: str) -> None:
        if name != TASK:
            return
        self.progress.setRange(0, max(1, n))
        self.progress.setValue(i)
        if i >= n:
            return  # the final call carries the result message; _done words it
        if msg.startswith("Paused"):
            self.lbl_progress.setText(f"{msg}. Resume, or Stop to keep the {i} images taken.")
            self.lbl_left.setText(f"{i} of {n} images, {fmt_seconds(self._elapsed())} elapsed, paused")
            return
        if msg == "Resumed":
            return
        elapsed = self._elapsed()
        left = ""
        if i > 0:
            left = f", about {max(1, round(elapsed / i * (n - i)))} s left"
        self.lbl_progress.setText(msg)
        self.lbl_left.setText(f"{i} of {n} images, {fmt_seconds(elapsed)} elapsed{left}")
        if self._run is None:
            return  # a stack started elsewhere (the Capture page's hot button)
        run = self._run
        k, idx, taken = self._run_slice_index(i)
        self.zview.set_run(taken, idx)
        after = []
        chans = run.get("channels", [])
        if run.get("order") is not ChannelOrder.Z_FIRST and k + 1 < len(chans):
            rest = [lab for _s, lab in chans[k + 1:]]
            after.append("Then " + (" and ".join(rest) if len(rest) < 3 else ", ".join(rest[:-1]) + " and " + rest[-1])
                         + ", channel by channel.")
        if run.get("live"):
            after.append("Live view is paused until the stack ends.")
        self.lbl_after.setText(" ".join(after))
        self.lbl_after.setVisible(bool(after))

    def _elapsed(self) -> float:
        """Run time so far, pauses excluded (they would inflate the time left)."""
        if not self._run_started:
            return 0.0
        paused = self._paused_total + (time.monotonic() - self._paused_since if self._paused_since else 0.0)
        return max(0.0, time.monotonic() - self._run_started - paused)

    def _pause_or_resume(self, _checked: bool = False) -> None:
        cap = self.session.capture
        if not self._paused:
            if cap.pause_zstack(True):
                self._paused, self._paused_since = True, time.monotonic()
                self.btn_pause.setText("Resume")
                self.btn_pause.setToolTip("Continue from the next slice; the light comes back on")
                self.lbl_progress.setText("Pausing after the current slice…")
        elif cap.pause_zstack(False):
            self._paused_total += time.monotonic() - (self._paused_since or time.monotonic())
            self._paused, self._paused_since = False, None
            self.btn_pause.setText("Pause")
            self.btn_pause.setToolTip("Pause after the current slice; the light is off while paused")
            self.lbl_progress.setText("Resuming…")

    def _arrived(self, slot: str, index: int, done: int, img) -> None:
        """A slice just taken: show it (the viewer was a frozen live frame until the end, design review U4)."""
        run = self._run
        if run is None:
            return
        pos = run["positions"]
        label_ = next((lab for s, lab in run["channels"] if s == slot), slot)
        if run["channels"] and slot == run["channels"][0][0]:
            self._run_curve.append(focus_measure(img.data))
            self.curve.set_values(self._run_curve, len(self._run_curve) - 1, pos, live=True)
        key = ("run", slot)
        if self.strip.key != key:
            self.strip.load(key, pos)
            self.transport.setVisible(True)
            self._z_ends(list(pos))
        self.strip.set_thumb(index, self._thumb_rgb(img.data, slot), pos[index])
        if self.view_mode.current() != "slice" or not self.isVisible():
            return
        self.strip.show_row(index)
        self.view.set_image(render_frame(img.data, slot, self.session.profile, self.session.settings))
        z_obs = img.z_um if img.z_um is not None else pos[index]
        self.view.badge = (f"ACQUIRING {done}/{run['total']} • Z {z_obs:,.2f} µm" +
                           (" • SIMULATED" if img.synthetic else ""))
        self.slice.blockSignals(True)
        self.slice.setRange(0, max(0, len(pos) - 1))
        self.slice.setValue(index)
        self.slice.blockSignals(False)
        self.lbl_slice.setText(f"Slice {index + 1} of {len(pos)}, {label_}, just taken")

    def _done(self, zs) -> None:
        self._run = None
        nimg = sum(len(st.images) for st in zs.stacks)
        total = len(zs.planned_z_um) * max(1, len(zs.plan.channels))
        nch = len(zs.plan.channels)
        if zs.status == "completed":
            n = len(zs.planned_z_um)
            text = (f"Done: {n} slices × {nch} channels in {zs.seconds:.1f} s." if nch > 1 else
                    f"Done: {n} slices in {zs.seconds:.1f} s.")
        elif zs.status == "stopped":
            text = (f"Stopped after {nimg} of {total} images ({zs.seconds:.1f} s). "
                    + ("The images taken are kept." if nimg else "Nothing was taken."))
        else:
            text = f"Failed after {zs.seconds:.1f} s: {zs.message}"
        self.lbl_progress.setText(text)
        self.model.add_log("info" if zs.complete else "warn", f"Z-stack {zs.status}: {zs.message}")
        self.zview.set_run(None)
        self._run = None
        self._proj_cache.clear()
        self._curve_cache.clear()
        self._ortho_cache = {}
        self._range_key = None
        self.view_mode.set_current("slice" if self.zs_settings.view in ("live", "slice") else self.zs_settings.view)
        self._refresh()
        if zs.stacks and self.zs_settings.auto_save:
            self._save(ask=False)

    def _error(self, msg: str) -> None:
        self.zview.set_run(None)
        self._run = None
        self.lbl_progress.setText(f"Not run: {msg}")

    # ------------------------------------------------------------------ viewer
    def _set(self):
        return self.session.capture.buffers.zstack_set

    def _curve_for(self, zs, st) -> np.ndarray:
        key = (id(zs), st.channel)
        if key not in self._curve_cache:
            self._curve_cache[key] = focus_curve([i.data for i in st.images])
        return self._curve_cache[key]

    def _refresh(self) -> None:
        zs = self._set()
        cur = self.view_channel.currentData()
        self.view_channel.blockSignals(True)
        self.view_channel.clear()
        if zs is not None:
            for st in zs.stacks:
                if st.images:
                    self.view_channel.addItem(st.images[0].channel_label, st.channel)
            if len(self._mergeable(zs)) > 1:
                self.view_channel.addItem("Merged", MERGED)
                self.view_channel.setItemData(self.view_channel.count() - 1, "All channels in their colours; "
                                              "transmitted light as a grey base", Qt.ItemDataRole.ToolTipRole)
            i = self.view_channel.findData(cur)
            self.view_channel.setCurrentIndex(max(i, 0))
        self.view_channel.setVisible(self.view_channel.count() > 1)
        self.view_channel.blockSignals(False)
        st = self._lead(zs)
        if self._run is None:
            self.slice.setRange(0, max(0, len(st.images) - 1) if st else 0)
        self._fill_details(zs)
        if zs is not None and not zs.complete:
            self.lbl_status.setText(f"Incomplete stack ({zs.status}): {zs.message}. Showing the slices taken.")
            self.lbl_status.setVisible(True)
        else:
            self.lbl_status.setVisible(False)
        self.btn_save.setEnabled(zs is not None)
        self.view_mode.set_results_enabled(zs is not None or self._run is not None, "No stack yet")
        if zs is not None and self.view_mode.current() == "live" and not self.isVisible():
            self.view_mode.set_current("slice")
        self._show()

    def _view_changed(self, key: str) -> None:
        if key != "live":
            self.zs_settings.view = key
        self._show()

    def _on_frame(self, frame) -> None:
        if self.view_mode.current() != "live" or not self.isVisible():
            return
        if frame.metadata.acquisition_id is not None:
            return
        cam = self.session.profile.camera(self.session.settings.active_camera)
        if cam is None or frame.metadata.device_id != cam.device_id:
            return
        self.view.set_image(render_frame(frame.data, frame.metadata.channel, self.session.profile,
                                         self.session.settings))
        self.view.badge = "LIVE • SIMULATED" if frame.metadata.synthetic else "LIVE"

    def _slice_row(self, *, slider: bool, goto: bool) -> None:
        for w in (self.btn_play, self.btn_prev, self.btn_next, self.slice, self.lbl_zlo, self.lbl_zhi):
            w.setVisible(slider)
        self.btn_goto.setVisible(goto)
        self.transport.setVisible(self.strip.count() > 0 and self.view_mode.current() != "live" and (slider or goto))

    def _z_ends(self, z: list[float]) -> None:
        self.lbl_zlo.setText(f"Z {z[0]:,.2f}" if z else "")
        self.lbl_zhi.setText(f"Z {z[-1]:,.2f} µm" if z else "")

    # ------------------------------------------------------------------ viewer: channels, ranges, sections
    def _mergeable(self, zs) -> list:
        """Stacks that can be merged: taken, and the same frame size as the first."""
        sts = [st for st in zs.stacks if st.images] if zs else []
        return [st for st in sts if st.images[0].data.shape[:2] == sts[0].images[0].data.shape[:2]]

    def _merged(self) -> bool:
        return self.view_channel.currentData() == MERGED

    def _lead(self, zs):
        """The stack that drives the slider, filmstrip and curve: the channel shown, or the first when merged."""
        if zs is None:
            return None
        if self._merged():
            m = self._mergeable(zs)
            return m[0] if m else zs.stack()
        return zs.stack(self.view_channel.currentData())

    def _is_fluo(self, slot: str) -> bool:
        ch = self.session.profile.channel(slot)
        return bool(ch is not None and ch.kind.value == "fluorescence")

    def _proj_range(self, n: int) -> tuple[int, int]:
        """Inclusive 0-based slice range of the projection shown."""
        a, b = self.proj_from.value() - 1, self.proj_to.value() - 1
        a, b = max(0, min(a, n - 1)), max(0, min(b, n - 1))
        return (a, b) if a <= b else (b, a)

    def _sync_range(self, n: int) -> None:
        for sb in (self.proj_from, self.proj_to):
            sb.blockSignals(True)
            sb.setMaximum(max(1, n))
        if self._range_key != n:  # a new stack: every slice
            self.proj_from.setValue(1)
            self.proj_to.setValue(max(1, n))
            self._range_key = n
        for sb in (self.proj_from, self.proj_to):
            sb.blockSignals(False)

    def _on_range_spin(self, _v: int = 0) -> None:
        self._show()

    def _use_all_range(self, _checked: bool = False) -> None:
        self.proj_from.setValue(1)
        self.proj_to.setValue(self.proj_to.maximum())

    def _use_sharp_range(self, _checked: bool = False) -> None:
        zs = self._set()
        st = self._lead(zs)
        if st is None:
            return
        rng = sharp_range(self._curve_for(zs, st))
        if rng is None:
            self.model.add_log("info", "Z-stack: the focus curve does not reach half height on both sides; "
                                       "projecting every slice")
            self._use_all_range()
            return
        n = len(st.images)
        a, b = math.floor(rng[0] + 0.5), math.ceil(rng[1] - 0.5)
        for sb, v in ((self.proj_from, a + 1), (self.proj_to, b + 1)):
            sb.blockSignals(True)
            sb.setValue(int(min(max(v, 1), n)))
            sb.blockSignals(False)
        if self.view_mode.current() not in METHODS:
            self.view_mode.pick("max")
        else:
            self._show()

    def _channel_image(self, zs, st, mode: str, i: int) -> tuple[np.ndarray, int]:
        """Raw data shown for one channel: slice i, or the projection over the chosen range. Returns (data, the slice
        index it stands for, which for "best" is the sharpest in range)."""
        imgs = [im.data for im in st.images]
        if mode not in METHODS:
            return imgs[min(i, len(imgs) - 1)], min(i, len(imgs) - 1)
        a, b = self._proj_range(len(imgs))
        if mode == "best":
            curve = self._curve_for(zs, st)
            k = a + int(np.argmax(curve[a:b + 1]))
            return imgs[k], k
        pk = (id(zs), st.channel, mode, a, b)
        if pk not in self._proj_cache:
            self._proj_cache[pk] = project(imgs[a:b + 1], mode)
        return self._proj_cache[pk], i

    def _render(self, data: np.ndarray, slot: str, mode: str, rng=None) -> np.ndarray:
        top = int(np.nanmax(data)) + 1 if mode in DERIVED else 4095  # derived values: no false saturation marks
        return render_frame(data, slot, self.session.profile, self.session.settings, rng, max_value=top)

    def _ortho_volume(self, zs, sts) -> tuple[np.ndarray, int]:
        """Rendered slices (n, H, W, 3), every slice on one display range per channel so brightness is comparable
        through the stack; downsampled by k so the volume stays under ~160 MB. Cached per stack and channel(s)."""
        key = (id(zs), tuple(st.channel for st in sts), self.session.settings.mono.pseudocolor_enabled)
        if self._ortho_cache.get("key") == key:
            return self._ortho_cache["vol"], self._ortho_cache["k"]
        n = min(len(st.images) for st in sts)
        h, w = sts[0].images[0].data.shape[:2]
        k = max(1, math.ceil(math.sqrt(n * h * w * 3 / 160e6)))
        ranges = {st.channel: auto_range(project([im.data[::k, ::k] for im in st.images[:n]], "max"))
                  for st in sts if st.images[0].data.ndim == 2}
        vol = np.empty((n, len(range(0, h, k)), len(range(0, w, k)), 3), dtype=np.uint8)
        for j in range(n):
            layers = []
            for st in sts:
                d = st.images[j].data[::k, ::k]
                layers.append((render_frame(d, st.channel, self.session.profile, self.session.settings,
                                            ranges.get(st.channel)), self._is_fluo(st.channel)))
            vol[j] = layers[0][0][..., :3] if len(layers) == 1 else merge_rgb(layers)
        self._ortho_cache = {"key": key, "vol": vol, "k": k}
        return vol, k

    def _on_view_click(self, x: float, y: float, _button) -> None:
        """Sections view: a click moves the cut (XY), or picks the slice and moves the cut (XZ, YZ)."""
        lay = self._ortho_layout
        if self.view_mode.current() != "ortho" or lay is None:
            return
        n = self.slice.maximum() + 1
        where = lay.region(x, y)
        if where == "xy":
            self._ortho_at = (x / lay.width, y / lay.height)
        elif where == "xz":
            self._ortho_at = (x / lay.width, self._ortho_at[1])
            self.slice.setValue(lay.slice_at(y - lay.height - lay.gap, n))
        elif where == "yz":
            self._ortho_at = (self._ortho_at[0], y / lay.height)
            self.slice.setValue(lay.slice_at(x - lay.width - lay.gap, n))
        self._show()

    def _show(self) -> None:
        mode = self.view_mode.current() or "slice"
        if self._run is not None and mode == "slice":
            return  # the arriving slices own the view
        zs = self._set()
        is_proj = mode in METHODS
        self.view.set_click_mode(mode == "ortho")
        self._ortho_layout = None
        for w in (*self._range_widgets, self._range_gap):
            w.setVisible(is_proj and zs is not None)
        self.slice.setEnabled(mode in ("slice", "ortho") and self._run is None)
        self.btn_play.setEnabled(mode in ("slice", "ortho") and zs is not None)
        if mode == "live":
            self._slice_row(slider=False, goto=False)
            self.strip.show_row(-1)
            self.lbl_slice.setText("Live view. Focus here, then set the range on the left.")
            self.curve.set_values([], 0)
            self.zview.set_state(self.zview.positions, self._focus(), self._safe(), self._limits())
            return
        st = self._lead(zs)
        if st is None or not st.images:
            self._slice_row(slider=False, goto=False)
            self.lbl_slice.setText("")
            return
        merged = self._merged()
        sts = self._mergeable(zs) if merged else [st]
        n = len(st.images)
        self._sync_range(n)
        curve = self._curve_for(zs, st)
        i = min(self.slice.value(), n - 1)
        skey = (id(zs), st.channel)
        if self.strip.key != skey:  # every planned plane; the ones not taken stay empty frames (kit ZR-002)
            self.strip.load(skey, list(zs.planned_z_um))
            for k, im in enumerate(st.images):
                self.strip.set_thumb(k, self._thumb_rgb(im.data, st.channel), st.z_um[k])
        self._z_ends(list(zs.planned_z_um))
        names = " + ".join(s_.images[0].channel_label for s_ in sts)
        sim = " • SIMULATED" if zs.synthetic else ""
        chan_badge = f"MERGED {names.upper()} • " if merged else ""
        if mode == "ortho":
            vol, k = self._ortho_volume(zs, sts)
            i = min(i, vol.shape[0] - 1)
            fx, fy = self._ortho_at
            px_um = st.images[0].pixel_size_um
            canvas, lay = ortho_composite(vol, int(fx * vol.shape[2]), int(fy * vol.shape[1]), i,
                                          step_um=zs.plan.step_um, pixel_um=px_um * k if px_um else None)
            self._ortho_layout = lay
            self.view.set_image(canvas)
            self.view.pixel_um = px_um * k if px_um else None
            self.view.pixel_calibrated = st.images[0].pixel_size_calibrated
            scale = ("Z NOT TO SCALE (PIXEL SIZE UNKNOWN)" if not lay.to_scale else
                     f"Z STRETCHED ×{lay.stretch:g}" if lay.stretch > 1.05 else "Z TO SCALE")
            self.view.badge = f"{chan_badge}SECTIONS • SLICE {i + 1}/{n} • {scale}{sim}"
            self.lbl_slice.setText(f"XZ below, YZ right; blue = slice {i + 1} (z {st.z_um[i]:,.2f} µm). Click to move "
                                   "the cut.")
            self.strip.show_row(i)
            self._slice_row(slider=True, goto=True)
            self.btn_goto.setEnabled(True)
            self.curve.set_values(curve, i, st.z_um)
            self.zview.set_state(self.zview.positions, self._focus(), self._safe(), self._limits(),
                                 current=st.z_um[i])
            return
        layers = []
        best_bits = []
        rendered: np.ndarray | None = None
        for s_ in sts:
            data, idx = self._channel_image(zs, s_, mode, i)
            if mode == "best":
                best_bits.append(f"{s_.images[0].channel_label} {idx + 1}")
                if s_ is st:
                    i = idx
            rgb = self._render(data, s_.channel, mode)
            layers.append((rgb, self._is_fluo(s_.channel)))
            if not merged:
                rendered = rgb
        img_rgb = merge_rgb(layers) if merged else rendered
        a, b = self._proj_range(n)
        part = f" • SLICES {a + 1}–{b + 1} OF {n}" if (a, b) != (0, n - 1) else f" • {n} SLICES"
        if is_proj:
            if mode == "best":
                if merged:
                    self.lbl_slice.setText("Sharpest plane per channel: " + ", ".join(best_bits))
                else:
                    self.lbl_slice.setText(f"Sharpest plane is slice {i + 1} of {n}, z {st.z_um[i]:,.2f} µm "
                                           "(one whole slice)")
                self.view.badge = f"{chan_badge}SHARPEST PLANE • SLICE {i + 1} OF {n}"
                self.strip.show_row(i)
            else:
                extra = " (32-bit; display stretched)" if mode in DERIVED else ""
                self.lbl_slice.setText(f"{METHODS[mode]}{extra}")
                self.view.badge = f"{chan_badge}{METHODS[mode].upper()}{part}"
                self.strip.show_row(-1)
            self.view.badge += sim
            self._slice_row(slider=False, goto=mode == "best")
            self.btn_goto.setEnabled(mode == "best")
        else:
            obs = st.images[i].z_um
            o = f" (observed {obs:,.2f})" if obs is not None and abs(obs - st.z_um[i]) > 0.05 else ""
            self.lbl_slice.setText(f"Slice {i + 1} of {n}, z {st.z_um[i]:,.2f} µm{o}")
            self.view.badge = f"{chan_badge}SLICE {i + 1}/{n} • Z {st.z_um[i]:,.2f} µm{sim}"
            self.strip.show_row(i)
            self._slice_row(slider=True, goto=True)
            self.btn_goto.setEnabled(True)
        self.view.set_image(img_rgb)
        self.view.pixel_um = st.images[0].pixel_size_um
        self.view.pixel_calibrated = st.images[0].pixel_size_calibrated
        self.curve.set_values(curve, i, st.z_um)
        self.zview.set_state(self.zview.positions, self._focus(), self._safe(), self._limits(), current=st.z_um[i])

    # ------------------------------------------------------------------ viewer: details
    def _on_toggled_details(self, on: bool) -> None:
        self.details.setVisible(on and self._set() is not None)

    def _fill_details(self, zs) -> None:
        """The stack's own settings, read from the result (not from the set-up, which may have changed since)."""
        self.btn_details.setEnabled(zs is not None)
        if zs is None:
            self.details.setVisible(False)
            return
        p, first = zs.plan, next((st.images[0] for st in zs.stacks if st.images), None)
        n = len(zs.planned_z_um)
        taken = sum(len(st.images) for st in zs.stacks)
        rows = [("Status", f"{zs.status}, {taken} of {n * max(1, len(p.channels))} images, {zs.seconds:.1f} s"),
                ("Started", zs.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")),
                ("Range", f"{zs.planned_z_um[0]:,.2f} to {zs.planned_z_um[-1]:,.2f} µm, {n} slices, step "
                          f"{p.step_um:g} µm"
                          + (f", centred on {p.centre_um:,.2f}" if p.centre_um is not None else "")),
                ("Order", "channel by channel" if p.order is ChannelOrder.CHANNEL_FIRST else "all channels at each Z")]
        for st in zs.stacks:
            if st.images:
                im = st.images[0]
                rows.append((im.channel_label, (f"{im.exposure_ms:g} ms, gain {im.gain:g}, "
                                                f"light {im.led_percent:g} %, {im.bit_depth}-bit")))
        if first is not None:
            obj = self.session.profile.objective(first.objective_slot) if first.objective_slot else None
            na = f", NA {obj.numerical_aperture:g}" if obj is not None and obj.numerical_aperture else ""
            rows.append(("Objective", (f"{first.magnification:g}×" if first.magnification else "unknown") + na))
            px = (f"{first.pixel_size_um:.3f} µm " + ("(calibrated)" if first.pixel_size_calibrated else "(nominal)")
                  if first.pixel_size_um else "unknown")
            h, w = first.data.shape[:2]
            rows.append(("Pixel", f"{px}, {w} × {h} px, {first.camera.value} camera"))
        rows.append(("Options", f"settle {p.settle_ms:g} ms, approach {p.approach_um:g} µm, light "
                                + ("off" if p.light_between_slices else "on") + " between slices"))
        if zs.synthetic:
            rows.append(("Source", "simulated images"))
        half = (len(rows) + 1) // 2  # two columns, so the image keeps its height
        pairs = [(rows[j], rows[j + half] if j + half < len(rows) else ("", "")) for j in range(half)]

        def cell(k: str, v: str) -> str:
            return f"<td style='padding-right:8px'><b>{k}</b></td><td style='padding-right:24px'>{v}</td>"

        body = "".join(f"<tr>{cell(*left)}{cell(*right)}</tr>" for left, right in pairs)
        self.details.setText(f"<table cellspacing='0' cellpadding='1'>{body}</table>")
        self.details.setVisible(self.btn_details.isChecked())

    def _thumb_rgb(self, data: np.ndarray, slot: str) -> np.ndarray:
        th = Filmstrip.THUMB
        k = max(1, int(np.ceil(max(data.shape[0] / th.height(), data.shape[1] / th.width()))))
        return render_frame(np.asarray(data)[::k, ::k], slot, self.session.profile, self.session.settings)

    def _toggle_play(self, on: bool) -> None:
        self.btn_play.setText("⏸" if on else "▶")
        self.btn_play.setAccessibleName("Pause" if on else "Play through the slices")
        if on:
            self._play.start()
        else:
            self._play.stop()

    def _step_play(self) -> None:
        if self.slice.maximum() <= 0 or self.view_mode.current() != "slice" or self._run is not None:
            self.btn_play.setChecked(False)
            return
        self.slice.setValue((self.slice.value() + 1) % (self.slice.maximum() + 1))

    # ------------------------------------------------------------------ save
    def _choose_folder(self, _checked: bool = False) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save Z-stacks to", self.zs_settings.save_folder or
                                             str(default_save_root()))
        if d:
            self.zs_settings.save_folder = d
            self._save_settings()

    def _save(self, *, ask: bool, root: str | None = None) -> None:
        zs = self._set()
        if zs is None:
            return
        if ask and root is None:
            root = QFileDialog.getExistingDirectory(self, "Save this Z-stack in", self.zs_settings.save_folder or
                                                    str(default_save_root()))
            if not root:
                return
        target = Path(root or self.zs_settings.save_folder or default_save_root())
        projections = tuple(self.zs_settings.projections)
        disp = self.model.dispatcher

        def work() -> None:  # off the UI thread: 200 slices take a while to write
            from openevos.imaging.zstack_io import save_zstack_set

            try:
                folder = save_zstack_set(target, zs, projections=projections)
                disp(lambda: self._saved("info", f"Saved to {folder}"))
            except Exception as exc:  # noqa: BLE001
                msg = f"Z-stack not saved: {exc}"
                disp(lambda: self._saved("error", msg))

        threading.Thread(target=work, name="zstack-save", daemon=True).start()

    def _saved(self, level: str, text: str) -> None:
        self.model.add_log(level, f"Z-stack: {text}" if level == "info" else text)
        prev = self.lbl_progress.text()
        keep = prev.split("\n")[0] if prev.startswith(("Done", "Stopped", "Failed")) else ""
        self.lbl_progress.setText(f"{keep}\n{text}" if keep else text)

    # ------------------------------------------------------------------ Qt
    def showEvent(self, e):
        super().showEvent(e)
        self._focus_timer.start()
        self._fill_channels()
        self._refresh()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._focus_timer.stop()
        self.btn_play.setChecked(False)


__all__ = ["MAX_SLICES", "ChannelChips", "ChannelOrder", "ViewPicker", "ZStackPage"]
