"""ADR-0061 (operator 2026-11-22, "lets do a deep fix with automatic handling"): a landing outside the 5-count band at
motion complete gets a settle window, then one automatic re-approach, and locks only on a repeated or large miss; a
small landing lock can be released. On the unit, 2026-11-19 05:50 Y read -6 at the check and +1 ~0.3 s later; on
2026-11-21 18:18 Y stayed +6 off its target at 65.6 mm and the stage locked overnight. Simulated drive chain."""

from __future__ import annotations

import csv
import threading
import time

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft import watchdog
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.core.models import MoveStageAbsolute

X0, Y0 = 1000, 40000


class Ctx:
    def __init__(self):
        self.status, self.message, self.code = None, "", None
        self.done = threading.Event()
        self.cancel_requested = False

    def mark_started(self):
        pass

    def report_progress(self, fraction, message=None):
        pass

    def complete(self, result=None, payload=None):
        self.status = "ok"
        self.done.set()

    def fail(self, message, error_code=None):
        self.status, self.message, self.code = "failed", message, error_code
        self.done.set()

    def mark_cancelled(self, result=None):
        self.status = "cancelled"
        self.done.set()


def _setup(evidence=None):
    chain = SimulatedDriveChain({("X", "LPOSMIN"): -1000, ("X", "LPOSMAX"): 269000, ("Y", "LPOSMIN"): -1500,
                                 ("Y", "LPOSMAX"): 175600, ("X", "APOS"): X0, ("X", "CPOS"): X0, ("Y", "APOS"): Y0,
                                 ("Y", "CPOS"): Y0}, allow_motion=True)
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True,
                              sleep=lambda s: time.sleep(0.001), monitor=sm.StageMonitor(evidence))
    svc.start()
    deadline = time.monotonic() + 5
    while not svc.monitor.status_seen and time.monotonic() < deadline:
        time.sleep(0.02)
    return chain, svc


def _move(svc, x_counts: int, y_counts: int) -> Ctx:
    ctx = Ctx()
    stage = next(b for b in svc.backends if b.device_id == "motor.stage")
    stage.start(MoveStageAbsolute(device_id="motor.stage", origin="test", x=tm.um_from_counts("X", x_counts),
                                  y=tm.um_from_counts("Y", y_counts), unit="um", frame="stage"), ctx)
    assert ctx.done.wait(30)
    return ctx


def _targets(chain, ax: str) -> list[int]:
    head = tp.move_target(ax, 0)[:5]
    return [int.from_bytes(w[7:9] + w[5:7], "big", signed=True) for w in chain.writes if len(w) > 5 and w[:5] == head]


def _notes(svc) -> str:
    return " ".join(c for _t, c in svc.monitor.commands)


def _landing_rows(folder) -> list[dict]:
    with open(folder / sm.LANDING_LOG_FILE, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_a_landing_that_settles_in_the_window_finishes_without_a_lock():
    chain, svc = _setup()
    try:
        # as at 05:50 on 2026-11-19: -6 at motion complete, in the band a moment later (15 reads, inside the window)
        chain.land_off, chain.land_settle_reads = {"Y": [-6]}, 15
        ctx = _move(svc, X0, Y0 + 5000)
        assert ctx.status == "ok", ctx.message
        assert not svc.monitor.latched
        assert chain.values[("Y", "APOS")] == Y0 + 5000
        assert _targets(chain, "Y") == [Y0 + 5000]  # nothing re-sent: the drive converged by itself
        assert "landing settled" in _notes(svc)
    finally:
        svc.stop()


@pytest.mark.parametrize("target, via", [(Y0 + 5000, Y0 + 5000 - tm.CORRECT_APPROACH_COUNTS),
                                         (Y0 - 5000, Y0 - 5000 + tm.CORRECT_APPROACH_COUNTS)])
def test_a_landing_that_stays_off_is_reapproached_once_the_way_it_came(tmp_path, target, via):
    chain, svc = _setup(tmp_path)
    try:
        chain.land_off = {"Y": [-6 if target > Y0 else 6]}  # stuck short of the target, as at 65.6 mm on 2026-11-21
        chain.values[("Y", "IQ")] = -2152 if target < Y0 else 2152
        ctx = _move(svc, X0, target)
        assert ctx.status == "ok", ctx.message
        assert not svc.monitor.latched
        assert chain.values[("Y", "APOS")] == target
        # back the way it came (never past the target), then onto the target; X is never commanded
        assert _targets(chain, "Y") == [target, via, target]
        assert _targets(chain, "X") == []
        assert svc.monitor.reapproaches == {"Y": [round(tm.um_from_counts("Y", target) / 1000.0, 1)]}
        assert "re-approaching once" in _notes(svc) and not svc.monitor.warnings
        rows = _landing_rows(tmp_path)
        assert [r["action"] for r in rows] == ["re-approached"]
        assert rows[0]["axis"] == "Y" and int(rows[0]["target"]) == target
        assert abs(int(rows[0]["miss_after_settle"])) == 6 and rows[0]["iq"] == str(chain.values[("Y", "IQ")])
    finally:
        svc.stop()


def test_a_second_miss_locks_and_the_small_lock_can_be_released(tmp_path):
    chain, svc = _setup(tmp_path)
    try:
        chain.land_off = {"Y": [-6, 0, -7]}  # the move, the step back, the second approach
        ctx = _move(svc, X0, Y0 + 5000)
        assert ctx.status == "failed" and ctx.code == "readback"
        assert "missed again on the automatic re-approach (it first ended -6)" in ctx.message
        assert "Release stage lock" in ctx.message
        latch = svc.monitor.latch
        assert latch.rule == "landing_error" and svc.monitor.latch_drift() == -7
        assert latch.values["reapproach_of"]["first"] == -6
        assert svc.monitor.state()["releasable"] is True
        assert [r["action"] for r in _landing_rows(tmp_path)] == ["locked"]
        ok, msg = svc.release_latch("test")
        assert ok, msg
        assert not svc.monitor.latched
        ctx = _move(svc, X0, Y0)  # the stage moves again after the release
        assert ctx.status == "ok", ctx.message
    finally:
        svc.stop()


def test_a_large_miss_locks_without_a_reapproach():
    chain, svc = _setup()
    try:
        chain.land_off = {"Y": [-(tm.LANDING_RETRY_MAX_COUNTS + 5)]}
        ctx = _move(svc, X0, Y0 + 5000)
        assert ctx.status == "failed" and svc.monitor.latch.rule == "landing_error"
        assert f"more than {tm.LANDING_RETRY_MAX_COUNTS} counts off" in ctx.message
        assert _targets(chain, "Y") == [Y0 + 5000]
        assert svc.monitor.state()["releasable"] is False  # over the release bound: re-home or power cycle
    finally:
        svc.stop()


def test_a_drive_error_locks_without_a_reapproach():
    chain, svc = _setup()
    try:
        chain.land_off = {"Y": [-6]}
        chain.values[("Y", "MER")] = 0x0004
        ctx = _move(svc, X0, Y0 + 5000)
        assert ctx.status == "failed" and "MER 0x0004" in ctx.message
        assert _targets(chain, "Y") == [Y0 + 5000]
    finally:
        svc.stop()


def test_the_third_reapproach_of_an_axis_warns_without_locking():
    chain, svc = _setup()
    try:
        chain.land_off = {"Y": [-6, 0, 0] * sm.REAPPROACH_WARN_AFTER}
        for i in range(sm.REAPPROACH_WARN_AFTER):
            ctx = _move(svc, X0, Y0 + 1000 * (i + 1))
            assert ctx.status == "ok", ctx.message
        assert not svc.monitor.latched
        assert len(svc.monitor.reapproaches["Y"]) == sm.REAPPROACH_WARN_AFTER
        warning = svc.monitor.warnings["landing Y"]
        assert "3 automatic re-approaches" in warning and "check Y's guide" in warning
    finally:
        svc.stop()


def test_the_settle_window_writes_nothing():
    chain = SimulatedDriveChain({("X", "LPOSMAX"): 270000, ("Y", "LPOSMAX"): 200000, ("X", "APOS"): 20006},
                                allow_motion=True)
    svc = tm.TelemetryService(lambda: chain, "SIM", motion=True, sleep=lambda s: None)
    link = tm.DriveLink(chain)
    run = tm._Leg(Ctx(), ["X"], {"X": 20000}, {"X": 0, "Y": 0}, {"X": 10.0},
                  {"X": watchdog.LegWatch("X", 0, 20000, "fast", 0.0)})
    n = len(chain.writes)
    assert svc._await_landing(link, run, "X") == 20006  # stays off: the whole window, then the caller decides
    assert [w for w in chain.writes[n:] if w not in tp.ALLOWED_REQUESTS] == []


def test_landing_lock_values_give_the_miss_and_its_release_bound():
    m = sm.StageMonitor(None)
    m.trip("landing_error", "Y", "Y ended at 148835", {"start": 150584, "apos": 148835, "target": 148829})
    assert m.latch_drift() == 6 and m.state()["releasable"] is True  # the 2026-11-21 lock
    m = sm.StageMonitor(None)
    m.trip("landing_error", "Y", "Y ended at 53", {"start": 72603, "apos": 53, "target": 0})
    assert m.latch_drift() == 53 and m.state()["releasable"] is False  # the 2026-11-19 07:56 lock at home
    m = sm.StageMonitor(None)
    m.trip("wrong_direction", "Y", "away", {"start": 0, "apos": -150, "target": 15000})
    assert "cannot be released" in m._release_blocker()
