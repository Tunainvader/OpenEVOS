"""Audit APP-08: hot-pixel calibration and correction work for the mono and the colour camera."""

from __future__ import annotations

import numpy as np
import pytest

from openevos.acquisition import calibration as C
from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.profile import CameraRole


@pytest.mark.parametrize("role", [CameraRole.MONO, CameraRole.COLOR])
def test_find_hot_pixels_runs_for_both_roles(role):
    s = build_sim_session(ThreadClock(), fast=True, cameras=None, illumination=None)
    try:
        r = C.find_hot_pixels(s.controller, role, frames=2, exposure_ms=5)
        assert isinstance(r.defects, list) and r.frames == 2 and r.threshold > 0
    finally:
        s.shutdown()


def test_correct_hot_pixels_on_colour_frames():
    img = np.full((20, 20, 3), 100, np.uint8)
    img[5, 7] = (255, 100, 100)  # hot in red only
    out = C.correct_hot_pixels(img, [(5, 7)])
    assert out.shape == img.shape and out.dtype == img.dtype
    assert tuple(out[5, 7]) == (100, 100, 100) and tuple(img[5, 7]) == (255, 100, 100)  # copy, original untouched
