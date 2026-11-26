"""Health Phase 2 (ADR-0070): capability-scoped protection, "ask, then restrict" (the operator's choice, 2026-11-26).

Health proposes, the operator confirms, the SafetyManager's registry enforces (an interlock for commands, a guard for
runs and captures). A restriction confirmed while a run needs it waits for the run (or stops it at its next safe point,
keeping everything taken). Only a verification that passed, or the operator, lifts it. Simulator and emulated drives
only; a fake clock steps Health's time."""

from __future__ import annotations

import threading
import time

import pytest
from test_health_phase1_sim import (  # noqa: F401 - the Phase 1 fixtures and helpers
    Clock,
    commands,
    fast_following,
    session,
    step,
    wait_for,
    watch,
)

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.scan_plan import Coverage, ScanRoutine, plan_scan
from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.zstack import ZStackPlan
from openevos.health import rules as R
from openevos.health.inject import SimulatorFaultInjector
from openevos.health.model import CapabilityState
from openevos.health.service import HealthService
from openevos.instrument.profile import CameraRole
from openevos.safety.restrictions import (
    MULTI_POSITION,
    RESTRICTIONS_FILE,
    Restriction,
    RestrictionRefused,
    RestrictionRegistry,
)


def make(rid: str = "r1", *, caps=(MULTI_POSITION,), cmds=(), key="X", policy="p.test") -> Restriction:
    return Restriction(rid, policy, "1", "test restriction", "a test", tuple(caps), tuple(cmds),
                       {"key": key}, ("a test verification passes",), "2026-11-26T00:00:00+00:00")


@pytest.fixture
def health(session, tmp_path):  # noqa: F811
    clk = Clock()
    h = HealthService(session, journal_path=tmp_path / "health.sqlite3", now=clk,
                      rules=R.RuleEngine([*R.default_detectors()[:4], fast_following(), *R.default_detectors()[5:]]))
    h.clock = clk  # type: ignore[attr-defined]
    h.start(sampler=False)
    yield h
    h.stop()


def kinds(h) -> list[str]:
    assert wait_for(lambda: h.journal.stats()["queued"] == 0)
    return [r["kind"] for r in h.journal.query()]


# ====================================================================================================== registry
def test_a_run_is_refused_and_a_restriction_waits_for_a_running_run():
    reg = RestrictionRegistry()
    stopped = []
    with reg.run("Scan", {MULTI_POSITION}, stop=lambda: stopped.append(1)):
        r = reg.request(make())
        assert r.state == "pending" and reg.run_refusal({MULTI_POSITION}) == []  # the running scan is not cut short
        assert reg.stop_runs("r1") == ["Scan"] and stopped == [1]
    assert r.state == "active" and "the Scan run ended" in r.history[-1]["why"]
    with pytest.raises(RestrictionRefused, match="test restriction"), reg.run("Multi-D", {MULTI_POSITION, "x"}):
        pass
    with reg.run("Z-stack", {"focus_z"}):  # a run that needs something else is not refused
        pass
    assert reg.runs() == []


def test_release_needs_a_passed_verification_or_the_operator_and_both_are_recorded():
    reg = RestrictionRegistry()
    reg.request(make("a"))
    reg.request(make("b", key="Y"))
    with pytest.raises(PermissionError):
        reg.release("a", by="verification", verification={"passed": False})
    with pytest.raises(PermissionError):
        reg.release("a", by="health")
    a = reg.release("a", by="verification", verification={"passed": True, "summary": "ok"})
    b = reg.release("b", by="operator", note="inspected")
    assert (a.released_by, b.released_by) == ("verification", "operator") and not a.open and not b.open
    assert [h["what"] for h in a.history] == ["confirmed", "active", "released"]
    assert reg.run_refusal({MULTI_POSITION}) == []


def test_restrictions_survive_a_restart(tmp_path):
    path = tmp_path / RESTRICTIONS_FILE
    reg = RestrictionRegistry(path)
    with reg.run("Scan", {MULTI_POSITION}):
        reg.request(make("p"))  # pending when the app "stops"
        reg.request(make("q", caps=("data_saving",), key="data"))  # this one is active
        reg2 = RestrictionRegistry(path)
    assert {r.restriction_id: r.state for r in reg2.all()} == {"p": "active", "q": "active"}
    assert reg2.run_refusal({MULTI_POSITION}) and reg2.run_refusal({"data_saving"})


def test_the_interlock_refuses_named_commands_and_never_a_safe_direction_one(session):  # noqa: F811
    light = session.profile.illumination_device
    reg = session.core.safety.restrictions
    reg.request(make("l", caps=(), cmds=((light, "illumination.set"),)))
    try:
        t = session.controller.set_light("trans", 10.0, True)
        t.wait(5)
        rec = t.record
        assert rec.status.value == "rejected" and "restricted by Health policy p.test" in (rec.message or ""), rec
        off = session.controller.set_light("trans", 0.0, False)
        off.wait(5)
        assert off.record.status.value == "succeeded"  # making it safer is always allowed
    finally:
        reg.release("l", by="operator")
    t = session.controller.set_light("trans", 10.0, True)
    t.wait(5)
    assert t.record.status.value == "succeeded"


# ====================================================================================================== end to end
def test_stage_drift_watch_proposes_the_operator_confirms_runs_are_refused_and_verification_releases(
        session, health, tmp_path):  # noqa: F811
    h = health
    chain = session.telemetry[0].simulated_chain
    step(h, 40)
    h.capture_baseline(R.following_metric("X"))
    inj = SimulatorFaultInjector(session, h)
    inj.offset_at_rest("X", 2)
    step(h, 14)
    assert watch(h, "stage.following_error")
    (p,) = h.proposals()
    assert p.policy.policy_id == "xy.multi_position.following_error" and p.capabilities == (MULTI_POSITION,)
    assert h.latest.capability("multi_position").state is not CapabilityState.RESTRICTED  # proposed, not restricted
    assert "protection.proposed" in kinds(h)

    r = h.confirm(p.proposal_id)
    assert r.state == "active" and h.proposals() == ()
    step(h, 1)
    cap = h.latest.capability("multi_position")
    assert cap.state is CapabilityState.RESTRICTED and cap.authority == "SafetyManager"
    assert h.latest.capability("xy_positioning").state is not CapabilityState.RESTRICTED  # manual moves stay
    assert any("Health policy xy.multi_position" in x.source for x in h.latest.restrictions)

    n = len(commands(chain))
    routine = ScanRoutine(name="p2", vessel_id=session.vessel.vessel_id, objective_slot=2, channels=["cube1"],
                          regions=["D7"], coverage=Coverage.GRID, grid_rows=1, grid_cols=2, auto_fine=False)
    plan = plan_scan(routine, session.vessel, session.profile, session.stage_map)
    with pytest.raises(RestrictionRefused, match="Multi-position runs"):
        ScanRunner(session.capture, session.autofocus, session.vessel).run(plan)
    assert commands(chain)[n:] == []  # refused before anything moved

    inj.offset_at_rest("X", 0)
    step(h, 30)
    assert h.registry.get(r.restriction_id).active  # the WATCH resolving is not a release
    step(h, 300)
    rel = h.registry.get(r.restriction_id)
    assert not rel.open and rel.released_by == "verification" and rel.verification["passed"] is True
    step(h, 1)
    assert h.latest.capability("multi_position").state is not CapabilityState.RESTRICTED
    ks = kinds(h)
    assert {"protection.confirmed", "restriction.active", "restriction.released"} <= set(ks)


def test_a_restriction_confirmed_during_a_run_stops_it_at_a_safe_point_and_keeps_the_frames(session, health):  # noqa: F811
    h = health
    cam = session.profile.camera(CameraRole.MONO).device_id
    backend = session.core.devices.get(cam).backend
    inj = SimulatorFaultInjector(session, h)
    inj.camera_incomplete(cam, 4)
    backend.start_stream()
    try:
        step(h, 12, real=0.4)
    finally:
        backend.stop_stream("test")
        inj.camera_incomplete(cam, 0)
    (p,) = [x for x in h.proposals() if x.policy.policy_id == "camera.capture.incomplete_frames"]
    z = session.controller.focus_z()
    out = {}
    plan = ZStackPlan(channels=("cube1",), role=CameraRole.MONO, bottom_um=z + 1, top_um=z + 41, step_um=1,
                     confirmed_above_safe=True)
    th = threading.Thread(target=lambda: out.setdefault("r", session.capture.run_zstack(plan)))
    th.start()
    assert wait_for(lambda: h.registry.runs())
    h.proposals()  # the proposal now names the running stack
    step(h, 1, real=0.0)
    (p,) = [x for x in h.proposals() if x.policy.policy_id == "camera.capture.incomplete_frames"]
    assert p.runs == ("Z-stack",)
    time.sleep(0.5)
    r = h.confirm(p.proposal_id, stop_runs=True)
    assert r.state in ("pending", "active")
    th.join(30)
    res = out["r"]
    assert res.status == "stopped" and 0 < len(res.stack().images) < 41  # cut at a safe point, slices kept
    assert h.registry.get(r.restriction_id).active
    with pytest.raises(CaptureRejected, match="capture refused"):
        session.capture.capture("cube1", CameraRole.MONO)
    ks = kinds(h)
    assert "restriction.pending" in ks and "restriction.active" in ks


def test_storage_restricts_new_saving_runs_only_and_decline_and_acknowledge_restrict_nothing(session, health):  # noqa: F811
    h = health
    h.disk_period_s = 0.0
    SimulatorFaultInjector(session, h).disk_filling(20 * 10**9, 20e6)
    step(h, 8, dt=30.0, real=0.0)
    (p,) = [x for x in h.proposals() if x.policy.policy_id.startswith("storage.")]
    a = p.assessment
    h.acknowledge(a.assessment_id)
    h.decline(p.proposal_id)
    step(h, 2, dt=30.0, real=0.0)
    assert not [x for x in h.proposals() if x.policy.policy_id.startswith("storage.")]  # declined: not proposed again
    assert watch(h, "storage.time_to_full")  # acknowledging and declining clear nothing
    assert h.registry.open() == []
    ks = kinds(h)
    assert "assessment.acknowledged" in ks and "protection.declined" in ks

    h._declined.clear()  # a new WATCH would propose again; here the same one, for the test
    step(h, 1, dt=30.0, real=0.0)
    (p,) = [x for x in h.proposals() if x.policy.policy_id.startswith("storage.")]
    h.confirm(p.proposal_id)
    z = session.controller.focus_z()
    plan = ZStackPlan(channels=("cube1",), role=CameraRole.MONO, bottom_um=z + 1, top_um=z + 3, step_um=1,
                     confirmed_above_safe=True)
    with pytest.raises(RestrictionRefused, match="new runs that save data"):
        session.capture.run_zstack(plan)
    img = session.capture.capture("cube1", CameraRole.MONO)  # single captures stay available
    assert img is not None
    step(h, 1, dt=30.0, real=0.0)
    assert h.latest.capability("data_saving").state is CapabilityState.RESTRICTED


def test_a_recurring_restriction_escalates_to_service(session, health):  # noqa: F811
    h = health
    h.disk_period_s = 0.0
    SimulatorFaultInjector(session, h).disk_filling(20 * 10**9, 20e6)
    step(h, 8, dt=30.0, real=0.0)
    (p,) = [x for x in h.proposals() if x.policy.policy_id.startswith("storage.")]
    r = h.confirm(p.proposal_id)
    h.lift(r.restriction_id, "moved data off")
    step(h, 1, dt=30.0, real=0.0)
    (p,) = [x for x in h.proposals() if x.policy.policy_id.startswith("storage.")]
    assert p.recurring
    h.confirm(p.proposal_id)
    ks = kinds(h)
    assert "service.escalated" in ks and ks.count("protection.confirmed") == 2


def test_health_never_restricts_by_itself(session, health):  # noqa: F811
    """WATCHes with policies, many samples, no operator: nothing is restricted and no restriction event is written."""
    h = health
    h.disk_period_s = 0.0
    SimulatorFaultInjector(session, h).disk_filling(20 * 10**9, 20e6)
    step(h, 20, dt=30.0, real=0.0)
    assert h.proposals() and h.registry.open() == []
    assert all(c.state is not CapabilityState.RESTRICTED or c.authority == "SafetyManager" and
               not any("Health policy" in w for w in c.why) for c in h.latest.capabilities)
    assert not [k for k in kinds(h) if k.startswith("restriction.")]

