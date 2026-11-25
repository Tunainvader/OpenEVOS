"""Impeccable critique 2026-11-24, fixes 2-4 on the Capture page (simulated hardware, temporary settings):
- refusals and holds on the page where they happen (a notice line under the image; why focus is held);
- the light's state on the image (badge and hint) and a quieter selected-but-dark channel;
- the objective row high in the Instrument column, with the change's step while it runs."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint

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


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings_path=tmp_path / "ui_settings.json",
                          calibration_path=tmp_path / "calibration.json")
    w = MainWindow(SessionModel(s))
    w.resize(1600, 960)
    w.show()
    pump(app, 0.3)
    yield app, w, s
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


# ------------------------------------------------------------------ fix 2: why something did not happen, here
def test_a_refusal_shows_under_the_image_and_goes(win):
    app, w, _s = win
    cap = w.pages["capture"]
    assert not cap.notice.isVisible()
    w.model.add_log("info", "vessel: 96-well plate")  # information stays in the log
    assert not cap.notice.isVisible()
    w.model.add_log("warn", "Z step refused: no focus readback")
    assert cap.notice.isVisible() and cap.notice.text == "Z step refused: no focus readback"
    assert cap.notice.level == "warn" and cap.notice.btn_close.isVisible()
    assert cap.notice.accessibleName() == "Warning: Z step refused: no focus readback"
    cap.notice.btn_close.click()
    assert not cap.notice.isVisible()
    cap.notice.show_passing("error", "Change to 10X failed: timeout", seconds=0.2)
    assert cap.notice.isVisible()
    assert wait_for(app, lambda: not cap.notice.isVisible(), 3.0)  # a passing notice goes by itself


def test_quick_save_from_the_capture_page_says_it_saved(win, tmp_path):
    """Operator 2026-11-24: "no confirmation for the saved image". Quick Save (hot button, Ctrl+Shift+S, gamepad
    Start) runs from the Capture page; its result only reached the Save page's label and the log, so the operator
    pressed it twice and got two files."""
    app, w, s = win
    cap = w.pages["capture"]
    q = s.settings.camera_settings(s.settings.active_camera).quick_save
    q.folder = str(tmp_path / "caps")
    w.pages["save"].quick_save()
    assert cap.notice.level == "warn" and cap.notice.text == "Quick Save: nothing captured yet"
    cap.notice.dismiss()
    cap.capture_current()
    assert wait_for(app, lambda: s.capture.buffers.all() and not w.model.tasks.busy, 10.0)
    w.pages["save"].quick_save()
    assert list((tmp_path / "caps").glob("Image_0001.*"))
    assert cap.notice.isVisible() and cap.notice.level == "ok"
    assert cap.notice.text == f"Saved Image_0001.tif in {tmp_path / 'caps'}"
    assert cap.notice.accessibleName() == f"Done: Saved Image_0001.tif in {tmp_path / 'caps'}"


def test_the_motor_controller_coming_back_says_focus_again(win):
    """Operator 2026-11-25: the microscope switched off and on with the app running; Z went from 10 um to 0 and the
    camera and light failed, with nothing saying why (ADR-0066)."""
    from types import SimpleNamespace

    from openevos.core.models import Connection, Health

    _app, w, s = win
    cap = w.pages["capture"]
    snap = s.core.store.get(s.profile.stage_device)

    def event(prev, cur):
        return SimpleNamespace(device_id=s.profile.stage_device, reason="telemetry",
                               previous=snap.model_copy(update={"connection": prev}) if prev else None,
                               current=snap.model_copy(update={"connection": cur, "health": Health.NORMAL}))

    w.model._motors_were_connected = False
    w.model._on_device(event(Connection.DISCONNECTED, Connection.CONNECTED))  # launch: the first connect says nothing
    assert not cap.notice.isVisible() or "switched off and on" not in cap.notice.text
    w.model._on_device(event(Connection.CONNECTED, Connection.DISCONNECTED))
    w.model._on_device(event(Connection.DISCONNECTED, Connection.CONNECTED))
    assert cap.notice.isVisible() and cap.notice.level == "warn"
    assert "switched off and on" in cap.notice.text and "focus again" in cap.notice.text


def test_stop_and_a_drive_fault_stay_until_they_are_cleared(win, monkeypatch):
    app, w, s = win
    cap = w.pages["capture"]
    s.core.router.stop_all("test STOP")
    try:
        assert wait_for(app, lambda: cap.notice.isVisible() and cap.notice.standing, 2.0)
        assert "STOP is engaged (test STOP)" in cap.notice.text and not cap.notice.btn_close.isVisible()
        w.model.add_log("warn", "a refusal meanwhile")
        assert "STOP is engaged" in cap.notice.text  # the standing notice outranks a passing one
    finally:
        s.core.router.clear_stop()
    assert wait_for(app, lambda: "a refusal meanwhile" in cap.notice.text, 2.0)  # the passing one shows after it

    real_get = s.core.store.get
    focus = s.profile.focus_device

    def get(did):
        snap = real_get(did)
        return snap.model_copy(update={"recovery_required": True, "note": "readback mismatch"}) \
            if snap and did == focus else snap

    monkeypatch.setattr(s.core.store, "get", get)
    level, text, action = cap.standing_notice()
    assert level == "error" and text.startswith("Focus needs recovery (readback mismatch)")
    assert action == ("mic.recover", "Recover…")
    assert wait_for(app, lambda: cap.notice.btn_action.isVisible(), 2.0)
    ran = []
    monkeypatch.setattr(w, "run_command", ran.append)
    cap.notice.btn_action.click()
    assert ran == ["mic.recover"]
    monkeypatch.setattr(s.core.store, "get", real_get)
    assert wait_for(app, lambda: not cap.notice.standing, 2.0)


def test_a_stage_lock_names_its_reason_and_offers_the_release(win, monkeypatch):
    _app, w, s = win
    from types import SimpleNamespace

    cap = w.pages["capture"]
    mon = SimpleNamespace(latched=True, latch=SimpleNamespace(message="X drifted 9 counts at rest"))
    monkeypatch.setattr(s, "telemetry", [SimpleNamespace(monitor=mon)])
    level, text, action = cap.standing_notice()
    assert level == "error" and "Stage locked by the drift monitor: X drifted 9 counts at rest" in text
    assert action == ("mic.release", "Release stage lock…")


def test_focus_says_why_it_is_held_only_while_it_is(win):
    _app, w, _s = win
    f = w.pages["capture"].focus
    f.set_state("locked", "Not homed · Z refuses moves until it is homed")
    assert f.lbl_hold.isVisible() and f.lbl_hold.text().startswith("Not homed")
    f.set_state("ready", "Homed · idle")
    assert not f.lbl_hold.isVisible()  # hidden while all is well, as the operator asked (2026-11-18)
    w.pages["capture"]._refresh_focus()


# ------------------------------------------------------------------ fix 3: the light's state on the image
def test_the_badge_and_a_hint_say_the_light_is_off(win):
    app, w, s = win
    cap = w.pages["capture"]
    assert not s.rig.light.enabled
    assert wait_for(app, lambda: "LIGHT OFF" in cap.view.badge, 3.0)
    assert cap.view.hint.startswith("Light off")
    slot = next(iter(cap.ch_buttons))
    cap.ch_buttons[slot].clicked.emit(slot)
    assert wait_for(app, lambda: s.rig.light.enabled, 3.0)
    assert wait_for(app, lambda: "LIGHT OFF" not in cap.view.badge and " • LIVE • " in cap.view.badge, 3.0)
    assert cap.view.hint == ""
    assert not cap.view.grab().isNull()


def test_a_selected_dark_channel_draws_no_accent(win):
    _app, w, _s = win
    from PySide6.QtGui import QColor

    from openevos.ui import theme

    cap = w.pages["capture"]
    b = next(b for b in cap.ch_buttons.values() if b.selected and not b.lit)
    img = b.grab().toImage()
    acc = QColor(theme.ACCENT)
    near = [img.pixelColor(x, y) for x in range(0, img.width(), 2) for y in range(0, 40, 2)]
    assert not any(abs(c.red() - acc.red()) + abs(c.green() - acc.green()) + abs(c.blue() - acc.blue()) < 30
                   for c in near)


# ------------------------------------------------------------------ fix 4: the objective row
def test_objectives_sit_above_the_stage_controls_and_show_the_change(win):
    _app, w, _s = win
    cap = w.pages["capture"]
    left = cap.joy.parentWidget()
    b = next(iter(cap.obj_buttons.values()))
    assert b.mapTo(left, QPoint(0, 0)).y() < cap.joy.mapTo(left, QPoint(0, 0)).y()
    assert b.mapTo(left, QPoint(0, 0)).y() < cap.map.mapTo(left, QPoint(0, 0)).y()
    slot = list(cap.obj_buttons)[1]
    cap._objective_progress(slot, "2/4 · Turning to 10X")
    assert cap.obj_buttons[slot].property("pending") is True and cap.lbl_obj_step.isVisible()
    assert all(not bb.property("pending") for s2, bb in cap.obj_buttons.items() if s2 != slot)
    cap._objective_progress(0, "")
    assert not cap.lbl_obj_step.isVisible() and not any(bb.property("pending") for bb in cap.obj_buttons.values())


def test_a_physical_objective_change_reports_each_step(tmp_path):
    """The real-stage sequence (drive emulator): every step reaches the page, in order, and the row clears."""
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=12, settings_path=tmp_path / "ui_settings.json",
                          calibration_path=tmp_path / "calibration.json",
                          telemetry={"technosoft": {"port": "sim", "motion": True}})
    w = MainWindow(SessionModel(s))
    w.resize(1600, 960)
    w.show()
    pump(app, 0.3)
    try:
        cap = w.pages["capture"]
        steps: list[str] = []
        cap.objective_step.connect(lambda _slot, text: steps.append(text))
        assert wait_for(app, lambda: cap.ctl.objective_slot() is not None, 10.0)
        cur = cap.ctl.objective_slot()
        target = next(sl for sl, b in cap.obj_buttons.items() if sl != cur and b.isEnabled())
        cap._centre_then_objective(target, centre=False)
        assert cap.lbl_obj_step.isVisible() and cap.obj_buttons[target].property("pending") is True
        assert wait_for(app, lambda: not w.model.tasks.busy and not cap.lbl_obj_step.isVisible(), 60.0)
        assert [t.split(" · ")[1] for t in steps][:1] == [f"Turning to {s.profile.objective(target).short}"]
        assert steps[0].startswith("1/2")
    finally:
        w.close()
        s.shutdown()
        w.deleteLater()
        pump(app, 0.1)
