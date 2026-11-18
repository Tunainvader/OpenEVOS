"""The colour camera sees the sample only with the F carriage at its mirror (Motor Gate 12, 2026-11-15): switching to
the colour camera routes F there, switching back to mono returns it to slot 1. Simulated drive chain only."""

from __future__ import annotations

import time

from openevos.acquisition.session import build_sim_session
from openevos.backends.technosoft import protocol as tp
from openevos.core.clock import ThreadClock
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings


def _f(s):
    snap = s.core.store.get("motor.cube_carriage")
    return snap.observed_position.values[0] if snap and snap.observed_position else None


def _wait(cond, t=5.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_colour_routes_f_to_the_mirror_and_mono_brings_it_back():
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    try:
        svc = s.telemetry[0]
        svc.carriage_control = True  # as with the real LED board (ADR-0051)
        assert _wait(lambda: _f(s) is not None)
        assert s.route_light_path(CameraRole.MONO) == ""  # F at slot 1 already: nothing for mono
        msg = s.route_light_path(CameraRole.COLOR)
        assert "to mirror" in msg, msg
        chain = svc.simulated_chain
        assert chain.values[("F", "APOS")] == tp.F_EXPECTED_POS["mirror"]
        assert _wait(lambda: abs(_f(s) - tp.F_EXPECTED_POS["mirror"]) <= 200)
        assert s.route_light_path(CameraRole.COLOR) == ""  # already there: nothing sent
        assert "to slot1" in s.route_light_path(CameraRole.MONO)
        assert chain.values[("F", "APOS")] == tp.F_EXPECTED_POS["slot1"]
    finally:
        s.shutdown()


def test_no_carriage_control_means_nothing_moves():
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    try:
        assert s.route_light_path(CameraRole.COLOR) == ""
        assert s.telemetry[0].simulated_chain.values[("F", "APOS")] == 0
    finally:
        s.shutdown()
