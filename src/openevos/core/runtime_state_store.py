"""Authoritative runtime state (handoff §16.5).

* Every write goes through this store under one lock; readers get immutable snapshots.
* Each snapshot carries a version that increases by one per change.
* Observations carry a per-source sequence number; stale or duplicate observations are rejected
  and counted, so a late callback can never roll state backwards.
* Requested state (set by the command router on acceptance) is separate from observed state
  (reported by the backend). The store never invents observations.
* Events are published while the lock is held, so subscribers see changes in version order.
  Subscribers must therefore not block (UI subscribers use a queued dispatcher).
"""

from __future__ import annotations

import threading

from openevos.core.event_bus import DeviceStateChanged, EventBus
from openevos.core.models import (
    Activity,
    BackendKind,
    Connection,
    DeviceKind,
    DeviceSnapshot,
    FirmwareMode,
    Health,
    Position,
    utcnow,
)

_UNSET = object()


class UnknownDeviceError(KeyError):
    pass


class RuntimeStateStore:
    def __init__(self, bus: EventBus):
        self._bus = bus
        self._lock = threading.RLock()
        self._state: dict[str, DeviceSnapshot] = {}
        self._last_seq: dict[tuple[str, str], int] = {}
        self.stale_observations = 0

    # ------------------------------------------------------------------ reads
    def get(self, device_id: str) -> DeviceSnapshot | None:
        with self._lock:
            return self._state.get(device_id)

    def snapshot_all(self) -> dict[str, DeviceSnapshot]:
        with self._lock:
            return dict(self._state)

    # ------------------------------------------------------------------ writes
    def create(self, device_id: str, kind: DeviceKind, backend_kind: BackendKind) -> DeviceSnapshot:
        with self._lock:
            if device_id in self._state:
                raise ValueError(f"device {device_id!r} already registered")
            snap = DeviceSnapshot(device_id=device_id, kind=kind, backend_kind=backend_kind)
            self._state[device_id] = snap
            self._bus.publish(DeviceStateChanged(device_id, None, snap, "registered"))
            return snap

    def remove(self, device_id: str) -> None:
        with self._lock:
            self._state.pop(device_id, None)
            for key in [k for k in self._last_seq if k[0] == device_id]:
                del self._last_seq[key]

    def observe(self, device_id: str, *, source: str, seq: int,
                connection: Connection | None = None, health: Health | None = None,
                activity: Activity | None = None, position: Position | None | object = _UNSET,
                firmware_mode: FirmwareMode | None = None,
                attributes: dict | None = None, reason: str | None = None) -> bool:
        """Apply a backend observation. Returns False (and changes nothing) if it is stale."""
        with self._lock:
            cur = self._require(device_id)
            key = (device_id, source)
            if seq <= self._last_seq.get(key, -1):
                self.stale_observations += 1
                return False
            self._last_seq[key] = seq
            update: dict = {"observed_at": utcnow(), "observed_source": source}
            if connection is not None:
                update["connection"] = connection
            if health is not None:
                update["health"] = health
            if activity is not None:
                update["activity"] = activity
            if firmware_mode is not None:
                update["firmware_mode"] = firmware_mode
            if position is not _UNSET:
                update["observed_position"] = position
            if attributes is not None:  # merged: a backend may report only the facts that changed
                merged = dict(cur.attributes)
                merged.update(attributes)
                update["attributes"] = tuple(sorted(merged.items()))
            self._commit(cur, update, reason)
            return True

    def set_requested(self, device_id: str, position: Position | None, *, reason: str) -> None:
        with self._lock:
            self._commit(self._require(device_id), {"requested_position": position}, reason)

    def set_recovery_required(self, device_id: str, required: bool, *, reason: str) -> None:
        with self._lock:
            cur = self._require(device_id)
            if cur.recovery_required != required:
                self._commit(cur, {"recovery_required": required, "note": reason}, reason)

    # ------------------------------------------------------------------ internals
    def _require(self, device_id: str) -> DeviceSnapshot:
        cur = self._state.get(device_id)
        if cur is None:
            raise UnknownDeviceError(device_id)
        return cur

    def _commit(self, cur: DeviceSnapshot, update: dict, reason: str | None) -> None:
        new = cur.model_copy(update={**update, "version": cur.version + 1})
        # model_copy skips validation; re-validate so a bad update can never enter the store
        new = DeviceSnapshot.model_validate(new.model_dump())
        self._state[cur.device_id] = new
        self._bus.publish(DeviceStateChanged(cur.device_id, cur, new, reason))
