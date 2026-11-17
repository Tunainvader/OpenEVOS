"""Shared helpers for the Phase 2.1 core tests."""

from __future__ import annotations

import threading

import pytest

from openevos.core.application import attach_simulated_instrument, build_core
from openevos.core.clock import ManualClock
from openevos.core.command_scheduler import ManualWorker
from openevos.core.device_manager import OwnershipRegistry
from openevos.core.event_bus import (
    CommandLifecycleEvent,
    DeviceStateChanged,
    HardwareFaultEvent,
    StorageFaultEvent,
)
from openevos.core.models import BackendKind, CommandOrigin, DeviceKind, MoveStageAbsolute, Position


def stage_move(x: float, y: float, **kw) -> MoveStageAbsolute:
    kw.setdefault("origin", CommandOrigin.TEST)
    kw.setdefault("unit", "um")
    kw.setdefault("frame", "sim-stage")
    kw.setdefault("timeout_s", 30.0)
    return MoveStageAbsolute(device_id="sim-stage", x=x, y=y, **kw)


class Recorder:
    """Collects bus events in one timeline (thread-safe)."""

    def __init__(self, bus):
        self._lock = threading.Lock()
        self.timeline: list[tuple[str, object]] = []
        self.events: list[object] = []
        for t in (CommandLifecycleEvent, DeviceStateChanged, HardwareFaultEvent, StorageFaultEvent):
            bus.subscribe(t, self._on)

    def _on(self, e) -> None:
        with self._lock:
            self.events.append(e)
            if isinstance(e, CommandLifecycleEvent):
                self.timeline.append(("cmd", e.status.value))
            elif isinstance(e, DeviceStateChanged):
                self.timeline.append((f"state:{e.device_id}", e.current.activity.value))

    def of(self, t):
        with self._lock:
            return [e for e in self.events if isinstance(e, t)]

    def statuses(self, command_id: str) -> list[str]:
        return [e.status.value for e in self.of(CommandLifecycleEvent) if e.command_id == command_id]


@pytest.fixture
def manual_core():
    """Deterministic core: ManualClock + ManualWorker + private ownership registry."""
    clock = ManualClock()
    core = build_core(clock, worker_factory=ManualWorker, ownership=OwnershipRegistry())
    rec = Recorder(core.bus)
    sim = attach_simulated_instrument(core, camera_size=(64, 48))
    yield core, sim, clock, rec
    core.shutdown()


class ScriptedBackend:
    """A backend that does nothing by itself; tests drive its execution context directly."""

    backend_kind = BackendKind.SIMULATED

    def __init__(self, device_id: str = "scripted", kind: DeviceKind = DeviceKind.XY_STAGE):
        self.device_id, self.kind = device_id, kind
        self.physical_id = f"sim:{device_id}"
        self.ctx = None
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.position = Position(axes=("x", "y"), values=(0.0, 0.0), unit="um", frame="sim-stage")
        self._seq = 0
        self._report = None

    def attach(self, reporter):
        self._report = reporter

    def report(self, **kw):
        self._seq += 1
        return self._report(seq=self._seq, **kw)

    def connect(self):
        from openevos.core.models import Activity, Connection, Health

        self.report(connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE, position=self.position)

    def disconnect(self):
        pass

    def start(self, command, ctx):
        self.ctx = ctx
        self.started.append(command.command_id)

    def cancel(self, command_id):
        self.cancelled.append(command_id)

    def read_position(self):
        return self.position

    def recover(self):
        pass
