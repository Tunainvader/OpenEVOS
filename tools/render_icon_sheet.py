"""Render the SVG icon set to a labelled PNG sheet (needs PySide6; offscreen OK)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main(icon_dir: Path, out: Path) -> int:
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    files = sorted(icon_dir.glob("*.svg"))
    cols, cell = 11, 96
    img = QImage(cols * cell, ((len(files) + cols - 1) // cols) * cell, QImage.Format_RGB32)
    img.fill(QColor("#151922"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    for i, f in enumerate(files):
        x, y = (i % cols) * cell, (i // cols) * cell
        QSvgRenderer(QByteArray(f.read_text().replace("currentColor", "#F3F5F7").encode())).render(
            p, QRectF(x + 30, y + 14, 36, 36))
        p.setPen(QColor("#B4BBC6"))
        p.setFont(QFont("Inter", 8))
        p.drawText(QRectF(x, y + 60, cell, 20), Qt.AlignCenter, f.stem)
    p.end()
    img.save(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
