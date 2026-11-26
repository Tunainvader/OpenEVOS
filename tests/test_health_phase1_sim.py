"""Health Phase 1 (ADR-0069) on the simulator: faults injected through the simulator's own knobs are detected, and
detected again when repeated; a clean session raises nothing (bounded false alarms); the rules command nothing; the
injector refuses a session with a physical part; a broken rule leaves the Phase 0 assessment standing; the diagnostic
bundle holds what the rules saw. Emulated drive chain only (`port="sim"`); a fake clock steps the rules' time."""

from __future__ import annotations

import json
import time
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.backends.technosoft import protocol as tp
from openevos.core.clock import ThreadClock
from openevos.core.models import BackendKind, DeviceKind, DeviceSnapshot, utcnow
from openevos.health import rules as R
from openevos.health.export import export_diagnostics
from openevos.health.inject import NotSimulated, SimulatorFaultInjector
from openevos.health.model import HealthState, Provenance
from openevos.health.service import HealthService
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings

SIM = {"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}}
READS = {v.frame for vs in tp.VARS.values() for v in vs.values()} | {bytes([0xFF])}


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


class Clock:
    def __init__(self):
        self.t = utcnow()

    def __call__(self):
        return self.t


def fast_following() -> R.FollowingError:
    d = R.FollowingError()
    d.window_s, d.min_points, d.raise_after, d.clear_after_s = 20.0, 8, 3, 5.0
    return d


@pytest.fixture
def health(session, tmp_path):
    clk = Clock()
    h = HealthService(session, journal_path=tmp_path / "health.sqlite3", now=clk,
                      rules=R.RuleEngine([*R.default_detectors()[:4], fast_following(), *R.default_detectors()[5:]]))
    h.clock = clk  # type: ignore[attr-defined]
    h.start(sampler=False)
    yield h
    h.stop()


def step(h, n: int, dt: float = 1.0, real: float = 0.03) -> None:
    for _ in range(n):
        time.sleep(real)  # the emulated drives are polled every 20 ms: each sample sees a fresh reading
        h.clock.t += timedelta(seconds=dt)
        h.sample()


def watch(h, detector: str):
    return [a for a in h.latest.assessments if a.detector == detector]


def commands(chain) -> list[bytes]:
    return [w for w in list(chain.writes) if w not in READS]


# ====================================================================================================== injector
def test_injector_refuses_any_session_with_a_physical_part():
    phys = DeviceSnapshot(device_id="cam", kind=DeviceKind.CAMERA, backend_kind=BackendKind.PHYSICAL)
    s = SimpleNamespace(core=SimpleNamespace(store=SimpleNamespace(snapshot_all=lambda: {"cam": phys})), telemetry=[])
    with pytest.raises(NotSimulated, match="cam"):
        SimulatorFaultInjector(s)
    real_port = SimpleNamespace(emulated=False, port_label="COM3")
    s2 = SimpleNamespace(core=SimpleNamespace(store=SimpleNamespace(snapshot_all=dict)), telemetry=[real_port])
    with pytest.raises(NotSimulated, match="COM3"):
        SimulatorFaultInjector(s2)


# ================================================================================================ stage drift trend
def test_offset_at_rest_is_detected_below_the_monitor_limit_repeatably_and_commands_nothing(session, health):
    h = health
    chain = session.telemetry[0].simulated_chain
    mon = session.telemetry[0].monitor
    step(h, 40)
    assert "no baseline" in next(d.note for d in h.latest.detectors if d.name == "stage.following_error")
    b = h.capture_baseline(R.following_metric("X"))
    assert b.source == "simulator" and b.qualification == "not-qualified" and b.median == 0
    assert b.fingerprint.get("motor.stage.backend") == "simulated"
    inj = SimulatorFaultInjector(session, h)
    n = len(commands(chain))
    for cycle in range(2):  # repeatable: the same fault is found each time
        inj.offset_at_rest("X", 2)  # 2 counts: under the drift monitor's own 3-count limit
        step(h, 14)
        found = watch(h, "stage.following_error")
        assert len(found) == 1, (cycle, h.latest.detectors)
        a = found[0]
        assert a.key == "X" and a.provenance is Provenance.DEMO and a.baseline["regime"] == "idle, not-qualified"
        assert h.latest.subsystem("xy_stage").state is HealthState.WATCH
        assert not mon.latched and not mon.warnings  # the monitor neither locked nor warned: Health saw it first
        inj.offset_at_rest("X", 0)
        step(h, 30)
        assert watch(h, "stage.following_error") == [], cycle
    assert commands(chain)[n:] == []  # sampling, rules and injection sent no frame to the drives
    kinds = [r["kind"] for r in h.journal.query()] if wait_for(lambda: h.journal.stats()["queued"] == 0) else []
    assert kinds.count("assessment.raised") >= 2 and kinds.count("assessment.resolved") >= 2
    assert "baseline.recorded" in kinds and "simulation.injected" in kinds


def test_a_clean_session_raises_nothing(session, health):
    """Bounded false alarms: moves, settling and a quiet stage under every rule raise no WATCH."""
    h = health
    step(h, 40)
    h.capture_baseline(R.following_metric("X"))
    h.capture_baseline(R.following_metric("Y"))
    for i in range(4):
        session.controller.move_stage(500.0 * (i + 1), 300.0 * (i % 2)).wait(10)
        step(h, 15)
    assert h.latest.assessments == ()
    assert all(d.note == "" or d.note.startswith("inactive") for d in h.latest.detectors)


# ================================================================================================== camera, storage
def test_incomplete_camera_frames_are_detected(session, health):
    h = health
    cam = session.profile.camera(CameraRole.MONO).device_id
    backend = session.core.devices.get(cam).backend
    inj = SimulatorFaultInjector(session, h)
    inj.camera_incomplete(cam, 4)  # every 4th frame incomplete: 25 %
    backend.start_stream()
    try:
        step(h, 12, real=0.4)  # the simulated camera streams ~8 frames/s
    finally:
        backend.stop_stream("test")
        inj.camera_incomplete(cam, 0)
    found = watch(h, "camera.incomplete_frames")
    assert len(found) == 1 and found[0].key == cam and found[0].provenance is Provenance.DEMO
    assert 0.15 < found[0].data["share"] < 0.35
    assert h.latest.subsystem("mono_camera").state is HealthState.WATCH
    metrics = {m.name: m for m in h.latest.subsystem("mono_camera").metrics}
    assert metrics["Incomplete frames skipped (session)"].value > 0  # published on the card, from the counter


def test_a_filling_data_drive_is_detected(session, health):
    h = health
    h.disk_period_s = 0.0
    SimulatorFaultInjector(session, h).disk_filling(20 * 10**9, 20e6)  # 20 GB at 20 MB/s: full in ~17 min
    step(h, 8, dt=30.0, real=0.0)
    found = watch(h, "storage.time_to_full")
    assert {a.key for a in found} >= {"data"}
    assert 10 * 60 < next(a for a in found if a.key == "data").data["eta_s"] < 20 * 60


# ================================================================================================== failure, export
def test_a_broken_rule_leaves_the_phase0_assessment_and_the_other_rules(session, tmp_path):
    class Broken(R.Detector):
        name, version, subsystem, title = "test.broken", "1", "host", "broken"

        def check(self, inp):
            raise RuntimeError("bug")

    h = HealthService(session, journal_path=tmp_path / "h.sqlite3",
                      rules=R.RuleEngine([Broken(), R.CommandErrorBurst()])).start(sampler=False)
    try:
        rep = h.sample()
        assert rep.error == "" and rep.overall is not HealthState.UNKNOWN
        st = {d.name: d for d in rep.detectors}
        assert not st["test.broken"].active and "RuntimeError" in st["test.broken"].note
        assert st["comm.error_burst"].active
        h.rules.evaluate = lambda inp: (_ for _ in ()).throw(RuntimeError("engine down"))  # type: ignore[method-assign]
        rep = h.sample()
        assert rep.error == "" and rep.assessments == () and h.recorder_errors >= 1
    finally:
        h.stop()


def test_the_diagnostic_bundle_holds_what_the_rules_saw(session, health, tmp_path):
    h = health
    light = session.rig.light
    SimulatorFaultInjector(session, h)
    for _ in range(3):
        light.fail_next = "simulated LED driver fault"
        session.controller.set_light("trans", 10.0, True).wait(5)
        light.recover()
    time.sleep(0.1)
    step(h, 2)
    assert watch(h, "comm.error_burst")
    assert wait_for(lambda: h.journal.stats()["queued"] == 0)
    path = export_diagnostics(h, tmp_path / "out")
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        assert names == {"manifest.json", "report.json", "assessments.json", "detectors.json", "incidents.json",
                         "events.jsonl", "baselines.json", "inventory.json", "series.json"}
        man = json.loads(z.read("manifest.json"))
        assert man["simulated"] is True and man["physical_parts"] == [] and man["events"] > 0
        ass = json.loads(z.read("assessments.json"))
        assert any(a["detector"]["name"] == "comm.error_burst" for a in ass["open"])
        assert any(r["kind"] == "assessment.raised" for r in ass["history"])
        incs = json.loads(z.read("incidents.json"))
        assert any("simulated LED driver fault" in i["title"] for i in incs)
        lines = [json.loads(x) for x in z.read("events.jsonl").decode().splitlines()]
        assert any(r["kind"] == "command.failed" for r in lines)
        inv = json.loads(z.read("inventory.json"))
        assert any(s["extract"] == "backend:frames_produced" and s["monitored"] for s in inv)
        assert "backend.frames_produced." + session.profile.camera(CameraRole.MONO).device_id in \
            json.loads(z.read("series.json"))
