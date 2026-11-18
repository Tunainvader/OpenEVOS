"""Operator release of a small drift lock (ADR-0057): only drift-type latches with a small recorded drift, and only
when fresh X/Y reads show both axes homed, idle, fault-free, powered and within 3 counts of their command."""

from __future__ import annotations

import time

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def ok_reads(x=1000, y=41129, dx=0, dy=0):
    base = {"IINITONCEDONE": tp.INIT_DONE_MARKER, "ESTATEV": 0, "SRH": tp.SRH_MOTION_COMPLETE_BIT, "SRL": 0x8000,
            "ISTATUSFLAGS": 0}
    return {"X": {**base, "APOS": x + dx, "CPOS": x}, "Y": {**base, "APOS": y + dy, "CPOS": y}}


def latched_on_idle_drift(drift: int):
    c = Clock()
    m = sm.StageMonitor(None, clock=c)
    m.status(ok_reads())
    c.t = 1.0
    m.fast(1000, 41129)  # reference taken after the settle time
    c.t = 1.1
    m.fast(1000, 41129 + drift)
    return m


def test_small_idle_drift_releases_after_good_reads_and_watches_again():
    m = latched_on_idle_drift(-3)  # the 2026-11-18 lock: Y 41129 -> 41126 at rest
    assert m.latched and m.latch.rule == "idle_encoder_drift" and m.latch_drift() == -3
    assert m.state()["releasable"] is True
    assert m.release(ok_reads(dy=-3)) == ""
    assert not m.latched and m.ref == {"X": None, "Y": None}  # fresh references: it keeps watching
    assert any("release" in c for _t, c in m.commands)


def test_release_refused_when_reads_are_not_safe():
    m = latched_on_idle_drift(-3)
    assert "would lock again" in m.release(ok_reads(dy=-5))  # encoder now 5 counts off its command
    r = ok_reads()
    r["Y"]["ISTATUSFLAGS"] = sm.ISF_OVERCURRENT
    assert "over-current" in m.release(r)
    r = ok_reads()
    r["X"]["IINITONCEDONE"] = 0
    assert "X not homed" in m.release(r)
    r = ok_reads()
    del r["Y"]["CPOS"]
    assert "reads missing" in m.release(r)
    assert m.latched  # still locked after every refusal


def test_large_drift_and_non_drift_latches_need_a_power_cycle():
    m = latched_on_idle_drift(-12)
    assert m.state()["releasable"] is False
    assert "over 10" in m.release(ok_reads(dy=-2)) and m.latched
    c = Clock()
    m = sm.StageMonitor(None, clock=c)
    m.trip("overcurrent_trip", "Y", "Y drive over-current trip recorded")
    assert "cannot be released" in m.release(ok_reads()) and m.latched


def test_release_through_the_service_on_the_simulated_chain():
    from openevos.backends.technosoft import telemetry as tm
    from openevos.backends.technosoft.transport import SimulatedDriveChain

    chain = SimulatedDriveChain(allow_motion=True)
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True, sleep=lambda s: time.sleep(0.001))
    svc.start()
    try:
        deadline = time.monotonic() + 5
        while not svc.monitor.status_seen and time.monotonic() < deadline:
            time.sleep(0.02)
        y = chain.values[("Y", "APOS")]
        svc.monitor.trip("idle_encoder_drift", "Y", "test", {"reference": y + 3, "apos": y})
        assert svc.monitor.latched
        n_writes = len(chain.writes)
        ok, msg = svc.release_latch("test")
        assert ok, msg
        assert not svc.monitor.latched
        writes = [w for w in chain.writes[n_writes:] if w not in tp.ALLOWED_REQUESTS]
        assert writes == []  # reads only
    finally:
        svc.stop()
