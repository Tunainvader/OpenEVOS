"""Audit APP-16: the steps of a stage leg, a turret change and an F move, each on its own, on the simulated drive chain.

The split of _move_leg / _run_turret / _carriage_procedure into these steps changed no frame, message or error code
(checked against golden traces of 28 move and turret scenarios before the split); these tests pin each step.
"""

from __future__ import annotations

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft import watchdog
from openevos.backends.technosoft.transport import SimulatedDriveChain

CAL = {("X", "LPOSMIN"): 0, ("X", "LPOSMAX"): 270000, ("Y", "LPOSMIN"): 0, ("Y", "LPOSMAX"): 200000}
CENTRE = {("X", "APOS"): round(59.5 * tp.COUNTS_PER_MM["X"]), ("Y", "APOS"): round(39.0 * tp.COUNTS_PER_MM["Y"])}


class Ctx:
    def __init__(self):
        self.calls = []

    cancel_requested = False

    def fail(self, message, error_code=None):
        self.calls.append(("fail", message, error_code))

    def mark_cancelled(self, result=None):
        self.calls.append(("cancelled",))

    def complete(self, result=None, payload=None):
        self.calls.append(("complete", result))

    def mark_started(self):
        pass


def _setup(values=None):
    chain = SimulatedDriveChain({**CAL, **(values or {})}, allow_motion=True)
    svc = tm.TelemetryService(lambda: chain, "SIM", motion=True, sleep=lambda s: None)
    return chain, svc, tm.DriveLink(chain)


def _motion_writes(chain):
    return [w for w in chain.writes if w not in tp.ALLOWED_REQUESTS]


def _leg(axes, targets, start, ctx=None):
    return tm._Leg(ctx or Ctx(), axes, targets, start, dict.fromkeys(axes, 10.0),
                   {a: watchdog.LegWatch(a, start[a], targets[a], "fast", 0.0) for a in axes})


def test_leg_speeds_writes_the_preset_only_when_the_drive_holds_another():
    chain, svc, link = _setup()
    now = {"X": {"CSPDCMD": tp.speed_raw("slow")}}
    speed = svc._leg_speeds(link, ["X"], now, "slow")
    assert _motion_writes(chain) == [] and speed["X"] > 0
    now = {"X": {"CSPDCMD": tp.POWER_UP_SPEED_RAW}}
    svc._leg_speeds(link, ["X"], now, "slow")
    assert _motion_writes(chain) == [tp.speed_write("X", "slow")] and now["X"]["CSPDCMD"] == tp.speed_raw("slow")
    assert svc._speed_changed
    chain.writes.clear()
    svc._leg_speeds(link, ["X"], {"X": {"CSPDCMD": 1}}, None)  # no speed control: never written
    assert _motion_writes(chain) == []


def test_wrong_way_guard_trips_only_beyond_its_margin():
    chain, svc, link = _setup()
    run = _leg(["Y"], {"Y": 15000}, {"Y": 0})
    run.live = {"X": 0, "Y": -tm.WRONG_WAY_COUNTS}  # exactly at the margin: still under control
    assert not svc._leg_wrong_way(link, run, "Y") and not run.ctx.calls
    run.live["Y"] = -tm.WRONG_WAY_COUNTS - 1
    assert svc._leg_wrong_way(link, run, "Y")
    assert run.outcome == "wrong direction" and svc.monitor.latch.rule == "wrong_direction"
    assert run.ctx.calls[-1][2] == "wrong_direction" and "controlled stop sent" in run.ctx.calls[-1][1]
    assert _motion_writes(chain) == [*tp.stop_frames("X"), *tp.stop_frames("Y")]


def test_still_axis_backstop_latches_on_drift_and_stops_the_moving_axis_only():
    chain, svc, link = _setup({("Y", "APOS"): tm.DRIFT_MAX_COUNTS})
    run = _leg(["X"], {"X": 20000}, {"X": 0, "Y": 0})
    assert svc._leg_still_axis_ok(link, run)  # at the bound: fine
    # 2026-11-19: a knock up to CROSS_AXIS_TOLERANCE_COUNTS is tracked and put back after the move, not latched
    chain.values[("Y", "APOS")] = sm.CROSS_AXIS_TOLERANCE_COUNTS
    assert svc._leg_still_axis_ok(link, run) and not svc.monitor.latched
    chain.values[("Y", "APOS")] = sm.CROSS_AXIS_TOLERANCE_COUNTS + 1
    assert not svc._leg_still_axis_ok(link, run)
    assert run.outcome == "cross-axis drift" and svc.monitor.latch.rule == "cross_axis_encoder_drift"
    assert run.ctx.calls[-1][2] == "cross_axis_encoder_drift"
    assert _motion_writes(chain) == list(tp.stop_frames("X"))


@pytest.mark.parametrize("apos, code, outcome, latched", [(20000, None, None, False),
                                                           (20000 + tm.SETTLE_BAND_COUNTS, None, None, False),
                                                           (3, "no_motion", "no motion", False),
                                                           (20040, "readback", "landing error", True)])
def test_landing_readback(apos, code, outcome, latched):
    _chain, svc, link = _setup({("X", "APOS"): apos})
    run = _leg(["X"], {"X": 20000}, {"X": 0, "Y": 0})
    assert svc._leg_landed(link, run) is (code is None)
    assert (run.ctx.calls[-1][2] if run.ctx.calls else None) == code
    assert run.outcome == (outcome or "link error") and svc.monitor.latched is latched


def test_drive_flags_after_the_move_latch():
    chain, svc, link = _setup()
    run = _leg(["X"], {"X": 20000}, {"X": 0, "Y": 0})
    assert svc._leg_flags_ok(link, run)
    chain.values[("X", "SRL")] = 0x4400  # power stage off
    assert not svc._leg_flags_ok(link, run)
    assert run.outcome == "drive flags" and run.ctx.calls[-1][2] == "power_stage_off"


def test_leg_tick_cancel_and_timeout():
    chain, svc, link = _setup()
    run = _leg(["X"], {"X": 20000}, {"X": 0, "Y": 0})
    run.live, run.deadline = {"X": 0, "Y": 0}, float("inf")
    chain.values[("X", "SRH")] = 0x013F  # moving: motion-complete bit clear
    assert svc._leg_tick(link, run, 0.0) and not run.done
    chain.values[("X", "SRH")] = 0x033F
    assert svc._leg_tick(link, run, 0.0) and run.done == {"X"}
    run.done.clear()
    chain.values[("X", "SRH")] = 0x013F
    run.deadline = 0.0
    assert not svc._leg_tick(link, run, 0.0)
    assert run.outcome == "timeout" and run.ctx.calls[-1][2] == "timeout"
    run2 = _leg(["X"], {"X": 20000}, {"X": 0, "Y": 0})
    run2.ctx.cancel_requested = True
    run2.live = {"X": 0, "Y": 0}
    assert not svc._leg_tick(link, run2, 0.0)
    assert run2.outcome == "stopped" and run2.ctx.calls[-1] == ("cancelled",)


def test_turret_path_decreases_and_rejects_going_back():
    assert tm.TelemetryService._turret_path(1, 3) == [5, 4, 3]
    assert tm.TelemetryService._turret_path(4, 2) == [3, 2]
    assert tm.TelemetryService._turret_path(2, 2) == []
    path = [5, 4, 3]
    assert not tm.TelemetryService._turret_off_path(5, 1, path)
    assert not tm.TelemetryService._turret_off_path(4, 5, path)
    assert tm.TelemetryService._turret_off_path(5, 4, path)  # back along the path
    assert tm.TelemetryService._turret_off_path(2, 3, path)  # past the target


def test_turret_preconditions():
    _chain, svc, link = _setup(CENTRE)
    assert svc._turret_preconditions(link) == (1, [])
    _chain, svc, link = _setup({("X", "LPOSMAX"): tp.POWER_UP_LIMITS[1]})
    _cur, problems = svc._turret_preconditions(link)
    assert len(problems) == 1 and "limits are not loaded" in problems[0]
    _chain, svc, link = _setup({**CENTRE, ("O", "SRL"): 0xC400})
    assert svc._turret_preconditions(link)[1] == ["turret motor is on at rest"]


def test_turret_turn_and_abort():
    chain, svc, link = _setup(CENTRE)
    ctx = Ctx()
    assert svc._turret_turn(link, ctx, 1, 3) == ""
    assert _motion_writes(chain) == [tp.o_target(3), tp.O_GOTO_FRAME]
    chain.writes.clear()
    svc._turret_abort(link, ctx, "the drive flagged the turret stuck at position 1")
    assert _motion_writes(chain) == [tp.O_AXISOFF_FRAME] and ctx.calls[-1][2] == "turret"


def test_carriage_problems():
    _chain, _svc, link = _setup()
    names = ("ESTATEV", "IINITONCEDONE", "APOS", "CPOS", "LPOSMIN", "LPOSMAX", "MER", "LCONVERGEBAND")
    base = {n: link.read(tp.VARS["F"][n]) for n in names}
    stored = dict(tp.F_EXPECTED_POS)
    assert tm.TelemetryService._carriage_problems(base, stored) == []
    assert tm.TelemetryService._carriage_problems({**base, "ESTATEV": 2, "MER": 4}, stored) == [
        "F is not idle (state 2)", "F error register MER 0x0004 not clear"]
    assert "not the reviewed ones" in tm.TelemetryService._carriage_problems(base, {**stored, "slot1": 1})[0]
