"""Health Phase 1 (ADR-0069), pure: robust statistics, versioned baselines and their invalidation, metric series,
the rule engine's persistence and hold, each rule's limit, event correlation, and how a rule's WATCH reaches the
report. No session, no device."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from openevos.core.models import Activity, BackendKind, DeviceKind, DeviceSnapshot
from openevos.health import rules as R
from openevos.health.assess import HealthView, LinkView, assess
from openevos.health.correlate import correlate
from openevos.health.model import (
    Assessment,
    HealthEvent,
    HealthState,
    Provenance,
    capabilities_affected_by,
)
from openevos.health.stats import (
    MIN_BASELINE_SAMPLES,
    BaselineStore,
    build_baseline,
    linear_slope,
    mad,
    median,
    quantile,
    robust_z,
)

T0 = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)
E0 = T0.timestamp()
FP = {"motor.stage.backend": "physical", "motor.stage.link": "technosoft:COM3"}


def ev(kind: str, at_s: float, *, sub: str = "illumination", sev: str = "error", did: str = "light",
       msg: str = "x", prov: Provenance = Provenance.MEASURED, eid: str | None = None) -> HealthEvent:
    t = T0 + timedelta(seconds=at_s)
    return HealthEvent(eid or f"e{kind}{at_s}{did}", kind, sub, sev, msg, t, t, prov, "test", did)


def snap(did: str, kind: DeviceKind, *, sim: bool = False, **attrs) -> DeviceSnapshot:
    return DeviceSnapshot(device_id=did, kind=kind, backend_kind=BackendKind.SIMULATED if sim else
                          BackendKind.PHYSICAL, activity=Activity.IDLE, observed_at=T0,
                          attributes=tuple(sorted(attrs.items())))


def inp(at_s: float = 0.0, *, events=(), series=None, baselines=None, view: HealthView | None = None,
        fp=None) -> R.RuleInput:
    now = T0 + timedelta(seconds=at_s)
    v = view or HealthView(now=now, devices={"illumination": ("light",), "xy_stage": ("motor.stage",),
                                             "mono_camera": ("cam.mono",)},
                           snapshots={"light": snap("light", DeviceKind.ILLUMINATION),
                                      "motor.stage": snap("motor.stage", DeviceKind.XY_STAGE),
                                      "cam.mono": snap("cam.mono", DeviceKind.CAMERA)})
    return R.RuleInput(now, v, tuple(events), series or R.MetricSeries(), baselines or BaselineStore(None),
                       FP if fp is None else fp)


# ====================================================================================================== statistics
def test_robust_statistics():
    xs = [1, 2, 2, 3, 100]
    assert median(xs) == 2 and median([1, 2, 3, 4]) == 2.5
    assert mad(xs) == 1  # the outlier does not move it
    assert quantile([0, 10], 0.5) == 5 and quantile(xs, 0.0) == 1 and quantile(xs, 1.0) == 100
    assert robust_z(5, 2, 0, floor=1.0) == 3.0  # a zero MAD is floored, never divides by zero
    assert linear_slope([(0, 10), (10, 0)]) == pytest.approx(-1.0)
    assert linear_slope([(1, 1)]) is None and linear_slope([(1, 1), (1, 2)]) is None
    with pytest.raises(ValueError):
        median([])


def test_baseline_needs_enough_samples_and_ignores_non_finite():
    with pytest.raises(ValueError, match="at least"):
        build_baseline("xy_stage.following_error.X", "idle", "counts", [1.0] * (MIN_BASELINE_SAMPLES - 1),
                       source="instrument", fingerprint=FP)
    b = build_baseline("xy_stage.following_error.X", "idle", "counts",
                       [0.0] * 20 + [1.0] * 20 + [float("nan")], source="instrument", fingerprint=FP, now=T0)
    assert b.n == 40 and b.median == 0.5 and b.qualification == "not-qualified" and not b.qualified
    assert b.ref() == {"id": "xy_stage.following_error.X@idle", "version": "1", "regime": "idle, not-qualified"}


def test_baselines_are_versioned_persisted_and_end_when_the_instrument_changes(tmp_path):
    path = tmp_path / "b.json"
    store = BaselineStore(path)

    def mk():
        return build_baseline("m.x", "idle", "counts", range(40), source="instrument", fingerprint=FP, now=T0)

    assert store.add(mk()).version == 1 and store.add(mk()).version == 2
    again = BaselineStore(path)  # reloaded from the file
    assert [b.version for b in again.all()] == [1, 2] and again.latest("m.x", "idle").version == 2
    assert again.valid("m.x", "idle", FP)[0] is not None
    b, why = again.valid("m.x", "idle", {**FP, "motor.stage.link": "technosoft:COM4"})
    assert b is None and "motor.stage.link changed" in why  # a changed port ends the baseline: no guess
    assert again.valid("m.y", "idle", FP) == (None, "no baseline")
    q = again.qualify("m.x", "idle", "operator")
    assert q.qualified and q.version == 2 and BaselineStore(path).latest("m.x", "idle").qualified


def test_a_simulator_baseline_can_never_be_qualified_and_a_bad_file_means_none(tmp_path):
    store = BaselineStore(None)
    store.add(build_baseline("m.x", "idle", "", range(40), source="simulator", fingerprint={}, now=T0))
    with pytest.raises(ValueError, match="simulator"):
        store.qualify("m.x", "idle", "operator")
    with pytest.raises(ValueError, match="name"):
        store.qualify("m.x", "idle", " ")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    s = BaselineStore(bad)
    assert s.all() == [] and s.load_error


# ====================================================================================================== series
def test_series_delta_counts_growth_across_a_counter_reset_and_drops_out_of_order():
    s = R.MetricSeries()
    for t, v in [(0, 10), (10, 12), (20, 15), (15, 99), (30, 2), (40, 4)]:  # (15, 99) is out of order: dropped
        s.record("c", t, v)
    assert [v for _, v in s.points("c")] == [10, 12, 15, 2, 4]
    assert s.delta("c", since=5) == 2 + 3 + 2 + 2  # 10->12->15, then a reset to 2 counts 2, then +2
    assert s.delta("c", since=40) is None  # nothing after the window start
    s.record("c", 40, 5)  # same time: a repeat, dropped
    assert s.last("c") == (40, 4.0, "")


# ====================================================================================================== engine
class Flaky(R.Detector):
    name, version, subsystem, title = "test.flaky", "1", "illumination", "flaky"
    raise_after, clear_after_s = 3, 10.0

    def __init__(self):
        self.on = True

    def check(self, inp):
        return [R.Hit("light", "illumination", "bad", "detail", ("e1",))] if self.on else []


class Broken(R.Detector):
    name, version, subsystem, title = "test.broken", "1", "host", "broken"

    def check(self, inp):
        raise RuntimeError("bug")


def test_engine_persistence_hold_and_a_broken_rule_does_not_stop_the_others():
    d = Flaky()
    eng = R.RuleEngine([Broken(), d])
    seen = []
    for i in range(3):
        active, statuses, tr = eng.evaluate(inp(i))
        seen += tr
        assert (len(active) == 1) == (i == 2)  # raised on the third consecutive hit, not before
    assert [w for w, _ in seen] == ["raised"]
    a = seen[0][1]
    assert a.state is HealthState.WATCH and a.provenance is Provenance.INFERRED and a.evidence_event_ids == ("e1",)
    assert a.affected_capabilities == capabilities_affected_by("illumination")
    assert statuses[0].active is False and "error: RuntimeError: bug" in statuses[0].note
    d.on = False
    active, _, tr = eng.evaluate(inp(5))
    assert len(active) == 1 and tr == []  # held: one quiet sample does not resolve it
    active, _, tr = eng.evaluate(inp(13))
    assert active == () and [w for w, _ in tr] == ["resolved"]


def test_assessment_json_follows_the_kit_schema():
    a = Assessment("id1", "comm.error_burst", "1", "light", "illumination", "t", "d", Provenance.DEMO, T0, T0,
                   evidence_event_ids=("e1",), affected_capabilities=("fluorescence_imaging",),
                   hypotheses=("cable",), checks=("look",))
    j = a.to_json()
    kit = {"schema_version", "assessment_id", "state", "severity", "title", "provenance", "confidence", "detector",
           "baseline", "evidence_event_ids", "affected_capabilities", "hypotheses", "recommended_operator_checks"}
    assert kit <= set(j) and j["confidence"] is None and j["provenance"] == "demo"
    assert j["hypotheses"] == [{"label": "cable", "confirmed": False}]
    json.dumps(j)


# ====================================================================================================== rules
def test_error_burst_needs_three_failures_on_one_device_within_a_minute():
    d = R.CommandErrorBurst()
    two = [ev("command.failed", 0), ev("command.timed_out", 30)]
    assert d.check(inp(40, events=two)) == []
    spread = two + [ev("command.failed", 90)]
    assert d.check(inp(95, events=spread)) == []  # the first fell out of the 60 s window
    other = two + [ev("command.failed", 40, did="cam.mono")]
    assert d.check(inp(45, events=other)) == []  # a different device
    hits = d.check(inp(45, events=two + [ev("command.failed", 40)]))
    assert len(hits) == 1 and hits[0].key == "light" and hits[0].subsystem == "illumination"
    assert len(hits[0].evidence) == 3 and "1 timed out" in hits[0].detail and "provisional" in hits[0].limit


def test_link_errors_rule():
    link = LinkView("usb:cam.mono", "Mono camera USB", ("cam.mono",))
    errs = tuple((T0 + timedelta(seconds=s), "cam.mono: dropped off USB") for s in (0, 100, 200))
    v = HealthView(now=T0 + timedelta(seconds=250), links=(link,), link_errors={link.key: errs})
    assert len(R.LinkErrors().check(inp(250, view=v))) == 1
    v2 = HealthView(now=T0 + timedelta(seconds=700), links=(link,), link_errors={link.key: errs})
    assert R.LinkErrors().check(inp(700, view=v2)) == []  # 10 min later only one is left


def test_reconnects_and_camera_incomplete_frames_rules():
    s = R.MetricSeries()
    for t, rc, good, bad in [(0, 0, 0, 0), (30, 1, 300, 10), (60, 2, 600, 40)]:
        s.record("backend.reconnects.cam.mono", E0 + t, rc)
        s.record("backend.frames_produced.cam.mono", E0 + t, good)
        s.record("backend.incomplete_frames.cam.mono", E0 + t, bad)
    i = inp(60, series=s)
    assert R.Reconnects().status(i) == (True, "")
    assert [h.key for h in R.Reconnects().check(i)] == ["cam.mono"]
    hits = R.CameraIncompleteFrames().check(i)
    assert len(hits) == 1 and hits[0].data["share"] == pytest.approx(40 / 640)
    quiet = R.MetricSeries()
    for t, good, bad in [(0, 0, 0), (60, 600, 5)]:
        quiet.record("backend.frames_produced.cam.mono", E0 + t, good)
        quiet.record("backend.incomplete_frames.cam.mono", E0 + t, bad)
    assert R.CameraIncompleteFrames().check(inp(60, series=quiet)) == []  # under 5 %
    few = R.MetricSeries()
    for t, good, bad in [(0, 0, 0), (60, 10, 5)]:
        few.record("backend.frames_produced.cam.mono", E0 + t, good)
        few.record("backend.incomplete_frames.cam.mono", E0 + t, bad)
    assert R.CameraIncompleteFrames().check(inp(60, series=few)) == []  # too few frames to judge
    assert R.CameraIncompleteFrames().status(inp(0))[0] is False  # no counter: the rule says so


def _baseline(store: BaselineStore, values, fp=FP) -> None:
    store.add(build_baseline(R.following_metric("X"), "idle", "counts", values, source="instrument",
                             fingerprint=fp, now=T0))


def test_following_error_rule_needs_a_valid_baseline_and_a_sustained_shift():
    store = BaselineStore(None)
    s = R.MetricSeries()
    for t in range(60):
        s.record(R.following_metric("X"), E0 + t, 2.0, "idle")
    d = R.FollowingError()
    on, note = d.status(inp(60, series=s, baselines=store))
    assert not on and "X: no baseline" in note
    _baseline(store, [0.0] * 30 + [1.0] * 10)
    assert d.status(inp(60, series=s, baselines=store)) == (True, "")
    hits = d.check(inp(60, series=s, baselines=store))
    assert len(hits) == 1 and hits[0].key == "X" and hits[0].baseline["id"] == "xy_stage.following_error.X@idle"
    # same offset but moving, not at rest: not counted
    moving = R.MetricSeries()
    for t in range(60):
        moving.record(R.following_metric("X"), E0 + t, 2.0, "moving")
    assert d.check(inp(60, series=moving, baselines=store)) == []
    # within the baseline's own spread: nothing
    calm = R.MetricSeries()
    for t in range(60):
        calm.record(R.following_metric("X"), E0 + t, 0.0 if t % 2 else 1.0, "idle")
    assert d.check(inp(60, series=calm, baselines=store)) == []
    # the instrument changed: the baseline ends and the rule says why instead of guessing
    on, note = d.status(inp(60, series=s, baselines=store, fp={**FP, "motor.stage.backend": "simulated"}))
    assert not on and "invalid" in note


def test_warning_recurrence_landing_reapproach_and_time_to_full():
    w = [ev("stage_monitor.warning", 0, sub="xy_stage", sev="warning", did=""),
         ev("stage_monitor.warning", 600, sub="xy_stage", sev="warning", did="")]
    assert len(R.WarningRecurrence().check(inp(700, events=w))) == 1
    assert R.WarningRecurrence().check(inp(700, events=w[1:])) == []
    s = R.MetricSeries()
    for t, n in [(0, 0), (100, 1), (200, 3), (300, 4)]:
        s.record("xy_stage.reapproaches.Y", E0 + t, n)
    assert [h.key for h in R.LandingReapproach().check(inp(300, series=s))] == ["Y"]
    disk = R.MetricSeries()
    for t in range(0, 301, 30):  # 50 GB falling 10 MB/s: full in ~83 min
        disk.record("storage.free_bytes.data", E0 + t, 50e9 - 10e6 * t)
    hits = R.StorageTimeToFull().check(inp(300, series=disk))
    assert len(hits) == 1 and 70 * 60 < hits[0].data["eta_s"] < 90 * 60
    slow = R.MetricSeries()
    for t in range(0, 301, 30):  # 1 MB/s from 50 GB: ~14 h
        slow.record("storage.free_bytes.data", E0 + t, 50e9 - 1e6 * t)
    assert R.StorageTimeToFull().check(inp(300, series=slow)) == []


# ====================================================================================================== report
def test_a_rule_watch_raises_its_subsystem_but_never_lowers_a_worse_state():
    a = Assessment("a", "comm.error_burst", "1", "light", "illumination", "3 failures", "d", Provenance.INFERRED,
                   T0, T0, affected_capabilities=capabilities_affected_by("illumination"))
    base = HealthView(now=T0, devices={"illumination": ("light",)},
                      snapshots={"light": snap("light", DeviceKind.ILLUMINATION)}, assessments=(a,))
    rep = assess(base)
    sub = rep.subsystem("illumination")
    assert sub.state is HealthState.WATCH and sub.reasons[0].startswith("rule comm.error_burst v1")
    assert rep.assessments == (a,)
    assert rep.capability("fluorescence_imaging").state.value == "WATCH"
    stray = Assessment("b", "x", "1", "k", "incubator", "t", "d", Provenance.INFERRED, T0, T0)
    assert assess(HealthView(now=T0, assessments=(stray,))).subsystem("incubator").monitored is False


# ====================================================================================================== correlation
def test_correlation_groups_related_events_and_keeps_unrelated_ones_apart():
    evs = [
        ev("device.condition", 0, sub="illumination", sev="info", msg="light registered"),
        ev("command.failed", 5, msg="LED fault 1"),
        ev("command.failed", 10, msg="LED fault 2"),
        ev("device.condition", 12, sub="communications", sev="warning", did="", msg="link error"),
        ev("storage.fault", 15, sub="storage", did="", msg="disk"),  # unrelated to illumination: its own incident
        ev("command.failed", 100, msg="LED fault 3"),  # 90 s later: a new incident
        ev("recorder.started", 11, sub="host", sev="info", did=""),  # bookkeeping: ignored
    ]
    incs = correlate(evs)
    assert [len(i.event_ids) for i in incs] == [4, 1, 1]
    first = incs[0]
    assert first.subsystems == ("communications", "illumination") and first.severity == "error"
    assert first.title.startswith("LED fault 1") and "(+2 related)" in first.title
    assert correlate(list(reversed(evs)))[0].incident_id == first.incident_id  # deterministic, order-free
    assert correlate([ev("device.condition", 0, sev="info")]) == []  # info alone opens nothing


def test_inventory_backend_counters_exist_in_the_backends():
    """Every backend:<name> the inventory reads is a counter Health samples and some backend really keeps."""
    import inspect

    from openevos.backends.flycapture2 import camera as fc_cam
    from openevos.backends.ledboard import illumination as led
    from openevos.backends.rig import camera as rig_cam
    from openevos.backends.ueye import camera as ueye_cam
    from openevos.health import inventory
    from openevos.health.service import BACKEND_COUNTERS

    sources = "".join(inspect.getsource(m) for m in (fc_cam, led, rig_cam, ueye_cam))
    names = {s.extract.split(":", 1)[1] for s in inventory.INVENTORY if s.extract.startswith("backend:")}
    assert names == set(BACKEND_COUNTERS)
    for n in names:
        assert f"self.{n} = 0" in sources, n
