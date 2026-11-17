"""Segmented control: a labelled, exclusive choice drawn as one track with the active segment filled.

Styled entirely by the theme QSS (`QFrame#segmented`, `QPushButton#segment`) from the design tokens, so it
follows dark/light switches. Keyboard: Tab focuses the active segment, Left/Right moves the choice.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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
            b.setFocusPolicy(Qt.StrongFocus)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setAccessibleName(f"{accessible_name}: {text}" if accessible_name else text)
            b.clicked.connect(lambda _=False, k=key: self._picked(k))
            self._group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _picked(self, key: str) -> None:
        self.activated.emit(key)

    def current(self) -> str | None:
        return next((k for k, b in self.buttons.items() if b.isChecked()), None)

    def set_current(self, key: str | None) -> None:
        """Show `key` as active without emitting `activated` (reflecting state, not an operator action)."""
        for k, b in self.buttons.items():
            b.blockSignals(True)
            b.setChecked(k == key)
            b.blockSignals(False)

    def set_option_enabled(self, key: str, on: bool, reason: str = "") -> None:
        b = self.buttons[key]
        b.setEnabled(on)
        if reason:
            b.setToolTip(reason)

    def set_option_text(self, key: str, text: str) -> None:
        self.buttons[key].setText(text)

    def keyPressEvent(self, e):
        keys = list(self.buttons)
        cur = self.current()
        if e.key() in (Qt.Key_Left, Qt.Key_Right) and cur in keys:
            step = 1 if e.key() == Qt.Key_Right else -1
            i = keys.index(cur)
            while 0 <= i + step < len(keys):
                i += step
                if self.buttons[keys[i]].isEnabled():
                    self.buttons[keys[i]].click()
                    self.buttons[keys[i]].setFocus()
                    break
            return
        super().keyPressEvent(e)
