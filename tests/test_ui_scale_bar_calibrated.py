"""The scale bar is drawn only for a pixel size measured on this instrument (Calibration › pixel size); a nominal
camera-pixel / magnification estimate never shows one (operator, 2026-11-18)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openevos.acquisition.capture import CapturedImage
from openevos.instrument.profile import CameraRole
from openevos.ui.widgets.image_view import ImageView


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _img(source: str | None, px: float | None = 0.65) -> CapturedImage:
    extra = (("pixel_size_source", source),) if source else ()
    return CapturedImage(
        data=np.zeros((8, 8), np.uint16), channel="1", channel_label="DAPI", camera=CameraRole.MONO,
        objective_slot=2, magnification=10.0, pixel_size_um=px, stage_xy_um=None, z_um=None, exposure_ms=10.0,
        gain=1.0, led_percent=50.0, synthetic=True, frame_device="sim", frame_index=0, bit_depth=16, extra=extra)


def test_only_a_measured_pixel_size_counts_as_calibrated():
    assert _img("calibrated").pixel_size_calibrated
    assert not _img("nominal (camera pixel / magnification)").pixel_size_calibrated
    assert not _img(None).pixel_size_calibrated
    assert not _img("calibrated", px=None).pixel_size_calibrated


def _white_pixels_near_scale_bar(view: ImageView) -> int:
    view.viewport().repaint()
    shot = view.viewport().grab().toImage()
    w, h = shot.width(), shot.height()
    n = 0
    for x in range(w - 140, w - 20):
        for y in range(h - 26, h - 18):
            c = shot.pixelColor(x, y)
            n += c.red() > 240 and c.green() > 240 and c.blue() > 240
    return n


def test_scale_bar_hidden_for_nominal_and_shown_for_calibrated(app):
    view = ImageView()
    view.resize(640, 480)
    view.show()
    view.set_image(np.zeros((480, 640, 3), np.uint8))
    app.processEvents()
    view.pixel_um = 0.65
    view.pixel_calibrated = False
    assert _white_pixels_near_scale_bar(view) == 0
    view.pixel_calibrated = True
    assert _white_pixels_near_scale_bar(view) > 20
    view.close()
