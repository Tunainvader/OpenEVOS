"""Render the desktop-launcher icon (assets/openevos.ico) from the UI's own objective icon.

    python tools/make_launcher_icon.py
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "src" / "openevos" / "ui" / "assets" / "icons" / "objective.svg"
OUT = ROOT / "assets" / "openevos.ico"


def render(size: int) -> Image.Image:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#151922"))  # design-system chrome (s1), accent ring
    p.setPen(QColor("#6293FF"))
    r = size * 0.04
    p.drawRoundedRect(QRectF(r, r, size - 2 * r, size - 2 * r), size * 0.2, size * 0.2)
    svg = SVG.read_text("utf-8").replace("currentColor", "#98BAFF")
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(size * 0.18, size * 0.18, size * 0.64, size * 0.64))
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data())))


def main() -> int:
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    OUT.parent.mkdir(exist_ok=True)
    big = render(256)
    big.save(OUT, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
