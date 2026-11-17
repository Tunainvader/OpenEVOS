"""ParamRow — the Craftimon inspector row: [label | slider + number box | reset].

Conventions (Craftimon Image Adjust / inspector audit):
* every gesture has a slider, a number box and arrow keys; Shift = coarse x10, Alt = fine x0.1;
* double-click the label to reset; drag the label horizontally to scrub;
* the reset button (↺) and a semibold label appear only when the value differs from its default;
* sentence-case labels, units in the number box, tabular numerals; no prose in the row.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from openevos.ui import tokens as tk

MIN_BOX_W = 72  # number boxes never shrink below this, so short values keep one column edge


class _ScrubLabel(QLabel):
    scrub = Signal(float)  # delta in steps
    reset = Signal()

    def __init__(self, text: str):
        super().__init__(text)
        self.setObjectName("rowlabel")
        self.setCursor(Qt.SizeHorCursor)
        self._x: float | None = None

    def mousePressEvent(self, e):
        self._x = e.position().x()

    def mouseMoveEvent(self, e):
        if self._x is not None:
            dx = e.position().x() - self._x
            if abs(dx) >= 3:
                mult = 10.0 if e.modifiers() & Qt.ShiftModifier else 0.1 if e.modifiers() & Qt.AltModifier else 1.0
                self.scrub.emit(round(dx / 3) * mult)
                self._x = e.position().x()

    def mouseReleaseEvent(self, e):
        self._x = None

    def mouseDoubleClickEvent(self, e):
        self.reset.emit()


class ParamRow(QWidget):
    valueChanged = Signal(float)  # while dragging/typing
    committed = Signal(float)  # on release / Enter / step

    def __init__(self, label: str, minimum: float, maximum: float, value: float, *, step: float = 1.0,
                 decimals: int = 0, unit: str = "", default: float | None = None, slider: bool = True,
                 tooltip: str = "", parent=None):
        super().__init__(parent)
        self.minimum, self.maximum, self.step, self.decimals = minimum, maximum, step, decimals
        self.default = value if default is None else default
        self._scale = 10 ** decimals
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(2)
        self.label = _ScrubLabel(label)
        self.label.setFixedWidth(tk.LABEL_W)
        self.label.setToolTip(tooltip or f"{label}: drag to scrub, double-click to reset")
        self.box = QDoubleSpinBox()
        self.box.setRange(minimum, maximum)
        self.box.setDecimals(decimals)
        self.box.setSingleStep(step)
        self.box.setKeyboardTracking(False)
        if unit:
            self.box.setSuffix(f" {unit}")
        # step arrows are hidden by the theme QSS (wheel, arrow keys, slider and scrub adjust the value);
        # setButtonSymbols(NoButtons) is not used: with a stylesheet it renders the text area empty (Qt 6.12)
        self._fit_box()
        self.box.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.reset_btn = QPushButton("↺")
        self.reset_btn.setObjectName("reset")
        self.reset_btn.setToolTip(f"Reset to {self._fmt(self.default)}")
        self.reset_btn.setFixedWidth(20)
        lay.addWidget(self.label, 0, 0)
        if slider:
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(round(minimum * self._scale), round(maximum * self._scale))
            self.slider.setSingleStep(max(1, round(step * self._scale)))
            self.slider.setPageStep(max(1, round(step * self._scale * 10)))
            self.slider.setAccessibleName(label)
            lay.addWidget(self.slider, 0, 1)
        else:
            self.slider = None
            lay.setColumnStretch(1, 1)
        lay.addWidget(self.box, 0, 2)
        lay.addWidget(self.reset_btn, 0, 3)
        lay.setColumnStretch(1, 1)
        self.setFixedHeight(tk.ROW_H)
        self._block = False
        self.set_value(value)
        if self.slider:
            self.slider.installEventFilter(self)
            self.slider.valueChanged.connect(self._from_slider)
            self.slider.sliderReleased.connect(lambda: self.committed.emit(self.value()))
        self.box.valueChanged.connect(self._from_box)
        self.box.editingFinished.connect(lambda: self.committed.emit(self.value()))
        self.reset_btn.clicked.connect(self.reset)
        self.label.reset.connect(self.reset)
        self.label.scrub.connect(lambda d: self._nudge(d * self.step))

    def _fit_box(self) -> None:
        """Width for the widest value this row can show (both range ends, with decimals, sign and unit), in
        the monospaced (tabular) digit font, so no value is ever clipped at any window width."""
        f = QFont(tk.FONT_MONO)
        f.setPixelSize(12)
        fm = QFontMetrics(f)
        texts = [f"{v:.{self.decimals}f}{self.box.suffix()}" for v in (self.minimum, self.maximum)]
        text_w = max(fm.horizontalAdvance(t) for t in texts)
        # 2 x 8 px padding + 2 x 2 px focus border + caret room
        self.box.setFixedWidth(max(MIN_BOX_W, text_w + 16 + 4 + 6))

    def _fmt(self, v: float) -> str:
        return f"{v:.{self.decimals}f}"

    def value(self) -> float:
        return round(self.box.value(), self.decimals)

    def set_value(self, v: float, *, emit: bool = False) -> None:
        v = min(self.maximum, max(self.minimum, float(v)))
        self._block = True
        self.box.setValue(v)
        if self.slider:
            self.slider.setValue(round(v * self._scale))
        self._block = False
        self._mark()
        if emit:
            self.valueChanged.emit(self.value())
            self.committed.emit(self.value())

    def set_range(self, minimum: float, maximum: float) -> None:
        """Change the allowed range (e.g. to a device's capability limits) without emitting a change.
        The current value and the reset default are clamped into the new range."""
        if maximum < minimum:
            raise ValueError(f"empty range {minimum}..{maximum}")
        if (minimum, maximum) == (self.minimum, self.maximum):
            return
        v = self.value()
        self.minimum, self.maximum = minimum, maximum
        self.default = min(maximum, max(minimum, self.default))
        self._block = True
        self.box.setRange(minimum, maximum)
        if self.slider:
            self.slider.setRange(round(minimum * self._scale), round(maximum * self._scale))
        self._block = False
        self._fit_box()
        self.reset_btn.setToolTip(f"Reset to {self._fmt(self.default)}")
        self.set_value(v)

    def reset(self) -> None:
        self.set_value(self.default, emit=True)

    def _nudge(self, delta: float) -> None:
        self.set_value(self.value() + delta, emit=True)

    def _mark(self) -> None:
        changed = abs(self.value() - self.default) > 10 ** -(self.decimals + 1)
        self.reset_btn.setVisible(changed)
        f = self.label.font()
        f.setBold(changed)
        self.label.setFont(f)

    def _from_slider(self, iv: int) -> None:
        if self._block:
            return
        self._block = True
        self.box.setValue(iv / self._scale)
        self._block = False
        self._mark()
        self.valueChanged.emit(self.value())
        if self.slider and not self.slider.isSliderDown():
            self.committed.emit(self.value())

    def _from_box(self, v: float) -> None:
        if self._block:
            return
        self._block = True
        if self.slider:
            self.slider.setValue(round(v * self._scale))
        self._block = False
        self._mark()
        self.valueChanged.emit(self.value())

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent

        if obj is self.slider and e.type() == QEvent.KeyPress and e.key() in (
                Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            self.keyPressEvent(e)
            return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        mult = 10.0 if e.modifiers() & Qt.ShiftModifier else 0.1 if e.modifiers() & Qt.AltModifier else 1.0
        if e.key() in (Qt.Key_Right, Qt.Key_Up):
            self._nudge(self.step * mult)
        elif e.key() in (Qt.Key_Left, Qt.Key_Down):
            self._nudge(-self.step * mult)
        else:
            super().keyPressEvent(e)

    def setEnabled(self, on: bool) -> None:
        super().setEnabled(on)
        self.reset_btn.setVisible(on and abs(self.value() - self.default) > 10 ** -(self.decimals + 1))
