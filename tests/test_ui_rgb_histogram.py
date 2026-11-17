"""Colour camera frames show three R, G, B histogram curves with no level handles (operator, 2026-11-18)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from openevos.ui.widgets.controls import LevelsHistogram


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _hue_pixels(img, hexcol: str) -> int:
    want = QColor(hexcol)
    n = 0
    for x in range(0, img.width(), 2):
        for y in range(0, img.height(), 2):
            c = img.pixelColor(x, y)
            n += abs(c.red() - want.red()) < 40 and abs(c.green() - want.green()) < 40 \
                and abs(c.blue() - want.blue()) < 40
    return n


def test_rgb_mode_draws_three_curves_and_ignores_level_drags(app):
    h = LevelsHistogram()
    h.resize(320, 80)
    seen = []
    h.levelsChanged.connect(lambda lo, hi: seen.append((lo, hi)))
    h.autoRequested.connect(lambda: seen.append("auto"))
    counts = [np.r_[np.zeros(64), np.full(64, 500)], np.full(128, 200), np.r_[np.full(64, 500), np.zeros(64)]]
    h.set_rgb(counts, (0.0, 0.02))
    img = h.grab().toImage()
    for col in LevelsHistogram.RGB_COLORS:
        assert _hue_pixels(img, col) > 10, col
    assert h.rgb is not None and h.counts is None
    # mono data afterwards switches back to one curve with handles
    h.set_data(np.full(128, 10), 4095, 0, 4095, True, "#3E8EED", (0.0, 0.0))
    assert h.rgb is None
    h.set_rgb(counts, (0.0, 0.0))
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(h, Qt.LeftButton, pos=QPoint(100, 30))
    QTest.mouseDClick(h, Qt.LeftButton, pos=QPoint(100, 30))
    assert seen == []
