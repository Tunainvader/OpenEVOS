"""Handoff §23.1 (models) and §23.2 (event bus)."""

import dataclasses
import threading
import time

import pytest
from core_helpers import stage_move
from pydantic import ValidationError

from openevos.core.event_bus import (
    CommandLifecycleEvent,
    DeviceStateChanged,
    EventBus,
    HardwareFaultEvent,
    Severity,
    StorageFaultEvent,
)
from openevos.core.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    Activity,
    BackendKind,
    CommandOrigin,
    CommandStatus,
    Connection,
    DeviceKind,
    DeviceSnapshot,
    Health,
    Position,
    SetCameraExposure,
    parse_command,
)
from openevos.core.runtime_state_store import RuntimeStateStore


# ------------------------------------------------------------------ models
def test_new_snapshot_is_unknown_on_every_axis():
    s = DeviceSnapshot(device_id="d", kind=DeviceKind.CAMERA, backend_kind=BackendKind.SIMULATED)
    assert (s.connection, s.health, s.activity) == (Connection.UNKNOWN, Health.UNKNOWN, Activity.UNKNOWN)


def test_invalid_enum_and_frozen():
    with pytest.raises(ValidationError):
        DeviceSnapshot(device_id="d", kind="toaster", backend_kind=BackendKind.SIMULATED)
    s = DeviceSnapshot(device_id="d", kind=DeviceKind.CAMERA, backend_kind=BackendKind.SIMULATED)
    with pytest.raises(ValidationError):
        s.health = Health.FAULT


def test_position_is_deeply_immutable():
    p = Position(axes=("x", "y"), values=(1.0, 2.0), unit="um", frame="f")
    d = p.as_dict()
    d["x"] = 99
    assert p.get("x") == 1.0
    assert isinstance(p.values, tuple)
    with pytest.raises(ValidationError):
        Position(axes=("x", "x"), values=(1.0, 2.0), unit="um", frame="f")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, False, "10", None])
def test_coordinates_must_be_finite_numbers_not_bool(bad):
    with pytest.raises(ValidationError):
        stage_move(bad, 0.0)


@pytest.mark.parametrize("bad", [float("nan"), True, 0, -1])
def test_exposure_validation(bad):
    with pytest.raises(ValidationError):
        SetCameraExposure(device_id="c", origin=CommandOrigin.TEST, exposure_ms=bad)


def test_ints_accepted_as_floats():
    assert stage_move(10, 20).x == 10.0


def test_unknown_command_kind_and_extra_fields_rejected():
    with pytest.raises(ValidationError):
        parse_command({"kind": "stage.teleport", "device_id": "s", "origin": "test"})
    with pytest.raises(ValidationError):
        parse_command({"kind": "stage.move_absolute", "device_id": "s", "origin": "test", "x": 1, "y": 1,
                       "unit": "um", "frame": "f", "speed": 99})
    assert parse_command({"kind": "stage.move_absolute", "device_id": "s", "origin": "test", "x": 1, "y": 2,
                          "unit": "um", "frame": "f"}).y == 2.0


def test_command_timestamps_and_ids_are_per_instance():
    a = stage_move(0, 0)
    time.sleep(0.002)
    b = stage_move(0, 0)
    assert a.created_at < b.created_at and a.command_id != b.command_id


def test_terminal_states_have_no_exit():
    for t in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[t] == frozenset()
    assert CommandStatus.SUCCEEDED not in ALLOWED_TRANSITIONS[CommandStatus.ACCEPTED]  # must pass through RUNNING


# ------------------------------------------------------------------ events
@pytest.mark.parametrize("cls", [DeviceStateChanged, CommandLifecycleEvent, HardwareFaultEvent, StorageFaultEvent])
def test_event_timestamp_uses_default_factory(cls):
    ts = next(f for f in dataclasses.fields(cls) if f.name == "timestamp")
    assert ts.default is dataclasses.MISSING and ts.default_factory is not dataclasses.MISSING


def test_event_timestamps_differ_per_instance():
    a = HardwareFaultEvent("d", "m", Severity.ERROR)
    time.sleep(0.002)
    b = HardwareFaultEvent("d", "m", Severity.ERROR)
    assert b.timestamp > a.timestamp


def test_events_are_frozen():
    e = HardwareFaultEvent("d", "m", Severity.ERROR)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.message = "x"


def test_subscribers_receive_typed_payloads_and_buses_are_isolated():
    bus1, bus2 = EventBus(), EventBus()
    got1, got2, everything = [], [], []
    bus1.subscribe(HardwareFaultEvent, got1.append)
    bus1.subscribe(object, everything.append)
    bus2.subscribe(HardwareFaultEvent, got2.append)
    e = HardwareFaultEvent("d", "m", Severity.ERROR)
    bus1.publish(e)
    bus1.publish(StorageFaultEvent("a", "m"))
    assert got1 == [e] and got2 == [] and len(everything) == 2


def test_failing_subscriber_does_not_break_publisher_or_others():
    errors = []
    bus = EventBus(error_handler=lambda exc, ev: errors.append(exc))
    got = []
    bus.subscribe(HardwareFaultEvent, lambda e: 1 / 0)
    bus.subscribe(HardwareFaultEvent, got.append)
    bus.publish(HardwareFaultEvent("d", "m", Severity.ERROR))
    assert len(got) == 1 and isinstance(errors[0], ZeroDivisionError) and bus.delivery_errors == 1


def test_unsubscribe():
    bus, got = EventBus(), []
    sub = bus.subscribe(HardwareFaultEvent, got.append)
    sub.unsubscribe()
    bus.publish(HardwareFaultEvent("d", "m", Severity.ERROR))
    assert got == []


def test_dispatcher_delivers_on_the_ui_thread():
    """Simulates a Qt queued connection: delivery happens on a dedicated 'main' thread."""
    import queue

    q: queue.Queue = queue.Queue()
    seen = []
    ui = threading.Thread(target=lambda: [q.get()() for _ in range(1)], name="fake-qt-main")
    ui.start()
    bus = EventBus()
    bus.subscribe(HardwareFaultEvent, lambda e: seen.append(threading.current_thread().name), dispatcher=q.put)
    worker = threading.Thread(target=lambda: bus.publish(HardwareFaultEvent("d", "m", Severity.ERROR)), name="worker")
    worker.start()
    worker.join()
    ui.join(2)
    assert seen == ["fake-qt-main"]


def test_state_events_carry_snapshots_not_live_objects():
    bus = EventBus()
    store = RuntimeStateStore(bus)
    got = []
    bus.subscribe(DeviceStateChanged, got.append)
    store.create("d", DeviceKind.FOCUS, BackendKind.SIMULATED)
    store.observe("d", source="b", seq=1, connection=Connection.CONNECTED)
    store.observe("d", source="b", seq=2, health=Health.FAULT)
    first_change = got[1]
    assert first_change.current.health is Health.UNKNOWN  # unaffected by the later update
    assert first_change.previous.version == 0 and first_change.current.version == 1
