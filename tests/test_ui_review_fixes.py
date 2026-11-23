"""UI review 2026-11-24 (UI UX Pro Max rules): the well map works from the keyboard, empty drop-downs say why, and
a short procedure table needs no inner scroll bar."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from openevos.instrument.vessels import builtin_vessels


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _plate96():
    return next(v for v in builtin_vessels() if v.is_plate and len(v.regions) == 96)


def _key(w, key, text=""):
    w.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text))


def test_selection_map_from_the_keyboard(app):
    from openevos.ui.widgets.vessel_map import VesselMap

    m = VesselMap(selectable=True)
    m.set_vessel(_plate96())
    clicked = []
    m.region_clicked.connect(clicked.append)
    assert m.focusPolicy() == Qt.StrongFocus and m.accessibleName() == "Vessel map"
    _key(m, Qt.Key_Right)
    _key(m, Qt.Key_Down)
    _key(m, Qt.Key_Space, " ")
    assert m.selected == {"B2"} and clicked == ["B2"]
    assert "well B2 selected" in m.accessibleDescription() and "1 selected" in m.accessibleDescription()
    for ch in "D7":
        _key(m, getattr(Qt, f"Key_{ch}"), ch)
    _key(m, Qt.Key_Return, "\r")
    assert m.selected == {"B2", "D7"}
    _key(m, Qt.Key_Up)  # clamps at the edge, never wraps off the plate
    _key(m, Qt.Key_Home)
    _key(m, Qt.Key_Left)
    assert "well A1" in m.accessibleDescription()
    _key(m, Qt.Key_Escape)
    assert m.selected == set() and clicked[-1] == ""


def test_navigation_map_enter_centres_the_stage_like_a_click(app):
    from openevos.ui.widgets.vessel_map import VesselMap

    m = VesselMap()  # interactive, not selectable
    v = _plate96()
    m.set_vessel(v)
    moves = []
    m.move_requested.connect(lambda x, y: moves.append((x, y)))
    _key(m, Qt.Key_Right)  # arrows only move the focus ring: the stage stays put
    assert moves == []
    _key(m, Qt.Key_Return, "\r")
    a2 = next(r for r in v.regions if r.region_id == "A2")
    assert moves == [(a2.cx, a2.cy)]


def test_display_only_map_takes_no_focus(app):
    from openevos.ui.widgets.vessel_map import VesselMap

    m = VesselMap(interactive=False)
    m.set_vessel(_plate96())
    assert m.focusPolicy() == Qt.NoFocus


def test_empty_combo_says_why_and_is_disabled_until_filled(app):
    from PySide6.QtWidgets import QComboBox

    from openevos.ui.common import empty_combo

    c = empty_combo(QComboBox(), "No image yet")
    assert not c.isEnabled() and c.placeholderText() == "No image yet" and c.currentIndex() == -1
    c.addItem("img 1")
    assert c.isEnabled()
    c.clear()
    assert not c.isEnabled()
