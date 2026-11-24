"""Segmented control: a labelled, exclusive choice drawn as one track with the active segment filled.

Styled entirely by the theme QSS (`QFrame#segmented`, `QPushButton#segment`) from the design tokens, so it
follows dark/light switches. Keyboard: one Tab stop (the active segment, or the first enabled one when none is
active), Left/Right moves the choice (WCAG 2.4.3; the Z-stack design review found seven Tab stops in one control).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QPushButton, QSizePolicy


class SegmentedControl(QFrame):
    #: emitted when the operator picks a segment (not when `set_current` is called from code)
    activated = Signal(str)

    def __init__(self, options: list[tuple[str, str]], *, accessible_name: str = "", parent=None):
        """`options` = [(key, text), ...] in display order."""
        super().__init__(parent)
        self.setObjectName("segmented")
        self.setAccessibleName(accessible_name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in options:
            b = QPushButton(text)
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            b.installEventFilter(self)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setAccessibleName(f"{accessible_name}: {text}" if accessible_name else text)
            b.clicked.connect(lambda _=False, k=key: self._picked(k))
            self._group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sync_focus()

    def _sync_focus(self) -> None:
        """Roving Tab stop: only the active segment (or the first enabled one) takes Tab focus."""
        cur = self.current()
        stop = cur if cur is not None and self.buttons[cur].isEnabled() else next(
            (k for k, b in self.buttons.items() if b.isEnabled()), None)
        for k, b in self.buttons.items():
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus if k == stop else Qt.FocusPolicy.ClickFocus)

    def _picked(self, key: str) -> None:
        if not self.buttons[key].isChecked():  # clicked while none was active (non-exclusive for a moment)
            self.buttons[key].setChecked(True)
        self._group.setExclusive(True)
        self._sync_focus()
        self.activated.emit(key)

    def current(self) -> str | None:
        return next((k for k, b in self.buttons.items() if b.isChecked()), None)

    def set_current(self, key: str | None) -> None:
        """Show `key` as active without emitting `activated` (reflecting state, not an operator action). `None` (or a
        key not in this control) shows none active: an exclusive group would refuse to uncheck its last button."""
        self._group.setExclusive(key in self.buttons)
        for k, b in self.buttons.items():
            b.blockSignals(True)
            b.setChecked(k == key)
            b.blockSignals(False)
        self._sync_focus()

    def set_option_enabled(self, key: str, on: bool, reason: str = "") -> None:
        b = self.buttons[key]
        b.setEnabled(on)
        if reason:
            b.setToolTip(reason)
        self._sync_focus()

    def set_option_text(self, key: str, text: str) -> None:
        self.buttons[key].setText(text)

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Type.KeyPress and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._move(1 if e.key() == Qt.Key.Key_Right else -1, obj)
            return True
        return super().eventFilter(obj, e)

    def _move(self, step: int, origin=None) -> None:
        keys = list(self.buttons)
        cur = self.current()
        if cur not in keys:  # nothing active (e.g. the other half of a split picker): start from the focused one
            cur = next((k for k, b in self.buttons.items() if b is origin), keys[0])
            if self.buttons[cur].isEnabled() and origin is not None:
                self.buttons[cur].click()
                return
        i = keys.index(cur)
        while 0 <= i + step < len(keys):
            i += step
            if self.buttons[keys[i]].isEnabled():
                self.buttons[keys[i]].click()
                self.buttons[keys[i]].setFocus()
                break

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.current() in self.buttons:
            self._move(1 if e.key() == Qt.Key.Key_Right else -1)
            return
        super().keyPressEvent(e)
