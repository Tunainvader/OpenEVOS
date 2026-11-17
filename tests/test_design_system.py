"""Craftimon design-system rules carried into OpenEVOS: contrast, computed on-accent labels, linear display."""

from __future__ import annotations

import numpy as np
import pytest

from openevos.imaging.display import (
    DisplaySettings,
    NonLinearRefused,
    clipped_fractions,
    clipping_overlay,
    map_to_8bit,
)
from openevos.ui import tokens as tk


@pytest.mark.parametrize("t", [tk.DARK, tk.LIGHT], ids=lambda t: t.name)
def test_text_roles_meet_wcag_aa(t):
    for role in ("tx", "t2", "t3", "acc_tx", "teal", "amber", "violet", "ok", "warn", "danger", "unknown"):
        for surface in ("bg", "s1", "s2"):
            assert tk.contrast(getattr(t, role), getattr(t, surface)) >= 4.5, (t.name, role, surface)
    for surface in ("bg", "s1"):  # non-text UI component boundary (WCAG 1.4.11)
        assert tk.contrast(t.bdd, getattr(t, surface)) >= 3.0


@pytest.mark.parametrize("t", [tk.DARK, tk.LIGHT], ids=lambda t: t.name)
def test_on_accent_label_is_computed(t):
    for c in (t.acc, t.teal, t.amber, t.ok, t.warn, t.danger):
        assert tk.contrast(c, tk.on_color(c)) >= 4.5
    assert tk.DARK.acc_on == "#111111"  # the dark-theme main blue takes a dark label (Craftimon finding)
    assert tk.LIGHT.acc_on == "#FFFFFF"


def test_semantics_do_not_follow_accent_presets():
    t = tk.with_accent(tk.DARK, "#00968F")
    assert t.acc == "#00968F" and t.ok == tk.DARK.ok and t.danger == tk.DARK.danger


def test_every_state_has_text():
    assert all(text for _attr, text in tk.STATES.values())


def test_scientific_display_is_linear_and_logged():
    raw = np.linspace(0, 4095, 4096, dtype=np.float32).reshape(64, 64).astype(np.uint16)
    with pytest.raises(NonLinearRefused):
        DisplaySettings(scientific=True, gamma=0.8)
    ds = DisplaySettings(scientific=False).with_gamma(0.8)
    assert not ds.linear and any("NON-LINEAR" in line for line in ds.log_lines(raw, 12))
    lin = DisplaySettings(auto=False, lo=1000, hi=3000)
    g8 = map_to_8bit(raw, lin)
    assert g8.min() == 0 and g8.max() == 255
    mid = raw == 2000
    assert abs(int(g8[mid][0]) - 128) <= 1  # linear: midpoint maps to mid-grey
    assert lin.log_lines(raw, 12)[0].startswith("Display: linear mapping of raw values 1000–3000 (of 0–4095)")
    lo_f, hi_f = clipped_fractions(raw, lin)
    assert lo_f == pytest.approx(1001 / 4096, abs=0.01) and hi_f == pytest.approx(1096 / 4096, abs=0.01)
    ov = clipping_overlay(np.zeros((64, 64, 3), np.uint8), raw, lin)
    assert tuple(ov[0, 0]) == (64, 128, 255) and tuple(ov[-1, -1]) == (255, 64, 64)
