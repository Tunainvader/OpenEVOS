"""Handoff §16.5 (state store) and §23.3 (safety)."""

import threading

import pytest
from core_helpers import stage_move
from pydantic import ValidationError

from openevos.core.event_bus import DeviceStateChanged, EventBus
from openevos.core.models import (
    Activity,
    BackendKind,
    CommandOrigin,
    Connection,
    DeviceKind,
    DeviceSnapshot,
    FirmwareMode,
    Health,
    Position,
    SetIllumination,
)
from openevos.core.runtime_state_store import RuntimeStateStore, UnknownDeviceError
from openevos.hal.status_model import activity_is_admissible
from openevos.inspector.evidence import EvidenceStage
from openevos.safety.capabilities import (
    AxisCapability,
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
)
from openevos.safety.command_validator import SafetyManager


# ------------------------------------------------------------------ state store
def test_versions_stale_and_requested_vs_observed():
    store = RuntimeStateStore(EventBus())
    store.create("s", DeviceKind.XY_STAGE, BackendKind.SIMULATED)
    p = Position(axes=("x", "y"), values=(1.0, 2.0), unit="um", frame="f")
    assert store.observe("s", source="b", seq=5, activity=Activity.IDLE, position=p)
    assert not store.observe("s", source="b", seq=5, activity=Activity.MOVING)  # duplicate
    assert not store.observe("s", source="b", seq=3, activity=Activity.MOVING)  # out of order
    assert store.observe("s", source="other", seq=1, health=Health.NORMAL)  # sequences are per source
    store.set_requested("s", Position(axes=("x", "y"), values=(9.0, 9.0), unit="um", frame="f"), reason="t")
    snap = store.get("s")
    assert snap.activity is Activity.IDLE and snap.observed_position == p and snap.requested_position.get("x") == 9.0
    assert snap.version == 3 and store.stale_observations == 2
    with pytest.raises(UnknownDeviceError):
        store.observe("nope", source="b", seq=1)


def test_concurrent_writers_keep_versions_consistent_and_ordered():
    bus = EventBus()
    store = RuntimeStateStore(bus)
    versions = []
    bus.subscribe(DeviceStateChanged, lambda e: versions.append(e.current.version))
    store.create("s", DeviceKind.XY_STAGE, BackendKind.SIMULATED)
    n_threads, n = 8, 300
    barrier = threading.Barrier(n_threads)

    def writer(k):
        barrier.wait()
        for i in range(1, n + 1):
            store.observe("s", source=f"src{k}", seq=i, activity=Activity.IDLE if i % 2 else Activity.MOVING)

    ts = [threading.Thread(target=writer, args=(k,)) for k in range(n_threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert store.get("s").version == n_threads * n
    assert versions == sorted(versions) and len(versions) == n_threads * n + 1


def test_activity_admissibility_links_runtime_and_evidence():
    assert not activity_is_admissible(Activity.MOVING, EvidenceStage.DETECTED)
    assert activity_is_admissible(Activity.MOVING, EvidenceStage.RESPONSIVE)
    assert activity_is_admissible(Activity.UNKNOWN, None)
    assert activity_is_admissible(Activity.MOVING, None, BackendKind.SIMULATED)


# ------------------------------------------------------------------ safety
def caps(**kw) -> DeviceCapabilities:
    axis_kw = kw.pop("axis_kw", {})
    base = {"device_id": "sim-stage", "backend_kind": BackendKind.SIMULATED, "source": CapabilitySource.SIMULATED,
                "supported_commands": frozenset({"stage.move_absolute"}),
                "axes": tuple(AxisCapability(**({"name": a, "limits": AxisLimits(minimum=-100, maximum=100, unit="um"),
                                              "coordinate_frame": "sim-stage", "calibration_valid": True,
                                              "readback_tolerance": 0.5} | axis_kw)) for a in ("x", "y"))}
    return DeviceCapabilities(**(base | kw))


def snap(**kw) -> DeviceSnapshot:
    base = {"device_id": "sim-stage", "kind": DeviceKind.XY_STAGE, "backend_kind": BackendKind.SIMULATED,
                "connection": Connection.CONNECTED, "health": Health.NORMAL, "activity": Activity.IDLE}
    return DeviceSnapshot(**(base | kw))


SM = SafetyManager()


def test_allowed_baseline():
    d = SM.validate(stage_move(10, -10), caps(), snap())
    assert d.allowed, d.reasons


@pytest.mark.parametrize("cmd_kw,caps_kw,snap_kw,expect", [
    ({}, None, {}, "not registered"),
    ({}, {}, {"connection": Connection.DISCONNECTED}, "not connected"),
    ({}, {}, {"connection": Connection.UNKNOWN}, "not connected"),
    ({}, {}, {"health": Health.FAULT}, "health is fault"),
    ({}, {}, {"health": Health.UNKNOWN}, "health is unknown"),
    ({}, {}, {"activity": Activity.MOVING}, "activity is moving"),
    ({}, {}, {"activity": Activity.UNKNOWN}, "activity is unknown"),
    ({}, {}, {"recovery_required": True}, "requires recovery"),
    ({"unit": "mm"}, {}, {}, "does not match capability unit"),
    ({"frame": "camera"}, {}, {}, "coordinate frame"),
    ({"x": 101}, {}, {}, "outside [-100.0, 100.0]"),
    ({"origin": CommandOrigin.AI}, {}, {}, "AI-originated"),
    ({}, {"supported_commands": frozenset()}, {}, "not supported"),
    ({}, {"axis_kw": {"limits": None}}, {}, "no known limits"),
    ({}, {"axis_kw": {"calibration_valid": False}}, {}, "calibration is not valid"),
    ({}, {"axis_kw": {"readback_tolerance": None}}, {}, "cannot be verified"),
    ({}, {}, {"kind": DeviceKind.FOCUS}, "does not apply"),
])
def test_fail_closed_rejections(cmd_kw, caps_kw, snap_kw, expect):
    d = SM.validate(stage_move(**({"x": 1, "y": 1} | cmd_kw)), None if caps_kw is None else caps(**caps_kw), snap(**snap_kw))
    assert not d.allowed and expect in d.message, d.message


def test_physical_device_needs_verified_capabilities_with_evidence():
    phys = snap(backend_kind=BackendKind.PHYSICAL)
    cand = caps(backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.CANDIDATE, evidence_ids=("pnp-abc",))
    d = SM.validate(stage_move(1, 1), cand, phys)
    assert not d.allowed and "'candidate', not verified" in d.message
    ok = caps(backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.VERIFIED, evidence_ids=("test-0001",))
    # ADR-0009: a physical device must also report that its application firmware is running.
    unknown_fw = SM.validate(stage_move(1, 1), ok, phys)
    assert not unknown_fw.allowed and "firmware mode is unknown" in unknown_fw.message
    running = snap(backend_kind=BackendKind.PHYSICAL, firmware_mode=FirmwareMode.APPLICATION)
    assert SM.validate(stage_move(1, 1), ok, running).allowed


def test_simulator_limits_cannot_be_applied_to_a_physical_backend():
    with pytest.raises(ValidationError, match="simulator capabilities"):
        caps(backend_kind=BackendKind.PHYSICAL)
    with pytest.raises(ValidationError, match="evidence IDs"):
        caps(backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.VERIFIED)
    with pytest.raises(ValidationError):
        AxisLimits(minimum=5, maximum=5, unit="um")
    with pytest.raises(ValidationError):
        AxisLimits(minimum=float("nan"), maximum=5, unit="um")


def test_illumination_channel_must_be_known():
    c = DeviceCapabilities(device_id="led", backend_kind=BackendKind.SIMULATED, source=CapabilitySource.SIMULATED,
                           supported_commands=frozenset({"illumination.set"}), illumination_channels=("dapi",))
    s = snap(device_id="led", kind=DeviceKind.ILLUMINATION)
    cmd = SetIllumination(device_id="led", origin=CommandOrigin.UI, channel="gfp", intensity_percent=10, enabled=True)
    assert "not a known channel" in SM.validate(cmd, c, s).message
