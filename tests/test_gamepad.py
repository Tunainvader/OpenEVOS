"""USB gamepad -> Capture page (operator request 2026-11-18): the mapper's rules, then the bridge on a sim window."""

from __future__ import annotations

import time

import pytest

from openevos.ui.gamepad import JOG_REPEAT_S, GamepadMapper, PadState


def test_left_stick_jogs_one_axis_only_and_releases_at_centre():
    m = GamepadMapper()
    acts = m.step(PadState(lx=0.9, ly=0.5), 0.0)  # mostly right: X only
    assert acts and acts[0][0] == "jog" and acts[0][1] > 0 and acts[0][2] == 0
    assert m.step(PadState(lx=0.9, ly=0.5), 0.05) == []  # held: repeats only every JOG_REPEAT_S
    assert m.step(PadState(lx=0.9, ly=0.5), JOG_REPEAT_S + 0.01)[0][0] == "jog"
    up = m.step(PadState(ly=1.0), 1.0)  # stick up = screen up (vy < 0) = Y+; a new direction stops first
    assert up[0] == ("jog_release",) and up[1][0] == "jog" and up[1][1] == 0 and up[1][2] < 0
    assert m.step(PadState(lx=0.1, ly=0.1), 1.1) == [("jog_release",)]  # inside the dead zone
    assert m.step(PadState(), 1.2) == []


def test_dpad_wins_over_the_stick_and_pace_follows_deflection():
    m = GamepadMapper()
    acts = m.step(PadState(lx=-1.0, buttons=frozenset({"right"})), 0.0)
    assert acts[0][0] == "jog" and acts[0][1] > 0
    slow = GamepadMapper().step(PadState(lx=0.3), 0.0)[0][1]
    fast = GamepadMapper().step(PadState(lx=1.0), 0.0)[0][1]
    assert 0.3 <= slow < fast <= 1.0


def test_right_stick_holds_focus_and_buttons_fire_on_press_only():
    m = GamepadMapper()
    assert m.step(PadState(ry=0.6), 0.0) == [("focus", 1, 1)]
    assert m.step(PadState(ry=-1.0), 0.01) == [("focus_release",), ("focus", -1, 2)]
    assert m.step(PadState(), 0.02) == [("focus_release",)]
    a = frozenset({"a"})
    assert m.step(PadState(buttons=a), 0.1) == [("capture",)]
    assert m.step(PadState(buttons=a), 0.2) == []  # still down: no repeat
    assert m.step(PadState(buttons=frozenset({"rb", "lb"})), 0.3) == [("focus_down",), ("focus_up",)]


def test_back_releases_everything_and_reset_swallows_buttons_already_down():
    m = GamepadMapper()
    m.step(PadState(lx=1.0, ry=1.0), 0.0)
    assert m.step(PadState(lx=1.0, ry=1.0, buttons=frozenset({"back"})), 0.01)[:3] == \
        [("jog_release",), ("focus_release",), ("stop",)]
    m = GamepadMapper()
    m.step(PadState(lx=1.0), 0.0)
    assert m.reset(PadState(buttons=frozenset({"a"}))) == [("jog_release",)]
    assert m.step(PadState(buttons=frozenset({"a"})), 1.0) == []  # A was held through the dialog: no capture


# ---------------------------------------------------------------------- the bridge on a simulated window
def _pump(app, s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _wait(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


class FakePad:
    available = True

    def __init__(self):
        self.state: PadState | None = PadState()

    def read(self):
        return self.state


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def window(app, tmp_path_factory):
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import AppSettings
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
        w = MainWindow(SessionModel(s))
        w.resize(1600, 960)
        w.show()
        _pump(app, 0.4)
        yield w
        w.close()
        s.shutdown()


@pytest.fixture
def bridge(window, monkeypatch):
    from openevos.ui.gamepad import GamepadBridge

    pad = FakePad()
    b = GamepadBridge(window, source=pad)
    monkeypatch.setattr(b, "_accepting", lambda: True)  # offscreen windows are never "active"
    b.pad = pad
    yield b
    b.stop()


def test_bridge_jogs_the_stage_and_focuses(app, window, bridge):
    cap, ctl = window.pages["capture"], window.session.controller
    _wait(app, lambda: not cap.coalesce.pending())
    x0, y0 = ctl.stage_xy()
    bridge.pad.state = PadState(lx=1.0)
    bridge.poll()
    assert _wait(app, lambda: ctl.stage_xy()[0] > x0 + 1)
    assert ctl.stage_xy()[1] == pytest.approx(y0)  # one axis only
    bridge.pad.state = PadState()
    bridge.poll()
    assert _wait(app, lambda: not cap.coalesce.pending())
    z0 = ctl.focus_z()
    bridge.pad.state = PadState(ry=-0.6)  # down: never meets the safe height
    for _ in range(3):
        bridge.poll()
        _pump(app, 0.13)
    bridge.pad.state = PadState()
    bridge.poll()
    assert _wait(app, lambda: not cap.coalesce.pending())
    assert ctl.focus_z() < z0


def test_bridge_buttons_light_and_focus_mode(app, window, bridge):
    cap = window.pages["capture"]
    was_on, mode = cap.btn_power.isChecked(), cap.focus.mode
    bridge.poll()  # idle first: buttons already down when input resumes do not fire
    bridge.pad.state = PadState(buttons=frozenset({"b", "y"}))
    bridge.poll()
    assert cap.btn_power.isChecked() is not was_on and cap.focus.mode != mode
    bridge.pad.state = PadState()
    bridge.poll()
    bridge.pad.state = PadState(buttons=frozenset({"b", "y"}))
    bridge.poll()
    assert cap.btn_power.isChecked() is was_on and cap.focus.mode == mode


def test_bridge_ignores_the_pad_when_disabled_or_unplugged(app, window, bridge, monkeypatch):
    from openevos.ui.gamepad import GamepadBridge

    cap = window.pages["capture"]
    pad = FakePad()
    b = GamepadBridge(window, source=pad)
    window.session.settings.basic.gamepad_enabled = False
    try:
        assert not b._accepting()
        was = cap.btn_power.isChecked()
        pad.state = PadState(buttons=frozenset({"b"}))
        b.poll()
        assert cap.btn_power.isChecked() is was
    finally:
        window.session.settings.basic.gamepad_enabled = True
    # unplugged mid-hold: the held move is released
    released = []
    monkeypatch.setattr(cap, "_joy_released", lambda: released.append(1))
    bridge.pad.state = PadState(lx=1.0)
    bridge.poll()
    bridge.pad.state = None
    bridge.poll()
    assert released and not bridge.connected
