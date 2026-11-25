"""Health recorder on the simulator (ADR-0068): the real StageMonitor lock and the SafetyManager's refusal drive the
report; probes never reach the router or the drives; the recorder's failure leaves the safety behaviour unchanged;
monitoring never blocks a publisher. Emulated drive chain only (`port="sim"`)."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.backends.technosoft import protocol as tp
from openevos.core.clock import ThreadClock
from openevos.core.event_bus import CommandLifecycleEvent
from openevos.core.models import CommandOrigin, CommandStatus, MoveStageAbsolute, utcnow
from openevos.health import service as hs
from openevos.health.journal import HealthJournal
from openevos.health.model import CapabilityState, HealthState, Provenance
from openevos.health.service import HealthService
from openevos.instrument.settings import AppSettings

SIM = {"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}}


def wait_for(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def session():
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), telemetry=SIM)
    assert wait_for(lambda: (s.core.store.get("motor.stage") is not None
                             and s.core.store.get("motor.stage").connection.value == "connected"))
    yield s
    s.shutdown()


@pytest.fixture
def health(session, tmp_path):
    h = HealthService(session, journal_path=tmp_path / "health.sqlite3").start(sampler=False)
    yield h
    h.stop()


def lock_stage(s, rule: str = "idle_encoder_drift") -> None:
    svc = s.telemetry[0]
    y = svc.simulated_chain.values[("Y", "APOS")]
    svc.monitor.trip(rule, "Y", "Y encoder changed -3 counts while the stage was at rest",
                     {"reference": y + 3, "apos": y})
    assert wait_for(lambda: s.core.store.get("motor.stage").attr("drift_lock") is True), "lock not on the snapshot"


READS = {v.frame for vs in tp.VARS.values() for v in vs.values()}


def commands(chain) -> list[bytes]:
    """Every frame sent to the emulated drives that is not a read request or the sync byte."""
    return [w for w in list(chain.writes) if w != bytes([0xFF]) and w not in READS]


def stage_move(s) -> MoveStageAbsolute:
    snap = s.core.store.get("motor.stage")
    x, y = snap.observed_position.values
    return MoveStageAbsolute(device_id="motor.stage", origin=CommandOrigin.UI, x=x + 500.0, y=y, unit="um",
                             frame="stage")


def test_simulator_report_is_demo_and_healthy(session, health):
    rep = health.sample()
    assert rep.error == "" and rep.overall is HealthState.HEALTHY
    xy = rep.subsystem("xy_stage")
    assert xy.provenance is Provenance.DEMO and xy.simulated and xy.expected_period_s == pytest.approx(0.02)
    assert "motor.stage" in xy.devices
    assert rep.subsystem("incubator").monitored is False
    assert rep.capability("xy_positioning").state is CapabilityState.PERMITTED
    assert rep.subsystem("communications").state is HealthState.HEALTHY


def test_the_real_lock_and_refusal_drive_the_restriction(session, health):
    assert health.sample().overall is HealthState.HEALTHY  # the baseline the transition is measured from
    lock_stage(session)
    mon = session.telemetry[0].monitor
    t = session.core.router.submit(stage_move(session))
    t.wait(5)
    assert t.status in (CommandStatus.REJECTED, CommandStatus.FAILED)
    rep = health.sample()
    assert rep.overall is HealthState.RESTRICTED
    assert rep.subsystem("xy_stage").state is HealthState.RESTRICTED
    cap = rep.capability("xy_positioning")
    assert cap.state is CapabilityState.RESTRICTED and cap.authority == "SafetyManager"
    assert any(mon.refusal() in w for w in cap.why)  # the SafetyManager's own words, which quote the monitor
    assert rep.capability("mono_imaging").state is CapabilityState.PERMITTED  # stationary imaging stays open
    (r,) = rep.restrictions
    assert r.refusal == mon.refusal() and r.rule == "idle_encoder_drift" and r.axis == "Y"
    assert r.releasable is True and "power-cycle" in r.recovery
    assert r.last_router_refusal and "drift monitor" in r.last_router_refusal
    kinds = [e.kind for e in health.recent_events()]
    assert "stage_monitor.latched" in kinds and "health.state_changed" in kinds
    assert any(k in kinds for k in ("command.rejected", "command.failed"))


def test_drive_fault_latch_is_fault(session, health):
    lock_stage(session, "overcurrent_trip")
    rep = health.sample()
    assert rep.subsystem("xy_stage").state is HealthState.FAULT
    assert rep.restrictions[0].releasable is False


def test_lock_clear_is_journaled_with_the_monitors_reason(session, health):
    lock_stage(session)
    health.sample()
    mon = session.telemetry[0].monitor
    mon.status({"X": {"IINITONCEDONE": 0}, "Y": {"IINITONCEDONE": 0}})  # the drives re-initialise (power-up homing)
    assert wait_for(lambda: not mon.latched)
    health.sample()
    cleared = [e for e in health.recent_events() if e.kind == "stage_monitor.cleared"]
    assert cleared and "re-initialisation" in cleared[0].message
    health.journal.close()
    assert any(r["kind"] == "stage_monitor.cleared" for r in health.journal.query())


def test_probes_never_reach_the_router_or_the_drives(session, health):
    seen: list[CommandLifecycleEvent] = []
    sub = session.core.bus.subscribe(CommandLifecycleEvent, seen.append)
    chain = session.telemetry[0].simulated_chain
    lock_stage(session)  # refusals too: still nothing is sent
    before = commands(chain)
    for _ in range(5):
        health.sample()
    sub.unsubscribe()
    assert seen == [] and commands(chain) == before  # only the poll's own read requests reached the drives
    assert not session.core.router.cancel_all()  # nothing queued or running


def test_stale_telemetry_on_the_simulator_reads_unknown(session, tmp_path):
    later = HealthService(session, now=lambda: utcnow() + timedelta(seconds=60)).start(sampler=False)
    try:
        rep = later.sample()
        xy = rep.subsystem("xy_stage")
        assert xy.stale and xy.state is HealthState.UNKNOWN
        assert rep.subsystem("communications").state is HealthState.UNKNOWN
        assert rep.capability("xy_positioning").state is CapabilityState.UNKNOWN
        assert rep.overall is not HealthState.HEALTHY
    finally:
        later.stop()


def test_recorder_failure_leaves_safety_unchanged(session, tmp_path, monkeypatch):
    lock_stage(session)
    core = session.core
    cmd = stage_move(session)
    rec = core.devices.get("motor.stage")

    def decide():
        return core.safety.validate(cmd, rec.capabilities, core.store.get("motor.stage"), core.store.snapshot_all())

    before = decide()
    assert not before.allowed
    t1 = core.router.submit(stage_move(session))
    t1.wait(5)

    blocker = tmp_path / "file"
    blocker.write_text("x")
    broken = HealthService(session, journal=HealthJournal(blocker / "j.sqlite3"))
    monkeypatch.setattr(hs, "assess", lambda view: (_ for _ in ()).throw(RuntimeError("assess broke")))
    monkeypatch.setattr(broken, "_emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("emit broke")))
    errors_before = core.bus.delivery_errors
    other: list = []
    sub = core.bus.subscribe(CommandLifecycleEvent, other.append)
    broken.start(sampler=False)
    try:
        rep = broken.sample()
        t2 = core.router.submit(stage_move(session))
        t2.wait(5)
        after = decide()
        assert after.allowed == before.allowed and after.reasons == before.reasons
        assert t2.status == t1.status
        assert other and other[-1].status == t2.status  # other subscribers still get every event
        assert core.bus.delivery_errors == errors_before  # the recorder swallowed its own errors
        assert broken.recorder_errors > 0 and "broke" in broken.last_error
        assert rep.overall is HealthState.UNKNOWN and "broke" in rep.error  # never the last green
        assert wait_for(lambda: broken.journal.state == "failed")
        assert session.telemetry[0].monitor.latched  # Health never touches the lock
    finally:
        sub.unsubscribe()
        broken.stop()


def test_monitoring_never_blocks_a_publisher(session, tmp_path):
    """The journal's disk stalls; 20,000 events from four threads still publish at once; the queue stays bounded."""
    import sqlite3

    gate = threading.Event()

    class Slow(sqlite3.Connection):
        def executemany(self, *a, **k):  # type: ignore[override]
            gate.wait(30)
            return super().executemany(*a, **k)

    j = HealthJournal(tmp_path / "j.sqlite3", max_queue=256,
                      connect=lambda p, timeout=5.0: sqlite3.connect(p, timeout=timeout, factory=Slow))
    h = HealthService(session, journal=j).start(sampler=False)
    bus = session.core.bus
    errors_before = bus.delivery_errors
    try:
        def flood(k: int) -> None:
            for i in range(5000):
                bus.publish(CommandLifecycleEvent(f"c{k}-{i}", "motor.stage", CommandStatus.REJECTED, None,
                                                  "test refusal"))

        t0 = time.perf_counter()
        threads = [threading.Thread(target=flood, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        elapsed = time.perf_counter() - t0
        assert elapsed < 10.0, f"publishing took {elapsed:.1f} s"
        assert j.high_water <= 256 and j.stats()["queued"] <= 256 and j.dropped > 0
        assert bus.delivery_errors == errors_before
        rep = h.sample()
        assert rep.subsystem("host").state is HealthState.WATCH  # the drops are shown, not hidden
        assert rep.subsystem("xy_stage").state is HealthState.HEALTHY  # and kept apart from the instrument
    finally:
        gate.set()
        h.stop()
    rows = j.query()
    assert any(r["kind"] == "journal.overflow" for r in rows)
    assert len([r for r in rows if r["kind"] != "journal.overflow"]) + j.dropped == j.offered
