"""Health Phase 0 (ADR-0068): state mapping, stale telemetry, provenance, capabilities, and the inventory against the
adapters. Pure functions on hand-made inputs: no session, no threads, no hardware."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openevos.backends.technosoft import stage_monitor
from openevos.core.models import (
    Activity,
    BackendKind,
    Connection,
    DeviceKind,
    DeviceSnapshot,
    FirmwareMode,
    Health,
    Position,
)
from openevos.health import assess as A
from openevos.health import inventory
from openevos.health.model import (
    CAPABILITIES,
    SUBSYSTEMS,
    CapabilityState,
    HealthState,
    Provenance,
    capabilities_affected_by,
    worst,
)

NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parents[1] / "src" / "openevos"


def snap(did: str = "motor.stage", *, kind: DeviceKind = DeviceKind.XY_STAGE, physical: bool = True,
         connection: Connection = Connection.CONNECTED, health: Health = Health.NORMAL,
         activity: Activity = Activity.IDLE, firmware: FirmwareMode = FirmwareMode.APPLICATION,
         recovery: bool = False, age_s: float | None = 0.5, attrs: dict | None = None,
         position: Position | None = None) -> DeviceSnapshot:
    return DeviceSnapshot(device_id=did, kind=kind, backend_kind=BackendKind.PHYSICAL if physical
                          else BackendKind.SIMULATED, connection=connection, health=health, activity=activity,
                          firmware_mode=firmware, recovery_required=recovery,
                          observed_at=None if age_s is None else NOW - timedelta(seconds=age_s),
                          observed_position=position, attributes=tuple(sorted((attrs or {}).items())))


def state_of(s: DeviceSnapshot | None, period: float | None = None) -> HealthState:
    findings, _stale = A.device_findings("dev", s, NOW, period)
    return worst(f.state for f in findings)


# ====================================================================================================== state mapping
@pytest.mark.parametrize(("kw", "expected"), [
    ({}, HealthState.HEALTHY),
    ({"health": Health.WARNING}, HealthState.WATCH),
    ({"health": Health.FAULT}, HealthState.FAULT),
    ({"recovery": True}, HealthState.FAULT),
    ({"health": Health.UNKNOWN}, HealthState.UNKNOWN),
    ({"connection": Connection.DISCONNECTED}, HealthState.OFFLINE),
    ({"connection": Connection.DISCONNECTED, "health": Health.FAULT}, HealthState.OFFLINE),
    ({"connection": Connection.CONNECTING}, HealthState.UNKNOWN),
    ({"connection": Connection.UNKNOWN}, HealthState.UNKNOWN),
    ({"firmware": FirmwareMode.BOOTLOADER, "connection": Connection.DISCONNECTED}, HealthState.FAULT),
    ({"attrs": {"homed": False}}, HealthState.RESTRICTED),
    ({"attrs": {"homed": True}}, HealthState.HEALTHY),
    ({"activity": Activity.MOVING}, HealthState.HEALTHY),  # busy is not unhealthy
])
def test_device_state_mapping(kw, expected):
    assert state_of(snap(**kw)) is expected


def test_unregistered_device_is_offline():
    assert state_of(None) is HealthState.OFFLINE


def test_drift_lock_warning_is_left_to_the_monitor():
    """The stage's WARNING that only carries the drift lock is not a second, weaker finding."""
    findings, _ = A.device_findings("motor.stage", snap(health=Health.WARNING, attrs={"drift_lock": True}), NOW)
    assert worst(f.state for f in findings) is HealthState.HEALTHY


def test_drift_rules_match_the_stage_monitor():
    assert A.DRIFT_RULES == stage_monitor.RELEASABLE_RULES


@pytest.mark.parametrize(("rule", "expected"), [
    ("idle_encoder_drift", HealthState.RESTRICTED), ("landing_error", HealthState.RESTRICTED),
    ("command_mismatch", HealthState.RESTRICTED), ("cross_axis_encoder_drift", HealthState.RESTRICTED),
    ("overcurrent_trip", HealthState.FAULT), ("oscillation_axis_off", HealthState.FAULT),
    ("comm_error_abort", HealthState.FAULT), ("power_stage_off", HealthState.FAULT),
    ("latch_file_unreadable", HealthState.FAULT),
])
def test_monitor_latch_mapping(rule, expected):
    m = A.MonitorView("COM3", {"latched": True, "rule": rule, "axis": "Y", "message": "m",
                               "since_utc": "2026-11-26T11:00:00+00:00"}, "locked")
    f = A.monitor_findings(m)
    assert [x.state for x in f] == [expected] and rule in f[0].text


def test_monitor_warning_and_clear():
    warn = A.MonitorView("COM3", {"latched": False, "warnings": ["Y homed 20 counts short"]}, "")
    assert [f.state for f in A.monitor_findings(warn)] == [HealthState.WATCH]
    ok = A.MonitorView("COM3", {"latched": False, "warnings": []}, "")
    assert [f.state for f in A.monitor_findings(ok)] == [HealthState.HEALTHY]


# ====================================================================================================== staleness
def test_stale_periodic_telemetry_becomes_unknown():
    fresh = snap(age_s=0.8)
    old = snap(age_s=30.0)
    assert state_of(fresh, period=1.0) is HealthState.HEALTHY
    findings, stale = A.device_findings("motor.stage", old, NOW, 1.0)
    assert stale and [f.state for f in findings] == [HealthState.UNKNOWN]
    assert "stale" in findings[0].text and "Last known: HEALTHY" in findings[0].text


def test_stale_fault_is_unknown_not_the_last_value():
    findings, stale = A.device_findings("motor.stage", snap(health=Health.FAULT, age_s=60.0), NOW, 1.0)
    assert stale and findings[0].state is HealthState.UNKNOWN and "Last known: FAULT" in findings[0].text


def test_never_observed_periodic_source_is_stale():
    findings, stale = A.device_findings("motor.stage", snap(age_s=None), NOW, 1.0)
    assert stale and findings[0].state is HealthState.UNKNOWN and "never observed" in findings[0].text


def test_stale_limit_scales_with_the_poll_period():
    assert A.stale_after(0.02) == A.STALE_MIN_S
    assert A.stale_after(5.0) == 25.0
    assert state_of(snap(age_s=20.0), period=5.0) is HealthState.HEALTHY
    assert state_of(snap(age_s=26.0), period=5.0) is HealthState.UNKNOWN


def test_event_driven_source_never_goes_stale():
    """A camera reports on change only: a quiet hour is not staleness."""
    assert state_of(snap("cam", kind=DeviceKind.CAMERA, age_s=3600.0), period=None) is HealthState.HEALTHY


def test_disconnected_device_is_offline_not_stale():
    assert state_of(snap(connection=Connection.DISCONNECTED, age_s=600.0), period=1.0) is HealthState.OFFLINE


def _view(**kw) -> A.HealthView:
    base: dict = {"now": NOW, "devices": {"xy_stage": ("motor.stage",)},
                  "snapshots": {"motor.stage": snap()}, "periods": {"motor.stage": 1.0}}
    base.update(kw)
    return A.HealthView(**base)


def test_stale_stage_keeps_the_authoritative_lock():
    m = A.MonitorView("COM3", {"latched": True, "rule": "idle_encoder_drift", "axis": "Y", "message": "drift"},
                      "locked")
    rep = A.assess(_view(snapshots={"motor.stage": snap(age_s=120.0)}, monitors=(m,)))
    xy = rep.subsystem("xy_stage")
    assert xy is not None and xy.state is HealthState.RESTRICTED and xy.stale
    assert any(f.state is HealthState.UNKNOWN and "stale" in f.text for f in xy.findings)


def test_zero_age_is_fresh_for_communications():
    """Regression: an observation taken this very instant (age 0.0) is fresh, not 'never observed'."""
    link = A.LinkView("technosoft:COM3", "Technosoft drives (COM3)", ("motor.stage",))
    rep = A.assess(_view(snapshots={"motor.stage": snap(age_s=0.0)}, links=(link,)))
    comm = rep.subsystem("communications")
    assert comm is not None and comm.state is HealthState.HEALTHY


def test_stale_link_is_unknown():
    link = A.LinkView("technosoft:COM3", "Technosoft drives (COM3)", ("motor.stage",))
    rep = A.assess(_view(snapshots={"motor.stage": snap(age_s=60.0)}, links=(link,)))
    assert rep.subsystem("communications").state is HealthState.UNKNOWN


def test_recent_link_errors_put_communications_in_watch():
    link = A.LinkView("technosoft:COM3", "Technosoft drives (COM3)", ("motor.stage",))
    errs = {"technosoft:COM3": ((NOW - timedelta(seconds=30), "motor.stage: telemetry failed: timeout"),)}
    old = {"technosoft:COM3": ((NOW - timedelta(hours=2), "motor.stage: telemetry failed: timeout"),)}
    assert A.assess(_view(links=(link,), link_errors=errs)).subsystem("communications").state is HealthState.WATCH
    assert A.assess(_view(links=(link,), link_errors=old)).subsystem("communications").state is HealthState.HEALTHY


# ====================================================================================================== overall
def test_unknown_is_never_reported_as_healthy():
    rep = A.assess(_view(devices={"xy_stage": ("motor.stage",), "focus": ("motor.focus",)},
                         snapshots={"motor.stage": snap(), "motor.focus": snap("motor.focus", kind=DeviceKind.FOCUS,
                                                                               health=Health.UNKNOWN)},
                         disks={"data": A.DiskView("C:/", 500 * 1024**3, 1000 * 1024**3, "", NOW)}))
    assert rep.overall is HealthState.UNKNOWN


def test_not_monitored_subsystems_stay_out_of_the_overall_state():
    rep = A.assess(_view(disks={"data": A.DiskView("C:/", 500 * 1024**3, 1000 * 1024**3, "", NOW)}))
    inc = rep.subsystem("incubator")
    assert inc is not None and not inc.monitored and inc.provenance is Provenance.UNAVAILABLE
    assert inc.state is HealthState.UNKNOWN  # displayed NOT MONITORED, never green
    assert rep.overall is HealthState.HEALTHY
    assert rep.counts.get("NOT MONITORED", 0) >= 1


def test_worst_order():
    assert worst([HealthState.WATCH, HealthState.UNKNOWN]) is HealthState.WATCH
    assert worst([HealthState.HEALTHY, HealthState.UNKNOWN]) is HealthState.UNKNOWN
    assert worst([HealthState.OFFLINE, HealthState.RESTRICTED]) is HealthState.RESTRICTED
    assert worst([HealthState.RESTRICTED, HealthState.FAULT]) is HealthState.FAULT
    assert worst([]) is HealthState.UNKNOWN


def test_failed_assessment_is_unknown():
    rep = A.failed_report(NOW, "boom")
    assert rep.overall is HealthState.UNKNOWN and rep.error == "boom" and not rep.subsystems


# ====================================================================================================== provenance
def test_provenance_labels():
    pos = Position(axes=("x", "y"), values=(1.0, 2.0), unit="um", frame="stage")
    attrs = {"counts": (10, 20), "commanded_counts": (12, 20), "homed": True}
    phys = A.device_metrics("xy_stage", ("motor.stage",), {"motor.stage": snap(attrs=attrs, position=pos)})
    sim = A.device_metrics("xy_stage", ("motor.stage",), {"motor.stage": snap(physical=False, attrs=attrs,
                                                                              position=pos)})
    by = {m.name: m for m in phys}
    assert by["Position X"].provenance is Provenance.MEASURED and by["Position X"].unit == "um"
    assert by["Encoder (APOS)"].provenance is Provenance.MEASURED
    fe = by["Following error |APOS - CPOS|"]
    assert fe.provenance is Provenance.DERIVED and fe.value == "2 / 0"
    assert {m.provenance for m in sim} == {Provenance.DEMO}  # simulator values are never Measured
    for m in phys + sim:
        assert m.source and m.observed_at is not None


def test_report_never_invents_inferred_values_and_marks_not_monitored():
    rep = A.assess(_view(disks={"data": A.DiskView("C:/", 500 * 1024**3, 1000 * 1024**3, "", NOW)}))
    for _key, m in A.iter_metrics(rep):
        assert m.provenance is not Provenance.INFERRED
        assert m.source
    xy = rep.subsystem("xy_stage")
    assert xy is not None and any("Motor current" in n for n in xy.not_monitored)
    assert all(not s.monitored for s in inventory.INVENTORY if s.provenance is Provenance.UNAVAILABLE)
    for s in inventory.INVENTORY:
        assert s.monitored == bool(s.extract), s.name  # every monitored row says how it is read; others say why not
        assert s.monitored or s.why, s.name


def test_inventory_attributes_are_published_by_the_adapters():
    """Every attr:<name> the inventory reads is a name the adapters really report (guards the inventory)."""
    code = "\n".join(p.read_text(encoding="utf-8") for p in (
        SRC / "backends/technosoft/telemetry.py", SRC / "backends/flycapture2/camera.py",
        SRC / "backends/ueye/camera.py", SRC / "backends/ledboard/illumination.py"))
    for s in inventory.INVENTORY:
        if s.extract.startswith("attr:"):
            name = s.extract.split(":", 1)[1]
            assert f'"{name}"' in code or f"{name}=" in code, f"{name} is not reported by any adapter"
    assert {s.subsystem for s in inventory.INVENTORY} == {d.key for d in SUBSYSTEMS}


# ====================================================================================================== storage, host
@pytest.mark.parametrize(("free_gb", "expected"), [(500, HealthState.HEALTHY), (5, HealthState.WATCH),
                                                   (1, HealthState.RESTRICTED)])
def test_storage_free_space(free_gb, expected):
    rep = A.assess(_view(disks={"data": A.DiskView("D:/data", free_gb * 1024**3, 1000 * 1024**3, "", NOW)}))
    assert rep.subsystem("storage").state is expected


def test_storage_unmeasured_or_stale_is_unknown():
    err = A.assess(_view(disks={"data": A.DiskView("X:/gone", None, None, "no such drive", NOW)}))
    assert err.subsystem("storage").state is HealthState.UNKNOWN
    stale = A.assess(_view(disks={"data": A.DiskView("D:/", 500 * 1024**3, 1000 * 1024**3, "",
                                                     NOW - timedelta(minutes=5))}))
    assert stale.subsystem("storage").state is HealthState.UNKNOWN


def test_journal_failure_is_watch_on_the_host_only():
    rep = A.assess(_view(host={"journal": {"state": "failed", "error": "disk full", "written": 3, "dropped": 9}},
                         disks={"data": A.DiskView("C:/", 500 * 1024**3, 1000 * 1024**3, "", NOW)}))
    assert rep.subsystem("host").state is HealthState.WATCH
    assert rep.subsystem("xy_stage").state is HealthState.HEALTHY


# ====================================================================================================== capabilities
def _caps(**kw):
    rep = A.assess(_view(disks={"data": A.DiskView("C:/", 500 * 1024**3, 1000 * 1024**3, "", NOW)}, **kw))
    return {c.key: c for c in rep.capabilities}, rep


def test_capability_refused_by_the_safety_manager_is_restricted_and_quoted():
    caps, rep = _caps(probe_refusals={"xy_stage": ("device health is warning, not normal: locked",)})
    c = caps["xy_positioning"]
    assert c.state is CapabilityState.RESTRICTED and c.authority == "SafetyManager"
    assert "device health is warning, not normal: locked" in c.why[0]
    assert caps["multi_position"].state is CapabilityState.RESTRICTED
    assert [r.source for r in rep.restrictions] == ["SafetyManager"]


@pytest.mark.parametrize(("snapkw", "expected"), [
    ({"connection": Connection.DISCONNECTED}, CapabilityState.UNAVAILABLE),
    ({"health": Health.FAULT}, CapabilityState.AT_RISK),
    ({"health": Health.WARNING}, CapabilityState.WATCH),
    ({"age_s": 120.0}, CapabilityState.UNKNOWN),
    ({}, CapabilityState.PERMITTED),
])
def test_capability_follows_its_dependencies(snapkw, expected):
    caps, _ = _caps(snapshots={"motor.stage": snap(**snapkw)})
    assert caps["xy_positioning"].state is expected


def test_capability_without_a_device_is_unavailable():
    caps, _ = _caps(probe_missing={"xy_stage": "motor.stage is not registered in this session"})
    assert caps["xy_positioning"].state is CapabilityState.UNAVAILABLE
    assert caps["environment_control"].state is CapabilityState.UNAVAILABLE  # no incubator: not monitored


def test_a_stage_lock_leaves_stationary_imaging_permitted():
    m = A.MonitorView("COM3", {"latched": True, "rule": "idle_encoder_drift", "axis": "Y", "message": "drift",
                               "releasable": True, "recovery": "power-cycle"}, "stage motion locked ...")
    cam = snap("cam", kind=DeviceKind.CAMERA)
    light = snap("led", kind=DeviceKind.ILLUMINATION)
    caps, rep = _caps(devices={"xy_stage": ("motor.stage",), "mono_camera": ("cam",), "illumination": ("led",)},
                      snapshots={"motor.stage": snap(health=Health.WARNING, attrs={"drift_lock": True}), "cam": cam,
                                 "led": light},
                      monitors=(m,), probe_refusals={"xy_stage": ("device health is warning: stage motion locked",)},
                      last_refusal={"motor.stage": ("rejected by the router: locked", NOW)})
    assert caps["xy_positioning"].state is CapabilityState.RESTRICTED
    assert caps["mono_imaging"].state is CapabilityState.PERMITTED
    (r,) = rep.restrictions
    assert r.source.startswith("StageMonitor") and r.refusal == "stage motion locked ..."
    assert set(r.capabilities) == {"xy_positioning", "multi_position"} and r.releasable is True
    assert r.last_router_refusal == "rejected by the router: locked"
    assert rep.subsystem("xy_stage").state is HealthState.RESTRICTED


def test_capability_map_is_consistent():
    keys = {d.key for d in SUBSYSTEMS}
    for c in CAPABILITIES:
        assert set(c.depends_on) <= keys and set(c.probes) <= set(c.depends_on)
    for d in SUBSYSTEMS:
        for cap in capabilities_affected_by(d.key):
            assert d.key in next(c for c in CAPABILITIES if c.key == cap).depends_on
