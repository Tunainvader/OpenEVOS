"""Operator 2026-11-23: "make this pane collapsable, better button and icons, better buttons and status indication of
the lights, quitting app should auto off the lights". Simulated hardware, temporary settings."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock


def pump(app, s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _session(tmp_path):
    return build_sim_session(ThreadClock(), fast=True, fps=30, settings_path=tmp_path / "ui_settings.json",
                             calibration_path=tmp_path / "calibration.json")


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _session(tmp_path)
    w = MainWindow(SessionModel(s))
    w.resize(1600, 960)
    w.show()
    pump(app, 0.3)
    yield app, w, s
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


# ------------------------------------------------------------------ quitting switches the lights off first
def test_shutdown_switches_the_light_off_before_anything_else(tmp_path):
    s = _session(tmp_path)
    s.controller.set_light("trans", 40, True)
    deadline = time.monotonic() + 5
    while not s.rig.light.enabled and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.rig.light.enabled
    order = []
    rec = s.core.devices.get(s.profile.illumination_device)
    real_halt, real_core = rec.backend.halt, s.core.shutdown
    rec.backend.halt = lambda: (order.append("lights off"), real_halt())[-1]
    s.core.shutdown = lambda: (order.append("devices closed"), real_core())[-1]
    s.shutdown()
    assert order[:2] == ["lights off", "devices closed"]
    assert not s.rig.light.enabled


def test_a_failing_light_off_does_not_stop_the_shutdown(tmp_path):
    s = _session(tmp_path)
    rec = s.core.devices.get(s.profile.illumination_device)

    def broken():
        raise RuntimeError("board gone")

    rec.backend.halt = broken
    closed = []
    real_core = s.core.shutdown
    s.core.shutdown = lambda: (closed.append(True), real_core())[-1]
    s.shutdown()
    assert closed == [True]


# ------------------------------------------------------------------ the navigation rail collapses
def test_the_rail_collapses_and_expands_and_remembers(win):
    app, w, s = win
    s.settings.basic.navigation = "expanded"
    w._apply_nav_mode()
    assert w.nav_frame.width() > 100 and w.nav_buttons["capture"].text() == "Capture"
    assert w.btn_rail.text() == "Collapse" and "Ctrl+B" in w.btn_rail.toolTip()
    w.btn_rail.click()
    pump(app, 0.05)
    assert s.settings.basic.navigation == "rail" and w.nav_frame.width() == 52
    assert w.nav_buttons["capture"].text() == "" and w.nav_buttons["capture"].toolTip() == "Capture"
    assert all(not h.isVisible() for h in w.nav_headers) and all(d.isVisible() for d in w.nav_dividers)
    assert w.btn_rail.text() == "" and w.btn_rail.toolTip().startswith("Expand")
    w.run_command("view.nav.toggle")  # Ctrl+B
    pump(app, 0.05)
    assert s.settings.basic.navigation == "expanded" and w.nav_buttons["capture"].text() == "Capture"
    assert all(not d.isVisible() for d in w.nav_dividers)


def test_collapse_from_automatic_keeps_what_is_shown_in_mind(win):
    app, w, s = win
    s.settings.basic.navigation = "auto"
    w.resize(1300, 900)  # automatic: icons only below 1440 px
    pump(app, 0.1)
    assert w.nav_collapsed()
    w.toggle_nav()  # the operator expands it: labels from now on, at any width
    assert s.settings.basic.navigation == "expanded" and not w.nav_collapsed()


# ------------------------------------------------------------------ the viewer toolbar keeps its labels
def test_toolbar_labels_stay_when_they_fit_and_go_under_icons_when_tight(win):
    app, w, _s = win
    cap = w.pages["capture"]
    assert cap.btn_overlay.property("icon_name") == "overlay" and cap.btn_sat.property("icon_name") == "saturation"
    assert all(b.objectName() == "tool" for b in cap.toolbuttons) and len(cap.tooldivs) == 3
    w.resize(2400, 1000)
    pump(app, 0.2)
    wide = cap.toolbar_style
    w.resize(1200, 800)
    pump(app, 0.2)
    narrow = cap.toolbar_style
    order = [Qt.ToolButtonIconOnly, Qt.ToolButtonTextUnderIcon, Qt.ToolButtonTextBesideIcon]
    assert order.index(wide) >= order.index(narrow)
    assert wide != Qt.ToolButtonIconOnly  # a wide window shows the words
    need = sum(b.sizeHint().width() for b in cap.toolbuttons)
    assert need <= cap.toolbar.width()  # whatever was chosen fits


# ------------------------------------------------------------------ the light channels show their state
def test_channel_buttons_show_which_light_is_on(win):
    app, w, s = win
    cap = w.pages["capture"]
    assert not cap.btn_power.isChecked() and cap.btn_power.text() == "Light off"
    assert not any(b.lit for b in cap.ch_buttons.values())
    slot = next(iter(cap.ch_buttons))
    cap.ch_buttons[slot].clicked.emit(slot)
    assert wait_for(app, lambda: s.rig.light.enabled)
    name = s.profile.channel(slot).display
    assert cap.btn_power.text() == f"{name} on" and cap.btn_power.accessibleName() == f"Light: {name} on"
    assert cap.ch_buttons[slot].lit and cap.ch_buttons[slot].accessibleDescription() == "light on"
    assert cap.ch_buttons[slot].color in cap.btn_power.styleSheet()  # the button takes the LED's colour
    assert [k for k, b in cap.ch_buttons.items() if b.lit] == [slot]
    cap.btn_power.setChecked(False)
    assert wait_for(app, lambda: not s.rig.light.enabled)
    assert not cap.ch_buttons[slot].lit and cap.btn_power.styleSheet() == ""


def test_channel_buttons_drop_empty_thumbnail_wells_until_a_capture(win):
    _app, w, s = win
    from openevos.ui.widgets.controls import CHANNEL_HEAD_H, CHANNEL_TALL_H

    cap = w.pages["capture"]
    cap._refresh_thumbnails()
    if not s.capture.buffers.all():
        assert all(b.height() == CHANNEL_HEAD_H for b in cap.ch_buttons.values())
    b = next(iter(cap.ch_buttons.values()))
    b.set_compact(False)
    assert b.height() == CHANNEL_TALL_H
    b.set_compact(True)
    assert b.height() == CHANNEL_HEAD_H
