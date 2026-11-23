"""Device registration, single-owner rule and backend wiring (handoff §16.6, §19).

* Application-scoped: construct one per application with explicit dependencies; no singleton.
* Single-owner rule: an `OwnershipRegistry` refuses a second claim on the same `physical_id`.
  The default registry is process-wide on purpose (two managers in one process must not both
  drive one stage). Cross-process exclusivity (e.g. vs. a Micro-Manager session) is enforced by
  the OS for serial ports/most SDKs and must be checked again by each physical backend.
* Each device gets its own serialized worker. The backend reports observations through a
  reporter bound to its device ID; it cannot write another device's state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openevos.core.command_scheduler import DeviceWorker, Worker
from openevos.core.event_bus import EventBus, HardwareFaultEvent, Severity
from openevos.core.models import new_id
from openevos.core.runtime_state_store import RuntimeStateStore
from openevos.hal.backend import DeviceBackend, Reporter
from openevos.safety.capabilities import DeviceCapabilities


class OwnershipError(RuntimeError):
    pass


class OwnershipRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[str, str] = {}

    def claim(self, physical_id: str, owner: str) -> None:
        with self._lock:
            holder = self._owners.get(physical_id)
            if holder is not None and holder != owner:
                raise OwnershipError(f"{physical_id} is already owned by {holder}")
            self._owners[physical_id] = owner

    def release(self, physical_id: str, owner: str) -> None:
        with self._lock:
            if self._owners.get(physical_id) == owner:
                del self._owners[physical_id]

    def owner_of(self, physical_id: str) -> str | None:
        with self._lock:
            return self._owners.get(physical_id)


PROCESS_OWNERSHIP = OwnershipRegistry()


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    backend: DeviceBackend
    capabilities: DeviceCapabilities
    owner: str


class DeviceManager:
    def __init__(self, store: RuntimeStateStore, bus: EventBus, *,
                 ownership: OwnershipRegistry = PROCESS_OWNERSHIP,
                 worker_factory: Callable[[str], Worker] = DeviceWorker,
                 owner_id: str | None = None):
        self.store, self.bus, self.ownership = store, bus, ownership
        self.owner_id = owner_id or f"openevos-{new_id()[:8]}"
        self._worker_factory = worker_factory
        self._lock = threading.Lock()
        self._records: dict[str, DeviceRecord] = {}
        self._workers: dict[str, Worker] = {}

    def register(self, backend: DeviceBackend, capabilities: DeviceCapabilities) -> DeviceRecord:
        if capabilities.device_id != backend.device_id:
            raise ValueError("capability record is for a different device")
        if capabilities.backend_kind is not backend.backend_kind:
            raise ValueError("capability record backend kind does not match the backend")
        with self._lock:
            if backend.device_id in self._records:
                raise ValueError(f"device {backend.device_id!r} already registered")
            self.ownership.claim(backend.physical_id, self.owner_id)
            try:
                self.store.create(backend.device_id, backend.kind, backend.backend_kind)
            except Exception:
                self.ownership.release(backend.physical_id, self.owner_id)
                raise
            rec = DeviceRecord(backend.device_id, backend, capabilities, self.owner_id)
            self._records[backend.device_id] = rec
            self._workers[backend.device_id] = self._worker_factory(backend.device_id)
        backend.attach(self._reporter_for(backend))
        return rec

    def _reporter_for(self, backend: DeviceBackend) -> Reporter:
        store, bus = self.store, self.bus
        device_id, source = backend.device_id, f"backend:{backend.device_id}"

        def report(*, seq: int, reason: str | None = None, fault: str | None = None, **fields: Any) -> bool:
            applied = store.observe(device_id, source=source, seq=seq, reason=reason, **fields)
            if applied and fault:
                bus.publish(HardwareFaultEvent(device_id, fault, Severity.ERROR, "DEVICE_FAULT"))
            return applied

        return report

    def connect(self, device_id: str) -> None:
        self._require(device_id).backend.connect()

    def get(self, device_id: str) -> DeviceRecord | None:
        with self._lock:
            return self._records.get(device_id)

    def worker(self, device_id: str) -> Worker:
        with self._lock:
            return self._workers[device_id]

    def device_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._records)

    def unregister(self, device_id: str) -> None:
        with self._lock:
            rec = self._records.pop(device_id, None)
            worker = self._workers.pop(device_id, None)
        if rec is None:
            return
        try:
            rec.backend.disconnect()
        finally:
            if worker is not None:
                worker.stop()
            self.ownership.release(rec.backend.physical_id, self.owner_id)
            self.store.remove(device_id)

    def shutdown(self) -> None:
        for device_id in self.device_ids():
            self.unregister(device_id)

    def _require(self, device_id: str) -> DeviceRecord:
        rec = self.get(device_id)
        if rec is None:
            raise KeyError(device_id)
        return rec
