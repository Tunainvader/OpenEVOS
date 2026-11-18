"""Audit APP-01/APP-03: the app's router enforces the instrument rules that fit its backends."""

from __future__ import annotations

import time

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.core.models import CommandStatus
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings
from openevos.safety.interlocks import FocusSafeHeight, InstrumentInterlocks


def _session(**kw):
    return build_sim_session(ThreadClock(), fast=True, settings=AppSettings(), cameras=None, illumination=None, **kw)


def test_sim_session_registers_the_turret_and_safe_height_rules():
    s = _session()
    try:
        names = [type(i).__name__ for i in s.core.safety.interlocks]
        assert "InstrumentInterlocks" in names and "FocusSafeHeight" in names
        ctl = s.controller
        # turret with Z above the turret-safe height: a raw turret command is refused by the router
        from openevos.core.models import CommandOrigin, SetTurretPosition

        assert ctl.move_focus(3000.0).wait(10) is CommandStatus.SUCCEEDED
        t = s.core.router.submit(SetTurretPosition(device_id=s.profile.turret_device, origin=CommandOrigin.TEST,
                                                   position=3))
        assert t.wait(10) is CommandStatus.REJECTED and "turret-safe" in t.record.message
        # the controller (every app path: UI, scans, time-lapse, calibration) lowers Z for the turn, so the turret
        # command passes the interlock, then puts Z back
        seen = []
        sub = s.core.bus.subscribe(__import__("openevos.core.event_bus", fromlist=["CommandLifecycleEvent"])
                                   .CommandLifecycleEvent, lambda e: seen.append(e))
        assert ctl.set_objective(3).wait(10) is CommandStatus.SUCCEEDED
        sub.unsubscribe()
        assert ctl.objective_slot() == 3 and abs(ctl.focus_z() - 3000.0) <= 1
        focus_targets = [round(e.result.get("z")) for e in seen if e.device_id == s.profile.focus_device
                         and e.status is CommandStatus.SUCCEEDED and e.result is not None]
        assert focus_targets[:2] == [1500, 3000]  # lowered for the turn, then back
        # the session's objective change lowers Z first, turns, and puts Z back
        t = s.change_objective(2)
        assert t.wait(10) is CommandStatus.SUCCEEDED and ctl.objective_slot() == 2
        end = time.monotonic() + 5
        while time.monotonic() < end and abs(ctl.focus_z() - 3000.0) > 1:
            time.sleep(0.02)
        assert abs(ctl.focus_z() - 3000.0) <= 1
    finally:
        s.shutdown()


def test_real_turret_service_is_not_double_guarded():
    s = _session(telemetry={"technosoft": {"port": "sim", "motion": True}})
    try:
        assert s.profile.turret_device == "motor.objective_turret"
        assert not any(isinstance(i, InstrumentInterlocks) for i in s.core.safety.interlocks)
        assert any(isinstance(i, FocusSafeHeight) for i in s.core.safety.interlocks)
    finally:
        s.shutdown()


def test_focus_safe_height_refuses_upward_moves_unless_confirmed():
    s = _session()
    try:
        ctl = s.controller
        assert ctl.move_focus(50.0).wait(10) is CommandStatus.SUCCEEDED
        s.settings.basic.focus_limit_um[str(ctl.objective_slot())] = 100.0
        t = ctl.move_focus(150.0)
        assert t.wait(10) is CommandStatus.REJECTED and "safe height" in t.record.message
        assert abs(ctl.focus_z() - 50.0) < 1
        assert ctl.move_focus(150.0, confirmed=True).wait(10) is CommandStatus.SUCCEEDED  # operator override
        assert ctl.move_focus(120.0).wait(10) is CommandStatus.SUCCEEDED  # downward: always allowed
        assert ctl.move_focus(90.0).wait(10) is CommandStatus.SUCCEEDED
        # autofocus never searches above it
        assert s.autofocus.z_cap() == 100.0
    finally:
        s.shutdown()


def test_autofocus_window_is_clamped_to_the_safe_height():
    s = build_sim_session(ThreadClock(), fast=True, settings=AppSettings())
    try:
        ctl = s.controller
        ctl.run(ctl.set_light("trans", 50.0, True))
        s.settings.basic.focus_limit_um[str(ctl.objective_slot())] = 3000.0
        r = s.autofocus.sweep(CameraRole.MONO, 3000.0, 400.0, 9)
        assert r.z and max(r.z) <= 3000.0 + 1e-6
    finally:
        s.shutdown()
