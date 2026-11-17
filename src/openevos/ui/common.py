"""Small UI helpers shared by pages: panels, labels, display rendering, command coalescing."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from openevos.imaging.composite import (
    adjust_color,
    auto_range,
    gray_rgb,
    highlight_saturated,
    pseudocolor,
    to_8bit,
)
from openevos.instrument.profile import InstrumentProfile
from openevos.instrument.settings import AppSettings


def label(text: str, kind: str = "", wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if kind:
        lab.setObjectName(kind)
    if kind in ("section", "navgroup"):
        from openevos.ui.theme import section_font

        lab.setFont(section_font(lab.font()))
    lab.setWordWrap(wrap)
    return lab


def panel(title: str | None = None, *, sub: bool = False, margins: int = 12, spacing: int = 8) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("subpanel" if sub else "panel")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    if title:
        lay.addWidget(label(title, "title" if not sub else "section"))  # sentence case (UI audit 2026-11-17)
    return f, lay


def hbox(*widgets, spacing: int = 6, stretch_last: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        elif isinstance(x, int):
            lay.addSpacing(x)
        else:
            lay.addWidget(x)
    if stretch_last:
        lay.addStretch(1)
    return w


def kv(key: str, value: str = "—") -> tuple[QWidget, QLabel]:
    v = label(value, "value")
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(1)
    lay.addWidget(label(key, "section"))
    lay.addWidget(v)
    return w, v


class _EmptyHint(QObject):
    """Paints a muted hint in an item view's viewport while the view has no rows."""

    def __init__(self, view, text: str):
        super().__init__(view)
        self.view, self.text = view, text

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QColor, QPainter

        if event.type() == QEvent.Paint and self.view.model() is not None and self.view.model().rowCount() == 0:
            from openevos.ui import theme

            p = QPainter(obj)
            p.setPen(QColor(theme.MUTED))
            p.drawText(obj.rect().adjusted(12, 12, -12, -12), Qt.AlignCenter | Qt.TextWordWrap, self.text)
            p.end()
            return True
        return False


def empty_hint(view, text: str):
    """Show `text` inside an empty QListWidget / QTableWidget (UI audit 2026-11-17: blank panels gave no clue)."""
    view.viewport().installEventFilter(_EmptyHint(view, text))
    return view


def expanding(w: QWidget) -> QWidget:
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    return w


class DisplayState:
    """Display range smoothing for live frames (avoid flicker); never alters raw data."""

    def __init__(self) -> None:
        self.lo: float | None = None
        self.hi: float | None = None
        self.auto = True

    def update(self, raw: np.ndarray) -> tuple[float, float]:
        lo, hi = auto_range(raw, 0.5, 99.8)
        if self.lo is None or not self.auto:
            self.lo, self.hi = (lo, hi) if self.lo is None else (self.lo, self.hi)
        else:
            self.lo = 0.7 * self.lo + 0.3 * lo
            self.hi = 0.7 * self.hi + 0.3 * hi
        return self.lo, self.hi

    def reset(self) -> None:
        self.lo = self.hi = None


def render_frame(raw: np.ndarray, slot: str | None, profile: InstrumentProfile, settings: AppSettings,
                 rng: tuple[float, float] | None = None, *, max_value: int = 4095) -> np.ndarray:
    """Raw camera frame -> RGB8 for display with pseudo colour / colour adjustments / saturation."""
    if raw.ndim == 3:
        c = settings.color
        rgb = adjust_color(raw, c.brightness, c.contrast, c.saturation, c.hue)
        if c.highlight_saturated:
            rgb = highlight_saturated(rgb, raw, 255)
        return rgb
    lo, hi = rng if rng else auto_range(raw)
    g8 = to_8bit(raw, lo, hi)
    ch = profile.channel(slot) if slot else None
    m = settings.mono
    if ch is not None and ch.kind.value == "fluorescence" and m.pseudocolor_enabled:
        override = m.channels.get(ch.slot)
        rgb = pseudocolor(g8, (override.pseudocolor if override and override.pseudocolor else None) or ch.pseudocolor)
    else:
        rgb = gray_rgb(g8)
    if m.highlight_saturated:
        rgb = highlight_saturated(rgb, raw, max_value)
    return rgb


def channel_color(slot: str, profile: InstrumentProfile, settings: AppSettings) -> str:
    ch = profile.channel(slot)
    o = settings.mono.channels.get(slot)
    return (o.pseudocolor if o and o.pseudocolor else None) or (ch.pseudocolor if ch else "#FFFFFF")


def overlay_layers(images, profile: InstrumentProfile, settings: AppSettings, trans_opacity: float = 0.4):
    """Layers for a multichannel overlay: fluorescence in pseudo colour at full weight, transmitted/phase
    channels as a dimmed grey base (so they do not wash out the fluorescence)."""
    from openevos.imaging.composite import Layer

    out = []
    for img in images:
        ch = profile.channel(img.channel)
        fluo = ch is not None and ch.kind.value == "fluorescence"
        out.append(Layer(img.data, channel_color(img.channel, profile, settings) if fluo else "#FFFFFF",
                         opacity=1.0 if fluo else trans_opacity))
    return out


class Coalescer(QObject):
    """Keeps only the latest requested value per key and submits it once the previous command of the
    same *group* (one group per device) has finished. Prevents flooding the router while a slider
    is dragged and avoids self-inflicted resource conflicts (e.g. exposure + gain on one camera)."""

    def __init__(self, parent=None, interval_ms: int = 60):
        super().__init__(parent)
        self._want: dict[str, tuple[str, Callable[[], object]]] = {}
        self._inflight: dict[str, object] = {}
        self._t = QTimer(self)
        self._t.setInterval(interval_ms)
        self._t.timeout.connect(self._pump)
        self._t.start()
        self.on_error: Callable[[str, Exception], None] | None = None

    def request(self, key: str, submit: Callable[[], object], group: str | None = None) -> None:
        self._want[key] = (group or key, submit)
        self._pump()

    def pending(self) -> bool:
        """True while any request is waiting or any submitted command is still running."""
        return bool(self._want) or any(not t.status.is_terminal for t in self._inflight.values() if t is not None)

    def busy(self, group: str) -> bool:
        t = self._inflight.get(group)
        return t is not None and not t.status.is_terminal

    def waiting(self, key: str) -> bool:
        """True while a request for `key` is queued behind the running command of its group."""
        return key in self._want

    def _pump(self) -> None:
        for key in list(self._want):
            group, fn = self._want[key]
            if self.busy(group):
                continue
            del self._want[key]
            try:
                self._inflight[group] = fn()
            except Exception as exc:  # noqa: BLE001
                if self.on_error:
                    self.on_error(key, exc)
