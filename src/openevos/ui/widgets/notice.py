"""The Capture page's notice line, under the image: why something did not happen, on the page where it happened.

Impeccable critique 2026-11-24 (P1): a refused stage, Z or objective move only reached the session log, which only the
Instrument page shows; a locked stage or a faulted drive showed only in the header pill's tooltip. In a darkened room
the operator watches the image, not the log, and does not hover.

Two kinds of notice:
- a passing one (a refusal, a failed step): shown for NOTICE_S, or until the next one or the close button;
- a standing one (STOP engaged, the stage locked, a drive needing recovery): shown while it holds, no close button,
  with its action when there is one (Release stage lock…). It outranks a passing one, which shows again after it.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton

from openevos.ui import theme
from openevos.ui.icons import icon, pixmap
from openevos.ui.widgets.controls import _mix

#: how long a passing notice stays (s): long enough to read twice
NOTICE_S = 8.0


class NoticeBar(QFrame):
    """A one-line notice: warning icon, the message, an optional action, a close button for passing notices."""

    action_requested = Signal(str)  # the standing notice's action key

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("notice")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # a subclass paints its QSS box only with it
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(8)
        self.lbl_icon = QLabel()
        self.lbl_icon.setFixedSize(18, 18)
        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setMinimumWidth(1)
        self.lbl_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_action = QPushButton()
        self.btn_action.setVisible(False)
        self.btn_action.clicked.connect(lambda: self._action and self.action_requested.emit(self._action[0]))
        self.btn_close = QToolButton()
        self.btn_close.setObjectName("noticeclose")
        self.btn_close.setIconSize(QSize(14, 14))
        self.btn_close.setToolTip("Dismiss")
        self.btn_close.setAccessibleName("Dismiss notice")
        self.btn_close.clicked.connect(self.dismiss)
        lay.addWidget(self.lbl_icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.lbl_text, 1)
        lay.addWidget(self.btn_action, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)
        self._passing: tuple[str, str] | None = None  # (level, text)
        self._standing: tuple[str, str] | None = None
        self._action: tuple[str, str] | None = None  # (key, label) for the standing notice
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self.setVisible(False)
        self.retint()

    # ------------------------------------------------------------------ what is shown
    @property
    def text(self) -> str:
        return self.lbl_text.text() if (self._standing or self._passing) else ""

    @property
    def level(self) -> str:
        shown = self._standing or self._passing
        return shown[0] if shown else ""

    @property
    def standing(self) -> bool:
        return self._standing is not None

    def show_passing(self, level: str, text: str, seconds: float = NOTICE_S) -> None:
        """A refusal or failed step ("warn" / "error"): shown for `seconds`, or until the next notice."""
        self._passing = (level, text)
        self._timer.start(int(seconds * 1000))
        self._render()

    def set_standing(self, level: str | None, text: str = "", action: tuple[str, str] | None = None) -> None:
        """A condition that holds (STOP, a stage lock, a drive fault); None clears it."""
        new = (level, text) if level else None
        new_action = action if level else None
        if (new, new_action) == (self._standing, self._action):
            return
        self._standing, self._action = new, new_action
        self._render()

    def dismiss(self) -> None:
        self._passing = None
        self._timer.stop()
        self._render()

    def _expire(self) -> None:
        self._passing = None
        self._render()

    def _render(self) -> None:
        shown = self._standing or self._passing
        if shown is None:
            self.setVisible(False)
            return
        level, text = shown
        self.lbl_text.setText(text)
        self.lbl_text.setToolTip(text)
        self.setAccessibleName(f"{'Error' if level == 'error' else 'Warning'}: {text}")
        standing = self._standing is not None
        self.btn_close.setVisible(not standing)
        self.btn_action.setVisible(standing and self._action is not None)
        if standing and self._action is not None:
            self.btn_action.setText(self._action[1])
        self.setVisible(True)
        self.retint()

    def retint(self) -> None:
        """Colours from the current theme: a tint of the level's colour, its full colour on the edge and the icon."""
        level = self.level or "warn"
        c = theme.FAULT if level == "error" else theme.WARN
        self.setStyleSheet(f"QFrame#notice {{ background: {_mix(theme.PANEL, c, 0.14).name()}; border: 1px solid {c}; "
                           f"border-radius: 8px; }} QFrame#notice QLabel {{ color: {theme.TEXT}; "
                           "background: transparent; border: none; }")
        self.lbl_icon.setPixmap(pixmap("warning", c, 18))
        self.btn_close.setIcon(icon("close"))
