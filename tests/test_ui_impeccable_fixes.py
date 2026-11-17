"""Impeccable audit fixes (2026-11-18): control boundaries at 3:1, keyboard access to the channel buttons and the
health badge, OS text scaling. Offscreen Qt only."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from openevos.ui import theme
from openevos.ui import tokens as tk


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("t", [tk.DARK, tk.LIGHT], ids=["dark", "light"])
def test_control_edges_use_the_3_to_1_boundary_colour(t):
    assert tk.contrast(t.bdd, t.s1) >= 3.0 and tk.contrast(t.bdd, t.s3) >= 3.0
    qss = theme.build_qss(t)
    field = qss[qss.index("QLineEdit, QSpinBox"):].split("}")[0]
    assert t.bdd in field  # the inset field's shaded edges are the control-border token


def test_channel_button_is_keyboard_operable(app):
    from openevos.ui.widgets.controls import ChannelButton

    b = ChannelButton("cube1", "DAPI", "#4F7CFF")
    got = []
    b.clicked.connect(got.append)
    assert b.focusPolicy() & Qt.TabFocus and b.accessibleName() == "Channel DAPI"
    b.show()
    for key in (Qt.Key_Space, Qt.Key_Return):
        QTest.keyClick(b, key)
    assert got == ["cube1", "cube1"]
    b.setEnabled(False)
    QTest.keyClick(b, Qt.Key_Space)
    assert got == ["cube1", "cube1"]


def test_health_badge_opens_its_list_from_the_keyboard(app):
    from openevos.ui.widgets.status import HealthSummary, StatusPill

    pills = [StatusPill("Stage"), StatusPill("Focus")]
    pills[0].set_state("busy", "MOVING")
    pills[1].set_state("ok", "IDLE")
    h = HealthSummary(pills)
    assert h.state == "busy" and "STAGE" in h.state_text
    assert h.focusPolicy() & Qt.TabFocus
    h.show()
    QTest.keyClick(h, Qt.Key_Return)
    assert h.popup.isVisible()
    h.popup.hide()


def test_type_follows_the_os_text_scale(monkeypatch):
    monkeypatch.setattr(theme, "TEXT_SCALE", 1.5)
    assert theme.px("body") == round(tk.TYPE["body"] * 1.5)
    assert tk.TYPE["caption"] >= 12 and tk.TYPE["section"] >= 12
    assert 1.0 <= theme.os_text_scale() <= 2.25
