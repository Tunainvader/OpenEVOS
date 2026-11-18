"""Changing objective with the stage outside the turret box: centre (X, then Y), turn, return (Y, then X)
(operator 2026-11-18/19)."""

from __future__ import annotations

import time

import pytest

from openevos.acquisition.turret_safety import TURRET_CENTRE_MM, TURRET_STAGE_BOX_MM, outside_turret_box


def test_the_ui_box_is_the_drive_service_box():
    from openevos.backends.technosoft.telemetry import TURRET_STAGE_BOX_MM as SERVICE_BOX

    assert TURRET_STAGE_BOX_MM == SERVICE_BOX
    assert all(lo <= TURRET_CENTRE_MM[ax] <= hi for ax, (lo, hi) in TURRET_STAGE_BOX_MM.items())


def test_outside_turret_box():
    assert outside_turret_box((0.0, 0.0)) and outside_turret_box((46_700.0, 5_600.0))
    assert not outside_turret_box((60_000.0, 40_000.0)) and not outside_turret_box(None)


def _wait(app, cond, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    import openevos.ui.pages.capture as capmod
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import AppSettings
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    monkeypatch.setenv("HOME", str(tmp_path))
    # the simulator's stage spans +-50 mm: aim inside the box at a point it can reach
    monkeypatch.setattr(capmod, "TURRET_CENTRE_MM", {"X": 45.0, "Y": 30.0})
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    monkeypatch.setattr(type(s), "physical_stage", property(lambda _self: True), raising=False)
    w = MainWindow(SessionModel(s))
    w.show()
    yield w
    w.close()
    s.shutdown()


def _record_moves(ctl, monkeypatch) -> list:
    moves: list = []
    real = ctl.move_stage
    monkeypatch.setattr(ctl, "move_stage", lambda x, y, **kw: moves.append((x, y)) or real(x, y, **kw))
    return moves


def test_off_centre_objective_click_centres_turns_and_returns(app, window, monkeypatch):
    cap, ctl = window.pages["capture"], window.session.controller
    ctl.run(ctl.move_stage(10_000.0, 8_000.0))
    moves = _record_moves(ctl, monkeypatch)
    cap._objective(2)
    assert _wait(app, lambda: ctl.objective_slot() == 2 and len(moves) >= 4 and not window.model.tasks.busy)
    # X alone, Y alone to the centre; after the turn Y alone, X alone back
    assert moves[:4] == [(45_000.0, 8_000.0), (45_000.0, 30_000.0), (45_000.0, 8_000.0), (10_000.0, 8_000.0)]
    x, y = ctl.stage_xy()
    assert x == pytest.approx(10_000.0, abs=5) and y == pytest.approx(8_000.0, abs=5)


def test_the_turret_command_outlasts_the_longest_real_turn():
    from openevos.acquisition.controller import TURRET_TIMEOUT_S
    from openevos.backends.technosoft.telemetry import TURRET_STEP_S_MAX

    assert TURRET_TIMEOUT_S >= 4 * TURRET_STEP_S_MAX + 5.0  # the service's own deadline for 4 steps


def test_a_failed_turn_leaves_the_stage_centred(app, window, monkeypatch):
    from openevos.acquisition.controller import CommandError
    from openevos.core.models import CommandStatus

    cap, s = window.pages["capture"], window.session
    ctl = s.controller
    ctl.run(ctl.move_stage(10_000.0, 8_000.0))
    moves = _record_moves(ctl, monkeypatch)

    def refuse(_slot):
        raise CommandError("turret.set_position", CommandStatus.FAILED, "turret stuck")

    monkeypatch.setattr(s, "change_objective", refuse)
    cap._objective(3)
    assert _wait(app, lambda: len(moves) >= 2 and not window.model.tasks.busy)
    time.sleep(0.2)
    app.processEvents()
    # the turret may still be swinging: no return move, the stage stays over the opening
    assert moves == [(45_000.0, 8_000.0), (45_000.0, 30_000.0)]
    assert "stays centred" in cap.lbl_task.text()


def test_a_stage_just_below_zero_after_homing_still_centres(app, window, monkeypatch):
    """2026-11-18 18:39: after power-up homing Y read -0.44 µm and every centring move was refused as out of range."""
    cap, ctl = window.pages["capture"], window.session.controller
    ctl.run(ctl.move_stage(10_000.0, 8_000.0))
    lim = ctl.axis_limits(window.session.profile.stage_device, "y")
    monkeypatch.setattr(ctl, "stage_xy", lambda: (10_000.0, lim[0] - 0.44))
    moves = _record_moves(ctl, monkeypatch)
    cap._objective(2)
    assert _wait(app, lambda: len(moves) >= 2 and not window.model.tasks.busy)
    assert moves[0] == (45_000.0, lim[0])  # Y held at its edge, not at -0.44 below it
    assert "failed" not in cap.lbl_task.text()


def test_nothing_moves_when_the_turret_is_off_its_positions(app, window, monkeypatch):
    """2026-11-18 18:56: after a power cycle the turret sensor read 42214 and the centring moved X anyway."""
    cap, ctl = window.pages["capture"], window.session.controller
    ctl.run(ctl.move_stage(10_000.0, 8_000.0))
    monkeypatch.setattr(ctl, "objective_slot", lambda: 42214)
    moves = _record_moves(ctl, monkeypatch)
    cap._objective(2)
    _wait(app, lambda: False, 0.3)
    assert moves == []


def test_nothing_moves_when_the_stage_is_not_ready(app, window, monkeypatch):
    from openevos.core.models import Health

    cap, s = window.pages["capture"], window.session
    ctl = s.controller
    ctl.run(ctl.move_stage(10_000.0, 8_000.0))
    snap = s.core.store.get(s.profile.stage_device)
    monkeypatch.setattr(s.core.store, "get", lambda _d: snap.model_copy(update={"health": Health.WARNING}))
    moves = _record_moves(ctl, monkeypatch)
    cap._objective(2)
    _wait(app, lambda: False, 0.3)
    assert moves == []


def test_inside_the_box_turns_without_moving_the_stage(app, window, monkeypatch):
    cap, ctl = window.pages["capture"], window.session.controller
    ctl.run(ctl.move_stage(45_000.0, 30_000.0))
    moves = _record_moves(ctl, monkeypatch)
    cap._objective(2)
    assert _wait(app, lambda: ctl.objective_slot() == 2 and not cap.coalesce.pending())
    assert moves == []
