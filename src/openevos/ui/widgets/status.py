"""Status pills and badges: colour + text, never colour alone; label colour computed for contrast."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

from openevos.ui import theme


class StatusPill(QWidget):
    #: the operator clicked (or pressed Enter/Space on) the pill; the shell decides what that offers
    clicked = Signal()

    def mousePressEvent(self, e):
        self.clicked.emit()
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self.focusPolicy() != Qt.NoFocus:
            self.clicked.emit()
            return
        super().keyPressEvent(e)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.state_text = "UNKNOWN"
        self.state = "unknown"
        self.setMinimumHeight(36)
        self.setMinimumWidth(92)
        self.setMaximumWidth(140)
        from PySide6.QtWidgets import QSizePolicy

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setAccessibleName(title)

    def set_state(self, state: str, text: str, tooltip: str = "") -> None:
        self.state, self.state_text = state, text
        self.setToolTip(tooltip)
        self.setAccessibleDescription(f"{self.title}: {text}")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(132, 36)

    def minimumSizeHint(self) -> QSize:
        return QSize(92, 36)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        theme.paint_surface(p, self.rect(), 10, inset=getattr(self, "_down", False), control=self.hasFocus())
        if self.hasFocus():
            p.setPen(QColor(theme.ACCENT))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 9, 9)
        c = QColor(theme.state_color(self.state))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cy = r.height() / 2
        if self.state in ("fault", "warn", "locked"):  # shape differs too, not only colour
            p.drawRect(QRectF(9, cy - 4.5, 9, 9))
        else:
            p.drawEllipse(QRectF(9, cy - 4.5, 9, 9))
        f = QFont(self.font())
        f.setPixelSize(11)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        fm = p.fontMetrics()
        p.drawText(QRectF(26, 3, r.width() - 30, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self.title, Qt.ElideRight, int(r.width() - 30)))
        f.setPixelSize(11)
        f.setWeight(QFont.Medium)
        p.setFont(f)
        p.setPen(c if self.state not in ("unknown", "unavailable") else QColor(theme.TEXT_2))
        p.drawText(QRectF(26, 18, r.width() - 30, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(self.state_text, Qt.ElideRight, int(r.width() - 30)))


class HealthSummary(StatusPill):
    """One header pill for the whole instrument (UI audit 2026-11-17: seven device pills crowded the header). It
    shows the worst device state; clicking it opens the per-device pills as a popup."""

    ORDER = ("fault", "locked", "warn", "busy", "unknown", "simulated", "ok")  # worst first; "unavailable" ignored

    def __init__(self, pills: list[StatusPill], parent=None):
        super().__init__("Instrument", parent)
        from PySide6.QtWidgets import QFrame, QGridLayout

        self.pills = pills
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)  # Tab reaches it; Enter/Space opens the list, Esc closes it
        self.setMaximumWidth(220)
        self.popup = QFrame(self, Qt.Popup)
        self.popup.setObjectName("panel")
        grid = QGridLayout(self.popup)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setSpacing(8)
        for i, pill in enumerate(pills):
            pill.setParent(self.popup)
            pill.setMaximumWidth(170)
            pill.setMinimumWidth(150)
            grid.addWidget(pill, i // 2, i % 2)
        self.refresh()

    def sizeHint(self) -> QSize:
        return QSize(190, 36)

    def refresh(self) -> None:
        live = [p for p in self.pills if p.state != "unavailable"]
        worst = min((p.state for p in live), key=lambda s: self.ORDER.index(s) if s in self.ORDER else 4,
                    default="unknown")
        hit = [p for p in live if p.state == worst]
        if worst in ("fault", "locked", "warn"):
            text = f"{hit[0].title.upper()} {hit[0].state_text}" if len(hit) == 1 else f"{len(hit)} NEED ATTENTION"
        elif worst == "busy":
            text = (f"{hit[0].title.upper()} {hit[0].state_text.replace('SIM · ', '')}" if len(hit) == 1
                    else f"{len(hit)} BUSY")
        elif worst == "simulated":
            text = "SIMULATED · IDLE"
        elif worst == "ok":
            text = "ALL IDLE"
        else:
            text = "UNKNOWN"
        lines = "\n".join(f"{p.title}: {p.state_text}" for p in self.pills)
        self.set_state(worst, text, f"{lines}\n\nClick for the device list")

    def open_list(self) -> None:
        self.popup.adjustSize()
        pos = self.mapToGlobal(self.rect().bottomRight())
        self.popup.move(pos.x() - self.popup.width(), pos.y() + 6)
        self.popup.show()
        self.popup.setFocus()

    def mousePressEvent(self, e):
        self.open_list()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.open_list()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.update()


class Badge(QWidget):
    """Small rounded label, e.g. SIMULATED / NOT CALIBRATED / RECOVERED."""

    def __init__(self, text: str, state: str = "simulated", parent=None):
        super().__init__(parent)
        self.text, self.state = text, state
        self.setFixedHeight(20)
        self.setAccessibleName(text)

    def set(self, text: str, state: str) -> None:
        self.text, self.state = text, state
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(16 + 7 * len(self.text), 20)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = theme.state_color(self.state)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        w = min(self.width(), self.sizeHint().width())
        p.drawRoundedRect(QRectF(0, 0, w, 20), 5, 5)
        f = QFont(self.font())
        f.setPixelSize(11)
        f.setWeight(QFont.DemiBold)
        f.setLetterSpacing(QFont.PercentageSpacing, 104)
        p.setFont(f)
        p.setPen(QColor(theme.on(bg)))
        p.drawText(QRectF(0, 0, w, 20), Qt.AlignCenter, self.text)
