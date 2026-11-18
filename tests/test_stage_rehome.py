"""Software re-home of X and Y during a stage lock (ADR-0058), on the simulated drive chain: refusals, the home
requests (X then Y, ESTATE 19 only through extra_writes), the monitor's clear rule, and the return X then Y."""

from __future__ import annotations

import time

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain, check_write

X0, Y0 = 60000, 40000  # where the stage was when it locked (counts)


def service(chain, evidence=None):
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True,
                              sleep=lambda s: time.sleep(0.001), monitor=sm.StageMonitor(evidence))
    svc.rehome_start_s, svc.rehome_timeout_s = 1.0, 2.0
    svc.start()
    deadline = time.monotonic() + 5
    while not svc.monitor.status_seen and time.monotonic() < deadline:
        time.sleep(0.02)
    return svc


def locked_chain(values=None):
    c = SimulatedDriveChain({("X", "LPOSMIN"): -1000, ("X", "LPOSMAX"): 269000, ("Y", "LPOSMIN"): -1500,
                             ("Y", "LPOSMAX"): 175600, ("X", "APOS"): X0, ("X", "CPOS"): X0, ("Y", "APOS"): Y0,
                             ("Y", "CPOS"): Y0, **(values or {})}, allow_motion=True)
    return c


def run_job(svc, timeout=15.0):
    job = svc.rehome_stage("test")
    assert not isinstance(job, str), job
    assert job.done.wait(timeout), job.stage
    return job


def writes_after(chain, n):
    return [w for w in chain.writes[n:] if w not in tp.ALLOWED_REQUESTS]


def trip_overcurrent(svc, chain):
    # the 2026-11-18 Y lock: over-current at rest, the drive switched Y off
    chain.values[("Y", "ISTATUSFLAGS")] = 0x1001
    chain.values[("Y", "SRL")] = 0x0400
    deadline = time.monotonic() + 5
    while not svc.monitor.latched and time.monotonic() < deadline:
        time.sleep(0.02)
    assert svc.monitor.latched and svc.monitor.latch.rule == "overcurrent_trip"


def test_home_frames_need_extra_writes():
    for ax, f in tp.XY_HOME_FRAMES.items():
        assert tp.home_frame(ax) == f and not tp.is_motion_write(f)
        with pytest.raises(tp.ProtocolRefused):
            check_write(f, allow_motion=True)
        check_write(f, allow_motion=True, extra=frozenset({f}))
    with pytest.raises(tp.ProtocolRefused):
        tp.home_frame("Z")


def test_monitor_rule_clears_only_on_good_reads():
    m = sm.StageMonitor(None)
    assert m.rehome_blocker() == "the stage is not locked"
    m.trip("overcurrent_trip", "Y", "test")
    assert m.rehome_blocker() == ""  # any lock may be re-homed
    good = {n: 0 for n in sm.REHOME_READS} | {"IINITONCEDONE": tp.INIT_DONE_MARKER, "HOMEDONE": 1, "SRL": 0xC400,
                                              "ISTATUSFLAGS": 0xD000, "SRH": 0x033F}
    bad = dict(good, ISTATUSFLAGS=0xD009)
    assert "interrupted" in m.rehome_done({"X": good, "Y": bad}) and m.latched
    assert "not at home" in m.rehome_done({"X": good, "Y": dict(good, APOS=40)}) and m.latched
    assert "reads missing" in m.rehome_done({"X": good}) and m.latched
    assert m.rehome_done({"X": good, "Y": good}, "test") == "" and not m.latched


def test_overcurrent_lock_rehomes_x_then_y_and_returns(tmp_path):
    chain = locked_chain()
    svc = service(chain, tmp_path)
    try:
        trip_overcurrent(svc, chain)
        assert "cannot be released" in svc.monitor._release_blocker()  # not drift-releasable: re-home is the way
        assert svc.rehome_blocker() == ""
        n = len(chain.writes)
        job = run_job(svc)
        assert job.ok, job.message
        assert job.homed and not svc.monitor.latched
        assert job.remembered == {"X": X0, "Y": Y0}
        w = writes_after(chain, n)
        homes = [f for f in w if f in tp.XY_HOME_FRAMES.values()]
        assert homes == [tp.XY_HOME_FRAMES["X"], tp.XY_HOME_FRAMES["Y"]]
        assert w.index(tp.XY_HOME_FRAMES["X"]) < w.index(tp.XY_HOME_FRAMES["Y"])
        # the return: X's move request strictly before Y's, after both homes, never both together
        mx, my = w.index(tp.move_request("X")), w.index(tp.move_request("Y"))
        assert w.index(tp.XY_HOME_FRAMES["Y"]) < mx < my
        assert (chain.values[("X", "APOS")], chain.values[("Y", "APOS")]) == (X0, Y0)
        assert chain.extra_writes == frozenset()  # registered only for the home request itself
        assert list(tmp_path.glob("stage-rehome-*-pass.json")) and list(tmp_path.glob("stage-latch-cleared-*.json"))
        assert "back at X" in job.message
    finally:
        svc.stop()


@pytest.mark.parametrize(("values", "why"), [
    ({("Y", "MER"): 0x0100}, "power cycle"),
    ({("Z", "APOS"): 20000, ("Z", "CPOS"): 20000}, "objective is not lowered"),
    ({("O", "ESTATEV"): 3}, "O is not idle"),
    ({("O", "ICURRENTPOSHF"): 0}, "turret is not on an objective"),
])
def test_refusals_send_nothing_and_keep_the_lock(values, why):
    chain = locked_chain()
    svc = service(chain)
    try:
        svc.monitor.trip("landing_error", "Y", "Y ended at 12")
        chain.values.update(values)
        n = len(chain.writes)
        job = run_job(svc)
        assert not job.ok and job.stage == "refused" and why in job.message
        assert writes_after(chain, n) == [] and svc.monitor.latched
    finally:
        svc.stop()


def test_not_offered_without_a_lock_or_motion():
    chain = locked_chain()
    svc = service(chain)
    try:
        assert svc.rehome_stage() == "the stage is not locked"
    finally:
        svc.stop()
    ro = tm.TelemetryService(lambda: chain, "SIM", motion=False)
    assert "motor control" in ro.rehome_blocker()


@pytest.mark.parametrize(("fault", "why"), [("overcurrent", "without completing"), ("stuck", "still homing"),
                                            ("ignore", "did not start")])
def test_homing_fault_stops_after_x_and_keeps_the_lock(fault, why):
    chain = locked_chain()
    svc = service(chain)
    try:
        svc.monitor.trip("landing_error", "Y", "Y ended at 12")
        chain.home_fault = fault
        n = len(chain.writes)
        job = run_job(svc)
        assert not job.ok and job.stage == "failed" and why in job.message
        assert svc.monitor.latched
        w = writes_after(chain, n)
        assert tp.XY_HOME_FRAMES["Y"] not in w and tp.move_request("X") not in w  # never Y, never a return
    finally:
        svc.stop()


def test_return_outside_limits_leaves_the_stage_home():
    chain = locked_chain({("X", "LPOSMAX"): 50000, ("X", "LPOSMIN"): 0})  # power-up limits, X was at 60000
    chain.values[("X", "APOS")] = chain.values[("X", "CPOS")] = 49000
    svc = service(chain)
    try:
        svc.monitor.trip("landing_error", "Y", "test")
        chain.values[("X", "APOS")] = 60000  # an unlikely state, to reach the guard
        job = run_job(svc)
        assert job.homed and not job.ok and job.stage == "homed, not returned"
        assert not svc.monitor.latched
        assert tp.move_request("X") not in chain.writes
    finally:
        svc.stop()
