"""SVG icon set (24 px grid, 1.75 stroke, round caps — the Craftimon toolbar style), tinted per theme."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ICON_DIR = Path(__file__).with_name("assets") / "icons"


def names() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))


@lru_cache(maxsize=512)
def _pixmap(name: str, color: str, size: int):
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    data = (ICON_DIR / f"{name}.svg").read_text("utf-8").replace("currentColor", color)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(data.encode())).render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def icon(name: str, color: str | None = None, size: int = 36):
    """QIcon with normal (text colour), disabled (tertiary) and active/checked (accent text) states."""
    from PySide6.QtGui import QIcon

    from openevos.ui import theme

    ic = QIcon()
    ic.addPixmap(_pixmap(name, color or theme.TEXT_2, size), QIcon.Normal, QIcon.Off)
    ic.addPixmap(_pixmap(name, color or theme.TEXT, size), QIcon.Active, QIcon.Off)
    ic.addPixmap(_pixmap(name, color or theme.ACCENT_TX, size), QIcon.Normal, QIcon.On)
    ic.addPixmap(_pixmap(name, color or theme.ACCENT_TX, size), QIcon.Active, QIcon.On)
    ic.addPixmap(_pixmap(name, theme.MUTED, size), QIcon.Disabled, QIcon.Off)
    return ic


def pixmap(name: str, color: str, size: int = 18):
    return _pixmap(name, color, size)
