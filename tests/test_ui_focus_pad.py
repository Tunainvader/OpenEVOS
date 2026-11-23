"""Capture page Z focus panel (design handoff 2026-11-18), on the simulated session only.

Acceptance checks from HANDOFF.md: each pad click moves the readback Z by exactly the step through ctl.move_focus; hover
moves the button at most 2 px; Coarse/Fine switch both the step list and the tape scale; out-of-range steps are refused
with a message, not clamped; locked / fault / objective states make the pad inert; renders in dark and light at the
narrow column width.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings
from openevos.ui.widgets.focus_pad import COARSE_STEPS, FINE_STEPS


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.theme import QSS

    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(QSS)
    return a


@pytest.fixture(scope="module")
def window(app, tmp_path_factory):
    """One window for the module: every extra MainWindow left in the process is restyled by each later theme switch,
    which made the full suite's theme test minutes slower."""
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
        w = MainWindow(SessionModel(s))
        w.resize(1600, 960)
        w.show()
        pump(app, 0.4)
        yield w
        w.close()
        s.shutdown()
        w.deleteLater()
        pump(app, 0.1)


def _idle(app, cap):
    return wait_for(app, lambda: not cap.coalesce.pending() and cap._z_goal is None)


def _warns(window):
    return [e for e in window.model.log if e[1] == "warn"]


def test_old_sliders_are_gone_and_the_panel_shows_the_readback(app, window):
    cap = window.pages["capture"]
    assert not hasattr(cap, "quick") and not hasattr(cap, "coarse") and not hasattr(cap, "lbl_z")
    z = cap.ctl.focus_z()
    assert z is not None
    assert wait_for(app, lambda: cap.focus.readout.text == f"{z:,.2f}")
    assert cap.focus.state == "simulated" and cap.focus.chip.state == "simulated"
    # the header row (title, status, chip) is hidden at the operator's request; the status is the readout's tooltip
    assert not cap.focus.header.isVisible()
    assert "Simulated drive" in cap.focus.readout.toolTip()


def test_each_click_moves_the_readback_by_exactly_the_step(app, window):
    cap = window.pages["capture"]
    assert _idle(app, cap)
    z0 = cap.ctl.focus_z()
    cap.focus.set_step(2.0)
    cap.focus.pad.up.click()
    assert _idle(app, cap)
    assert abs(cap.ctl.focus_z() - (z0 + 2.0)) < 1e-6
    cap.focus.pad.down.click()
    assert _idle(app, cap)
    assert abs(cap.ctl.focus_z() - z0) < 1e-6
    # rapid clicks add up from the in-flight target instead of being lost
    for _ in range(3):
        cap.focus.pad.up.click()
    assert _idle(app, cap)
    assert abs(cap.ctl.focus_z() - (z0 + 6.0)) < 1e-6


def test_coarse_fine_switch_steps_and_tape_scale(app, window):
    cap = window.pages["capture"]
    f = cap.focus
    assert f.mode == "fine" and f.tape.fine
    assert [f.cmb_step.itemData(i) for i in range(f.cmb_step.count())] == list(FINE_STEPS)
    f.set_step(5.0)
    f.seg_mode.buttons["coarse"].click()
    assert f.mode == "coarse" and not f.tape.fine
    assert [f.cmb_step.itemData(i) for i in range(f.cmb_step.count())] == list(COARSE_STEPS)
    f.seg_mode.buttons["fine"].click()
    assert f.step_um() == 5.0  # each mode remembers its step
    assert "5 µm" in f.pad.up.accessibleName() and "toward the sample" in f.pad.up.accessibleName()


def test_out_of_range_step_is_refused_not_clamped(app, window):
    cap = window.pages["capture"]
    assert _idle(app, cap)
    lo, _hi = cap.ctl.axis_limits(cap.profile.focus_device, "z")
    cap._move_z(lo + 3.0)
    assert _idle(app, cap)
    n = len(_warns(window))
    cap.focus.set_step(10.0)
    cap.focus.pad.down.click()  # would go to lo - 7 µm
    pump(app, 0.2)
    assert abs(cap.ctl.focus_z() - (lo + 3.0)) < 1e-6
    w = _warns(window)[n:]
    assert w and "refused" in w[-1][2] and "outside the focus range" in w[-1][2]


def test_inert_states_block_the_pad(app, window):
    cap = window.pages["capture"]
    f = cap.focus
    seen = []

    def record(step, held):
        seen.append((step, held))

    f.step_requested.connect(record)  # disconnected below: the window is shared by the whole module
    try:
        for state in ("locked", "fault", "objective"):
            f.set_state(state, "test")
            assert not f.pad.up.isEnabled() and not f.pad.down.isEnabled()
            f.pad.stepped.emit(1, 1, False)  # a wheel / key step while inert
            f.pad.stepped.emit(1, 1, True)  # a held repeat while inert
    finally:
        f.step_requested.disconnect(record)
    assert seen == []
    f.set_state("locked", "Not homed")
    f.set_values(None, 8050.0, None, None)
    assert f.readout.text == "— — —"
    f.set_state("ready", "Homed · idle")
    assert f.pad.up.isEnabled()
    cap._refresh_focus()  # back to the session's real state for the next test (one window per module)
    assert f.readout.text != "— — —"


def test_hover_nudges_two_px_and_press_pose_follows_motion(app, window):
    pad = window.pages["capture"].focus.pad
    pad.hover = "up"
    pad._sync()
    pump(app, 0.4)
    assert abs(pad._off - (-2.0)) < 0.05
    pad.set_moving("down")
    pump(app, 0.4)
    assert abs(pad._off - 18.0) < 0.05
    pad.set_moving(None)
    pad.hover = None
    pad._sync()
    pump(app, 0.4)
    assert abs(pad._off) < 0.05


def test_pad_drives_the_real_z_motor_frames_with_motor_control(app, tmp_path):
    """With motor control (the 'OpenEVOS (real stage)' shortcut), the pad moves motor.focus through the stage service:
    Gate 18's Z frames (target, move, stop) on the simulated drive chain. + is up, toward the sample."""
    from openevos.backends.technosoft import protocol as tp
    from openevos.backends.technosoft import telemetry as tm
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "motion": True}})
    w = MainWindow(SessionModel(s))
    try:
        w.show()
        pump(app, 0.4)
        cap = w.pages["capture"]
        assert s.profile.focus_device == "motor.focus"
        chain = s.telemetry[0].simulated_chain
        assert wait_for(app, lambda: cap.focus.state in ("ready", "simulated") and cap.ctl.focus_z() is not None)
        # the drive emulator is labelled simulated (audit APP-02), while the frames below are the real drive's frames
        assert cap.focus.state == "simulated" and "Simulated drive" in cap.focus.readout.toolTip()
        z0 = cap.ctl.focus_z()
        chain.writes.clear()
        cap.focus.set_mode("coarse")
        cap.focus.set_step(100.0)
        cap.focus.pad.up.click()
        assert wait_for(app, lambda: not cap.coalesce.pending() and cap._z_goal is None, 15)
        want = tm.counts_from_um("Z", z0 + 100.0)
        assert chain.values[("Z", "APOS")] == want
        z_writes = [x for x in chain.writes if len(x) > 2 and x[2] == tp.Z_ID << 4 and x not in tp.ALLOWED_REQUESTS]
        assert z_writes == [tp.z_target(want), tp.Z_MOVE_FRAME, tp.Z_STOP_FRAME]
        assert abs(cap.ctl.focus_z() - (z0 + 100.0)) < tm.um_from_counts("Z", 1)  # within one encoder count
    finally:
        w.close()
        s.shutdown()
        w.deleteLater()
        pump(app, 0.1)


def test_hold_keeps_moving_and_stops_on_release(app, window):
    """Holding the top half keeps stepping (auto-repeat); releasing adds no extra step, and Z stops within about two
    steps of the release."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    cap = window.pages["capture"]
    f = cap.focus
    assert _idle(app, cap)
    f.set_mode("fine")
    f.set_step(1.0)
    z0 = cap.ctl.focus_z()
    half = f.pad.up
    ended = []
    f.hold_released.connect(lambda: ended.append(cap.ctl.focus_z()))
    QTest.mousePress(half, Qt.LeftButton, pos=QPoint(half.width() // 2, half.height() // 2))
    pump(app, 1.3)  # 400 ms delay, then a step every 100 ms
    assert half.repeated and not ended
    QTest.mouseRelease(half, Qt.LeftButton, pos=QPoint(half.width() // 2, half.height() // 2))
    assert len(ended) == 1  # the release ends the hold once
    assert not cap.coalesce.waiting("focus") and cap._z_goal is None  # nothing left queued
    assert _idle(app, cap)
    z_end = cap.ctl.focus_z()
    assert z_end - z0 >= 4.0  # kept moving while held
    pump(app, 0.4)
    assert cap.ctl.focus_z() == z_end  # and stopped: no step after the release


def test_hold_release_drops_the_queued_step_and_cancels_the_running_move(app, window):
    cap = window.pages["capture"]

    class Ticket:
        def __init__(self):
            self.status = type("S", (), {"is_terminal": False})()
            self.cancelled = 0

        def cancel(self):
            self.cancelled += 1
            return True

    t = Ticket()
    cap.coalesce._inflight["focus"] = t
    cap.coalesce._want["focus"] = ("focus", lambda: None)
    cap._z_goal = 123.0
    cap._focus_hold_end()
    assert t.cancelled == 1 and not cap.coalesce.waiting("focus") and cap._z_goal is None
    assert any("Z stopped" in e[2] for e in window.model.log)
    cap.coalesce._inflight.pop("focus", None)


def test_objective_safe_height_warns_and_stops_a_hold(app, window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a: asked.append(a[2]) or QMessageBox.Yes)
    cap = window.pages["capture"]
    f = cap.focus
    assert _idle(app, cap)
    f.set_mode("fine")
    f.set_step(10.0)
    z0 = cap.ctl.focus_z()
    slot = cap.ctl.objective_slot()
    name = cap.profile.objective(slot).short
    try:
        cap._set_focus_limit()  # safe height = here
        assert cap.settings.basic.focus_limit_um[str(slot)] == round(z0, 2)
        assert f.tape.limit == round(z0, 2) and f.readout.warning == ""
        assert f.act_limit_set.text().startswith(f"Set {name} limit here")
        # a held move upward does not cross it
        n = len(_warns(window))
        cap._focus_step(10.0, held=True)
        pump(app, 0.2)
        assert _idle(app, cap) and abs(cap.ctl.focus_z() - z0) < 1e-6
        assert "stopped at the" in _warns(window)[n][2]
        # a deliberate click does, with an amber readout and a log line
        f.pad.up.click()
        assert _idle(app, cap)
        assert abs(cap.ctl.focus_z() - (z0 + 10.0)) < 1e-6
        cap._refresh_focus()
        assert f.readout.warning == f"above {name} limit"
        assert any("above the" in w[2] for w in _warns(window)[n:])
        # below the limit again: no warning
        f.pad.down.click()
        assert _idle(app, cap)
        cap._refresh_focus()
        assert f.readout.warning == ""
        # a held move from below the limit ends exactly at it
        cap._move_z(z0 - 25.0)
        assert _idle(app, cap)
        for _ in range(6):
            cap._focus_step(10.0, held=True)
            assert _idle(app, cap)
        assert abs(cap.ctl.focus_z() - round(z0, 2)) < 1e-6
    finally:
        cap._clear_focus_limit()
    assert str(slot) not in cap.settings.basic.focus_limit_um and f.tape.limit is None


@pytest.mark.parametrize("size", [(1280, 800), (1366, 768)])
def test_capture_button_is_visible_without_scrolling(app, window, size):
    """Audit APP-06: the Capture block is pinned below the scrolling Light & focus column."""
    from PySide6.QtCore import QPoint

    window.resize(*size)
    pump(app, 0.4)
    cap = window.pages["capture"]
    for b in (cap.btn_capture, *[h for h in cap.hot if h.isVisible()]):
        top = b.mapTo(window, QPoint(0, 0))
        assert b.isVisible() and not b.visibleRegion().isEmpty()
        assert top.y() + b.height() <= window.height(), (b.text(), top.y(), window.height())
    window.resize(1600, 960)
    pump(app, 0.2)


@pytest.mark.parametrize("name", ["dark", "light"])
def test_panel_renders_in_both_themes_at_column_width(app, window, name, tmp_path):
    from openevos.ui import theme

    theme.apply_theme(name, app)
    try:
        window.resize(1100, 720)  # the narrowest window: the panel scales to the column
        pump(app, 0.4)
        f = window.pages["capture"].focus
        assert f.pad.scale() >= 0.35 and f.width() <= 330
        assert f.height() >= f.minimumSizeHint().height()  # the column scrolls; the panel is never squashed
        assert f.pad.height() >= f.pad.minimumHeight()
        img = f.grab().toImage()
        out = Path(os.environ.get("OPENEVOS_SHOT_DIR", tmp_path)) / f"zfocus_{name}.png"
        img.save(str(out))
        colours = {img.pixel(x, y) for x in range(0, img.width(), 7) for y in range(0, img.height(), 7)}
        assert len(colours) > 40  # painted, not blank
    finally:
        theme.apply_theme("dark", app)
