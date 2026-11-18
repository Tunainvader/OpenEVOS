"""ADR-0056: the stage service loads the calibrated X/Y limits (Gate 7's frames) once X and Y finish homing on their
power-up limits, e.g. after a power cycle while the app runs. Simulated drive chain only."""

from __future__ import annotations

import time

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain, check_write
from openevos.maintenance import motor_gate7_xy_limits as g7

CAL = {"X": (-4000, 266000), "Y": (-4000, 173000)}


def test_frames_are_gate_7s_and_the_port_refuses_them_unless_registered():
    for ax, aid in (("X", 1), ("Y", 2)):
        for var, val in zip(("LPOSMIN", "LPOSMAX"), CAL[ax], strict=True):
            f = tp.limit_write(ax, var, val)
            assert f == g7.build_limit_write(aid, var, val)
            assert not tp.is_motion_write(f)
            with pytest.raises(tp.ProtocolRefused):
                check_write(f, allow_motion=True)
            check_write(f, allow_motion=True, extra=frozenset({f}))  # only while registered
            with pytest.raises(tp.ProtocolRefused):
                check_write(f, allow_motion=False, extra=frozenset({f}))  # and never without motor control


def _svc(chain):
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True, sleep=lambda s: time.sleep(0.001))
    svc.calibrated_limits = CAL
    svc.start()
    return svc


def _limit_writes(chain):
    return [w for w in chain.writes if w[3:5] in (bytes([0x25, 0xB4]), bytes([0x25, 0xB6]))]


def _wait(cond, t=5.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_reloads_after_homing_and_again_after_a_re_home():
    chain = SimulatedDriveChain(allow_motion=True)  # homed at 0, 0 on the power-up limits 0..50,000
    svc = _svc(chain)
    try:
        assert _wait(lambda: svc.limits_reloads)
        assert svc.limits_reloads[0]["ok"]
        assert {ax: (chain.values[(ax, "LPOSMIN")], chain.values[(ax, "LPOSMAX")]) for ax in "XY"} == CAL
        assert _limit_writes(chain) == [tp.limit_write(ax, v, val) for ax in "XY"
                                        for v, val in zip(("LPOSMIN", "LPOSMAX"), CAL[ax], strict=True)]
        assert chain.extra_writes == frozenset()  # registered only during the operation
        # power cycle while the app runs: X and Y re-initialise and come back on the power-up limits
        for ax in "XY":
            chain.values[(ax, "IINITONCEDONE")] = 0
        time.sleep(0.3)
        for ax in "XY":
            chain.values[(ax, "LPOSMIN")], chain.values[(ax, "LPOSMAX")] = tp.POWER_UP_LIMITS
            chain.values[(ax, "IINITONCEDONE")] = tp.INIT_DONE_MARKER
        assert _wait(lambda: len(svc.limits_reloads) == 2)
        assert chain.values[("Y", "LPOSMAX")] == CAL["Y"][1]
        time.sleep(0.3)
        assert len(_limit_writes(chain)) == 8  # once per homing, never repeated
    finally:
        svc.stop()


def test_no_write_when_away_from_home_or_busy_or_without_motor_control():
    chain = SimulatedDriveChain(allow_motion=True, values={("X", "APOS"): 38386, ("X", "CPOS"): 38386})
    svc = _svc(chain)
    try:
        time.sleep(0.5)
        assert _limit_writes(chain) == [] and svc.limits_reloads == []
    finally:
        svc.stop()
    chain = SimulatedDriveChain(allow_motion=False)
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=False)
    svc.calibrated_limits = CAL
    svc.start()
    try:
        time.sleep(0.4)
        assert _limit_writes(chain) == []
    finally:
        svc.stop()
