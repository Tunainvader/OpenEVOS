"""ADR-0067: bounded automatic recovery after an X/Y drive trip (operator 2026-11-25: "on such fault detection it will
automatically start repairing itself without user intervention"). On the drive emulator and the simulator only:
the trip classes (from the recorded trips), the service's ladder (re-home, slow test legs, return), its limits (one
attempt per axis per session, a trip during the ladder is a hard lock, a switched-off axis homed from where it
stands and recorded), the app side
(light off, objective down and back), the approach leg's staging landing, and the Multi-D and Scan runners."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.controller import LightState
from openevos.acquisition.stage_recovery import RecoveryOutcome, StageAutoRecovery
from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.core.models import CommandStatus, MoveStageAbsolute
from openevos.instrument.settings import AppSettings

STUCK = {"path": [0.0, 0.0], "flags": 0x0001, "off": False}  # over-current, never left its start, still powered


# ====================================================================================== trip classes (pure)
def _latch(axis, start, target, closest, apos, isf=0x1001):
    return sm.Latch("overcurrent_trip", axis, "test", "2026-11-25T14:28:54.347+00:00",
                    {"ISTATUSFLAGS": isf, "leg": {"start": start, "target": target, "closest": closest,
                                                  "apos": closest},
                     "drive": {axis: {"APOS": apos, "ISTATUSFLAGS": isf}}})


def test_the_recorded_trips_classify_as_the_fast_samples_show():
    # 2026-11-25 14:28, Multi-D B2 -> B5: X travelled 37,010 counts, then ran 36,319 back against its command
    kind, why = sm.classify_trip(_latch("X", 193352, 62052, 156342, 192661))
    assert kind == "ran_back" and "36319 counts back" in why
    # 2026-11-20 04:56: X moved 2,839 counts, then stopped dead
    assert sm.classify_trip(_latch("X", 176113, 0, 173274, 173274))[0] == "stopped_short"
    # 2026-11-18 17:42: Y never left 44,890
    assert sm.classify_trip(_latch("Y", 44890, 0, 44890, 44890))[0] == "stuck_at_start"
    # 2026-11-25 09:21: Y landed at 13,667 and oscillated (+7)
    assert sm.classify_trip(_latch("Y", 3, 13667, 13667, 13674, isf=0x3000))[0] == "landed"
    # the drives' own homing (ISTATUSFLAGS 0x1009: bits 0 and 3), e.g. 2026-11-21 17:16 Y at -12,025
    assert sm.classify_trip(_latch("Y", 0, 0, 0, -12025, isf=0x1009))[0] == "homing"
    # found by a status poll, no move running (2026-11-21 17:14, Y power stage off)
    at_rest = sm.Latch("power_stage_off", "Y", "test", "t", {"SRL": 0x4400})
    assert sm.classify_trip(at_rest)[0] == "at_rest"
    assert {"ran_back", "homing"}.isdisjoint(sm.RETRYABLE_TRIPS)


# ====================================================================================== the service's ladder
def _setup(x0, y0, evidence=None, values=None):
    chain = SimulatedDriveChain({("X", "LPOSMIN"): -1000, ("X", "LPOSMAX"): 269000, ("Y", "LPOSMIN"): -1500,
                                 ("Y", "LPOSMAX"): 175600, ("X", "APOS"): x0, ("X", "CPOS"): x0, ("Y", "APOS"): y0,
                                 ("Y", "CPOS"): y0, **(values or {})}, allow_motion=True)
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True,
                              sleep=lambda s: time.sleep(0.001), monitor=sm.StageMonitor(evidence))
    svc.rehome_start_s, svc.rehome_timeout_s = 1.0, 2.0
    svc.start()
    deadline = time.monotonic() + 5
    while not svc.monitor.status_seen and time.monotonic() < deadline:
        time.sleep(0.02)
    return chain, svc


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


def _move(svc, x_counts, y_counts) -> Ctx:
    ctx = Ctx()
    stage = next(b for b in svc.backends if b.device_id == "motor.stage")
    stage.start(MoveStageAbsolute(device_id="motor.stage", origin="test", x=tm.um_from_counts("X", x_counts),
                                  y=tm.um_from_counts("Y", y_counts), unit="um", frame="stage"), ctx)
    assert ctx.done.wait(30)
    return ctx


def _trip(svc, chain, axis, spec, x, y) -> Ctx:
    chain.trip_on_move[axis] = dict(spec)
    ctx = _move(svc, x, y)
    assert ctx.status == "failed" and svc.monitor.latched, ctx.message
    return ctx


def _recover(svc, timeout=30.0):
    job = svc.claim_auto_recovery("test")
    assert not isinstance(job, str), job
    svc.run_auto_recovery(job)
    assert job.done.wait(timeout), job.stage
    return job


def _home_writes(chain, n=0):
    return [w for w in chain.writes[n:] if w in tp.XY_HOME_FRAMES.values()]


def test_a_trip_that_leaves_the_axis_powered_is_recovered_tested_and_returned(tmp_path):
    x0, y0 = 60000, 40000
    chain, svc = _setup(x0, y0, tmp_path)
    try:
        ctx = _trip(svc, chain, "X", STUCK, 160000, y0)
        assert ctx.code == "overcurrent_trip"
        latch = svc.monitor.latch
        assert latch.values["leg"] == {"start": x0, "target": 160000, "closest": x0, "apos": x0}
        assert sm.classify_trip(latch)[0] == "stuck_at_start" and svc.auto_recovery_blocker() == ""
        n = len(chain.writes)
        job = _recover(svc)
        assert job.ok and job.stage == "recovered", job.message
        assert not svc.monitor.latched
        w = chain.writes[n:]
        # X then Y homed, each after its approach leg (both powered and further out than 3,000 counts)
        hx, hy = w.index(tp.XY_HOME_FRAMES["X"]), w.index(tp.XY_HOME_FRAMES["Y"])
        assert w.index(tp.move_target("X", tm.REHOME_APPROACH_TO_COUNTS)) < hx < hy
        # the test legs: X alone, at the slow preset, then its earlier (power-up) speed written back
        test = job.record["test"]
        assert test["axis"] == "X" and len(test["legs"]) == tm.RECOVERY_TEST_LEGS and all(g["ok"] for g in test["legs"])
        assert tp.speed_write("X", "slow") in w[hy:] and test["speed_restored"] == "fast"
        assert chain.values[("X", "CSPDCMD")] == tp.POWER_UP_SPEED_RAW
        assert not any(w_ == tp.speed_write("Y", "slow") for w_ in w)
        # back where the ladder found the stage
        assert chain.values[("X", "APOS")] == x0 and chain.values[("Y", "APOS")] == y0
        assert svc.auto_recovery_attempts.keys() == {"X"}
        rec = json.loads(next(tmp_path.glob("stage-recovery-*-recovered.json")).read_text(encoding="utf-8"))
        assert rec["trip_class"] == "stuck_at_start" and rec["lock"]["rule"] == "overcurrent_trip"
        steps = [s["step"] for s in rec["steps"]]
        assert steps[0] == "claimed" and "re-homed; lock cleared" in steps and steps[-1] == "recovered"
        assert any("automatic recovery" in c for _t, c in svc.monitor.commands)
        # a second trip of the same axis this session is a hard lock: no second attempt
        _trip(svc, chain, "X", STUCK, 120000, y0)
        assert "already had its automatic attempt" in svc.auto_recovery_blocker()
        assert isinstance(svc.claim_auto_recovery("test"), str)
    finally:
        svc.stop()


def test_an_axis_switched_off_far_from_home_is_homed_from_where_it_stands(tmp_path):
    # as every move trip on record: the drive switched the axis off 6-85 mm from home. Operator 2026-11-25 ("Allow
    # it"): the recovery homes it from there, as the operator's Re-home and every power-up homing do
    x0, y0 = 60000, 40000
    chain, svc = _setup(x0, y0, tmp_path)
    try:
        _trip(svc, chain, "X", dict(STUCK, off=True), 160000, y0)
        assert not chain.values[("X", "SRL")] & 0x8000
        assert svc.auto_recovery_blocker() == ""
        n = len(chain.writes)
        job = _recover(svc)
        assert job.ok and job.stage == "recovered", job.message
        assert not svc.monitor.latched and chain.values[("X", "SRL")] & 0x8000  # the home request's AXISON
        w = chain.writes[n:]
        hx, hy = w.index(tp.XY_HOME_FRAMES["X"]), w.index(tp.XY_HOME_FRAMES["Y"])
        # X had no approach leg (it cannot move switched off): its home request went out from 26.5 mm; Y, powered,
        # made its approach first
        assert tp.move_target("X", tm.REHOME_APPROACH_TO_COUNTS) not in w[:hx]
        assert w.index(tp.move_target("Y", tm.REHOME_APPROACH_TO_COUNTS)) < hx < hy
        assert job.record["homed_from_travel"] == {"X": "26.5 mm from home"}
        assert all(g["ok"] for g in job.record["test"]["legs"])
        assert chain.values[("X", "APOS")] == x0 and chain.values[("Y", "APOS")] == y0
        rec = json.loads(next(tmp_path.glob("stage-recovery-*-recovered.json")).read_text(encoding="utf-8"))
        step = next(s for s in rec["steps"] if s["step"] == "switched off by its drive: homed from where it stands")
        assert step["X"] == "26.5 mm from home" and "Y" not in step
        assert any("homed from where it stands" in c for _t, c in svc.monitor.commands)
        assert svc.auto_recovery_attempts.keys() == {"X"}  # used: a second trip of X needs the operator
    finally:
        svc.stop()


def test_a_trip_that_ran_back_against_its_command_is_never_retried():
    # the 2026-11-25 14:28 shape: half way to the target, then back near the start, current saturated
    chain, svc = _setup(60000, 40000)
    try:
        _trip(svc, chain, "X", {"path": [0.2, 0.4, 0.5, 0.5, 0.3, 0.05], "flags": 0x0001, "off": False},
              160000, 40000)
        kind, _why = sm.classify_trip(svc.monitor.latch)
        assert kind == "ran_back"
        assert "never retried automatically (ran_back" in svc.auto_recovery_blocker()
        assert isinstance(svc.claim_auto_recovery("test"), str) and svc.auto_recovery_attempts == {}
    finally:
        svc.stop()


def test_locks_that_are_not_drive_trips_are_left_to_the_operator():
    _chain, svc = _setup(60000, 40000)
    try:
        svc.monitor.trip("landing_error", "Y", "Y ended 6 off", {"apos": 6, "target": 0})
        assert "not a drive trip" in svc.auto_recovery_blocker()
    finally:
        svc.stop()


def test_a_trip_during_the_ladder_is_a_hard_lock():
    # both axes near home (no approach legs): Y trips on the ladder's return move after X recovered
    chain, svc = _setup(2000, 2500)
    try:
        _trip(svc, chain, "X", STUCK, 1500, 2500)
        chain.trip_on_move["Y"] = dict(STUCK)
        job = _recover(svc)
        assert not job.ok and job.stage == "escalated" and "the return move did not finish" in job.message
        assert job.record["test"]["legs"] and all(g["ok"] for g in job.record["test"]["legs"])
        latch = svc.monitor.latch
        assert latch is not None and latch.rule == "overcurrent_trip" and latch.axis == "Y"
        assert svc.auto_recovery_attempts.keys() == {"X"}  # Y never had its own attempt, and still gets none:
        assert "tripped during, or was not cured by, an automatic recovery" in svc.auto_recovery_blocker()
    finally:
        svc.stop()


def test_a_trip_on_the_test_move_keeps_the_stage_locked():
    chain, svc = _setup(2000, 2500)
    try:
        _trip(svc, chain, "X", STUCK, 1500, 2500)
        chain.trip_on_move["X"] = dict(STUCK)  # the next X move is the first slow test leg, after homing
        job = _recover(svc)
        assert not job.ok and "the test move of X failed" in job.message
        assert job.homed and job.record["test"]["legs"][0]["ok"] is False
        assert svc.monitor.latched and svc.monitor.latch.rule == "overcurrent_trip"
        assert svc.auto_recovery_blocker()
    finally:
        svc.stop()


def test_a_test_move_that_fails_without_a_trip_locks_the_stage_again():
    """The lock cleared by the re-home is set again when the ladder fails after it: never left unlocked."""
    chain, svc = _setup(2000, 2500)
    try:
        _trip(svc, chain, "X", STUCK, 1500, 2500)
        chain.values[("X", "LPOSMAX")] = 3000  # no room for three 1 mm legs either way from home
        chain.values[("X", "LPOSMIN")] = -10
        job = _recover(svc)
        assert not job.ok and "no room for the test legs" in job.message
        assert svc.monitor.latched and svc.monitor.latch.rule == tm.RECOVERY_FAILED_RULE
        assert tm.RECOVERY_FAILED_RULE not in sm.RELEASABLE_RULES and svc.auto_recovery_blocker()
    finally:
        svc.stop()


# ====================================================================================== the app side
class FakeTicket:
    def __init__(self, ok: bool, kind: str, message: str = ""):
        self.ok, self.kind, self.message = ok, kind, message

    def wait(self, timeout=None):
        return CommandStatus.SUCCEEDED if self.ok else CommandStatus.FAILED

    @property
    def record(self):
        return SimpleNamespace(command=SimpleNamespace(kind=self.kind), message=self.message)


class FakeCtl:
    """The controller as the recovery uses it: Z through the 'router' lands on the emulated drive."""

    def __init__(self, chain, *, z_fails=False):
        self.chain, self.z_fails, self.calls = chain, z_fails, []
        self.light = LightState("cube1", 40.0, True)

    def focus_z(self):
        return tm.um_from_counts("Z", self.chain.values[("Z", "APOS")])

    def move_focus(self, z, timeout_s=30.0, *, confirmed=False):
        self.calls.append(("focus", round(z), confirmed))
        if self.z_fails:
            return FakeTicket(False, "focus.move_absolute", "refused by the focus device")
        c = tm.counts_from_um("Z", z)
        self.chain.values[("Z", "APOS")] = self.chain.values[("Z", "CPOS")] = c
        return FakeTicket(True, "focus.move_absolute")

    def set_light(self, channel, intensity, enabled):
        self.calls.append(("light", channel, enabled))
        self.light = LightState(channel, intensity, enabled)
        return FakeTicket(True, "illumination.set")


def _session(svc, ctl, *, enabled=True):
    settings = AppSettings()
    settings.basic.stage_auto_recovery = enabled

    def lights_off():
        ctl.calls.append(("halt",))
        ctl.light = LightState(ctl.light.channel, ctl.light.intensity_percent, False)

    return SimpleNamespace(telemetry=[svc], settings=settings, controller=ctl, lights_off=lights_off)


def test_settings_default_on_and_round_trip(tmp_path):
    s = AppSettings()
    assert s.basic.stage_auto_recovery is True
    s.basic.stage_auto_recovery = False
    s.save(tmp_path / "s.json")
    assert AppSettings.load(tmp_path / "s.json").basic.stage_auto_recovery is False


def test_the_app_side_turns_the_light_off_lowers_the_objective_and_raises_it_back(tmp_path):
    z_counts = tm.counts_from_um("Z", 4000.0)
    chain, svc = _setup(2000, 2500, tmp_path, {("Z", "APOS"): z_counts, ("Z", "CPOS"): z_counts})
    try:
        _trip(svc, chain, "X", STUCK, 1500, 2500)
        ctl = FakeCtl(chain)
        logs: list[tuple[str, str]] = []
        rec = StageAutoRecovery(_session(svc, ctl), log=lambda lvl, txt: logs.append((lvl, txt)))
        assert rec.blocker() == ""
        out = rec.run("test move", confirmed_z=True)
        assert out.attempted and out.recovered and out.stage_ok, out.message
        assert ctl.calls[0] == ("halt",) and ctl.light.lit is None
        assert ("focus", 0, False) in ctl.calls and ctl.calls[-1] == ("focus", 4000, True)
        assert not svc.monitor.latched and chain.values[("Z", "APOS")] == z_counts
        assert any("Recovering automatically" in t for _l, t in logs)
        rec_file = Path(out.record)
        steps = [s["step"] for s in json.loads(rec_file.read_text(encoding="utf-8"))["steps"]]
        assert steps.index("light off") < steps.index("objective lowered") < steps.index("recovered") \
            < steps.index("objective raised back") < steps.index("handed back")
    finally:
        svc.stop()


def test_switched_off_in_settings_nothing_is_claimed():
    chain, svc = _setup(2000, 2500)
    try:
        _trip(svc, chain, "X", STUCK, 1500, 2500)
        ctl = FakeCtl(chain)
        rec = StageAutoRecovery(_session(svc, ctl, enabled=False))
        assert "switched off" in rec.blocker()
        out = rec.run("test")
        assert not out.attempted and svc.auto_recovery_attempts == {} and ctl.calls == []
    finally:
        svc.stop()


def test_if_the_objective_cannot_be_lowered_nothing_goes_to_x_or_y(tmp_path):
    z_counts = tm.counts_from_um("Z", 4000.0)
    chain, svc = _setup(2000, 2500, tmp_path, {("Z", "APOS"): z_counts, ("Z", "CPOS"): z_counts})
    try:
        _trip(svc, chain, "X", STUCK, 1500, 2500)
        n = len(chain.writes)
        out = StageAutoRecovery(_session(svc, FakeCtl(chain, z_fails=True))).run("test")
        assert out.attempted and not out.recovered and "lowering the objective" in out.message
        assert not [w for w in chain.writes[n:] if w not in tp.ALLOWED_REQUESTS]
        assert svc.monitor.latched and svc.auto_recovery_attempts.keys() == {"X"} and svc.auto_recovery_blocker()
        assert list(tmp_path.glob("stage-recovery-*-escalated.json"))
    finally:
        svc.stop()


def test_the_shell_recovers_a_trip_outside_a_run_by_itself(monkeypatch):
    """A drive trip found at rest (no run): the shell starts the recovery as a task and reports the result."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    def pump(app, s):
        end = time.monotonic() + s
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _w, title, text, *a, **k: shown.append((title, text)))
    monkeypatch.setattr(QMessageBox, "warning", lambda _w, title, text, *a, **k: shown.append((title, text)))
    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    w = MainWindow(SessionModel(s))
    w.show()
    try:
        pump(app, 0.5)
        svc = s.telemetry[0]
        svc.rehome_start_s, svc.rehome_timeout_s = 1.0, 3.0
        chain = svc.simulated_chain
        chain.values[("Y", "ISTATUSFLAGS")] |= 0x0001  # an over-current found by a status poll, Y still powered
        end = time.monotonic() + 60
        while not shown and time.monotonic() < end:
            pump(app, 0.1)
        assert shown and shown[0][0] == "Stage recovered", shown
        assert not svc.monitor.latched and svc.auto_recovery_attempts.keys() == {"Y"}
        assert any(f in chain.writes for f in tp.XY_HOME_FRAMES.values())
        assert any("Recovering automatically" in t for _ts, _lvl, t in w.model.log)
    finally:
        end = time.monotonic() + 30
        while w.model.tasks.busy and time.monotonic() < end:
            pump(app, 0.1)
        w.close()
        s.shutdown()
        w.deleteLater()
        pump(app, 0.1)


# ====================================================================================== approach leg (ADR-0058)
def test_an_approach_leg_accepts_a_small_miss_but_not_a_large_one(tmp_path):
    """2026-11-25 14:55: Y's approach before homing settled -6, one count outside the band, and the Re-home failed."""
    chain, svc = _setup(2000, 63054, tmp_path)
    try:
        svc.monitor.trip("overcurrent_trip", "X", "test")
        chain.land_off, chain.land_settle_reads = {"Y": [-6]}, None
        job = svc.rehome_stage("test")
        assert not isinstance(job, str) and job.done.wait(30)
        assert job.ok, job.message
        rows = (tmp_path / sm.LANDING_LOG_FILE).read_text(encoding="utf-8")
        assert "accepted (approach)" in rows
        svc.monitor.trip("overcurrent_trip", "X", "again")
        chain.values[("Y", "APOS")] = chain.values[("Y", "CPOS")] = 63054
        chain.land_off = {"Y": [-12]}
        job = svc.rehome_stage("test")
        assert not isinstance(job, str) and job.done.wait(30)
        assert not job.ok and "did not reach its approach position" in job.message
    finally:
        svc.stop()


# ====================================================================================== the runners
class FakeRecovery:
    def __init__(self, *, recovered=True, locked=False):
        self.recovered, self.locked, self.calls = recovered, locked, []

    def blocker(self):
        return ""

    def run(self, context, *, confirmed_z=False):
        self.calls.append(context)
        return RecoveryOutcome(True, self.recovered, "fake recovery", stage_ok=self.recovered)

    def stage_locked(self):
        return self.locked


def _flaky_moves(monkeypatch, ctl, fail_calls: set[int]):
    """move_stage fails on these call numbers (1-based), as a move refused or failed by a drive trip."""
    real, n = ctl.move_stage, {"i": 0}

    def move_stage(x, y, timeout_s=30.0):
        n["i"] += 1
        if n["i"] in fail_calls:
            return FakeTicket(False, "stage.move_absolute", "overcurrent_trip: X drive over-current trip")
        return real(x, y, timeout_s)

    monkeypatch.setattr(ctl, "move_stage", move_stage)


@pytest.fixture
def sim():
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock

    s = build_sim_session(ThreadClock(), fast=True, settings=AppSettings())
    time.sleep(0.3)
    yield s
    s.shutdown()


def _mda(sim, root, recovery):
    from openevos.acquisition.mda import (
        ChannelPlan,
        MultiDimPlan,
        PositionPlan,
        TimeAxisPlan,
        ZAxisPlan,
        preflight,
        summarize,
    )
    from openevos.acquisition.mda_runner import MultiDimRunner
    from openevos.imaging.mda_store import ExperimentReader, ExperimentWriter
    from openevos.instrument.profile import CameraRole

    xy = sim.controller.stage_xy()
    unit, frame = sim.controller.stage_frame()
    pos = tuple(PositionPlan(f"P{i + 1:04d}", f"Field {i + 1}", xy[0] + 400 * i, xy[1], unit, frame) for i in range(2))
    plan = MultiDimPlan(channels=(ChannelPlan("cube1", "DAPI", 20, 1, 50),), camera=CameraRole.MONO,
                        z=ZAxisPlan(enabled=True, below_um=1, above_um=1, interval_um=1.0),
                        time=TimeAxisPlan(False, 1, 10.0), positions=pos,
                        global_anchor_z_um=sim.controller.focus_z(), simulated=True, instrument=sim.profile.name)
    s = summarize(plan, sim.profile)
    pre = preflight(plan, sim, s, save_root=root)
    assert pre.ok, pre.errors
    res = MultiDimRunner(sim, ExperimentWriter(root), recovery=recovery).run(plan, s, pre)
    return res, ExperimentReader(Path(res.folder))


def test_multid_retries_the_position_once_after_a_recovery(sim, tmp_path, monkeypatch):
    from openevos.acquisition.mda_runner import MDAState

    _flaky_moves(monkeypatch, sim.controller, {1})
    rec = FakeRecovery()
    res, rd = _mda(sim, tmp_path, rec)
    assert res.state is MDAState.COMPLETED, res.message
    assert rec.calls == ["Multi-D Field 2, timepoint 1"]
    kinds = [e["kind"] for e in rd.events()]
    assert "stage_recovery" in kinds and any(e["kind"] == "move" and e.get("after_recovery") for e in rd.events())
    assert res.images_written == 2 * 3


def test_multid_skips_the_position_when_the_retry_fails_with_the_stage_unlocked(sim, tmp_path, monkeypatch):
    from openevos.acquisition.mda_runner import MDAState

    _flaky_moves(monkeypatch, sim.controller, {1, 2})
    res, rd = _mda(sim, tmp_path, FakeRecovery())
    assert res.state is MDAState.PARTIAL and "skipped after a stage recovery" in res.message
    assert any(e["kind"] == "position_skipped" for e in rd.events())
    assert res.images_written == 3  # Field 1 only
    assert [v["status"] for v in rd.manifest["volumes"]][1] == "not acquired"


def test_multid_ends_partial_when_the_stage_stays_locked_or_nothing_recovers(sim, tmp_path, monkeypatch):
    from openevos.acquisition.mda_runner import MDAState

    _flaky_moves(monkeypatch, sim.controller, {1, 2})
    res, _rd = _mda(sim, tmp_path / "a", FakeRecovery(locked=True))
    assert res.state is MDAState.PARTIAL and "stage is locked" in res.message and res.images_written == 3
    _flaky_moves(monkeypatch, sim.controller, {1})
    res, _rd = _mda(sim, tmp_path / "b", FakeRecovery(recovered=False))
    assert res.state is MDAState.PARTIAL and "did not succeed: fake recovery" in res.message
    _flaky_moves(monkeypatch, sim.controller, {1})
    res, _rd = _mda(sim, tmp_path / "c", None)  # no recovery: as before ADR-0067
    assert res.state is MDAState.PARTIAL and "overcurrent_trip" in res.message


def test_scan_retries_a_tile_once_and_skips_it_if_the_retry_fails(sim, monkeypatch):
    from openevos.acquisition.scan_plan import Coverage, ScanRoutine, plan_scan
    from openevos.acquisition.scan_runner import ScanRunner

    routine = ScanRoutine(name="rec", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1"],
                          regions=["D7"], coverage=Coverage.GRID, grid_rows=1, grid_cols=3, auto_fine=False)
    plan = plan_scan(routine, sim.vessel, sim.profile, sim.stage_map)
    _flaky_moves(monkeypatch, sim.controller, {2, 3, 4})  # tile 2 twice (skipped), tile 3 once (retried, taken)
    rec = FakeRecovery()
    res = ScanRunner(sim.capture, sim.autofocus, sim.vessel, recovery=rec).run(plan)
    assert [t.index for t, _imgs in res.images] == [0, 2]
    assert not res.completed and "1 skipped after a stage recovery" in res.message
    assert rec.calls == ["scan tile 2", "scan tile 3"]
    assert any("tile skipped" in e for _i, e in res.stage_events)
