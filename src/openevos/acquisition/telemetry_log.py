"""Always-on telemetry of every device on the instrument (operator, 2026-11-19: "always keep a telemetry on for the
positions and states of all items of the scope").

Appends one JSON line per record to `<config>/Logs/telemetry-YYYYMMDD.jsonl`:
* `state`: a device's state changed: position (with unit), activity, health, connection, firmware mode, recovery
  flag, note, the backend's reason and its small attributes (homed, turret position, light channel, ...). A change of
  position alone is written at most every MIN_POSITION_INTERVAL_S per device; any other change is written at once;
* `command`: every command lifecycle step (accepted, running, succeeded, failed, ...) with its message and result;
* `snapshot`: every SNAPSHOT_INTERVAL_S, the full state of every device, so quiet periods are on record too.

Reads the event bus and the state store only; sends nothing to any device. The session fault log (session_log.py)
stays as the short list of problems; this is the full record.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from openevos.core.event_bus import CommandLifecycleEvent, DeviceStateChanged
from openevos.core.models import utcnow

MIN_POSITION_INTERVAL_S = 0.25
SNAPSHOT_INTERVAL_S = 5.0
#: attributes kept in a record: scalars, and short tuples of scalars (e.g. limits); nothing bulky
MAX_ATTR_ITEMS = 8
CONDITION = ("connection", "health", "activity", "firmware_mode", "recovery_required", "note")


def _v(x):
    return x.value if hasattr(x, "value") else x


def _small(v: Any) -> bool:
    if isinstance(v, (bool, int, float, str)) or v is None:
        return not isinstance(v, str) or len(v) <= 200
    if isinstance(v, (tuple, list)) and len(v) <= MAX_ATTR_ITEMS:
        return all(_small(x) for x in v)
    return False


def describe(snap) -> dict:
    """One device's state as a flat, JSON-friendly dict."""
    pos = snap.observed_position
    out = {"device": snap.device_id, "device_kind": _v(snap.kind), "backend": _v(snap.backend_kind),
           "connection": _v(snap.connection), "health": _v(snap.health), "activity": _v(snap.activity),
           "firmware_mode": _v(snap.firmware_mode), "recovery_required": snap.recovery_required}
    if pos is not None:
        out["position"] = {a: round(float(v), 3) for a, v in zip(pos.axes, pos.values, strict=True)}
        out["unit"] = pos.unit
    if snap.requested_position is not None:
        rp = snap.requested_position
        out["requested"] = {a: round(float(v), 3) for a, v in zip(rp.axes, rp.values, strict=True)}
    if snap.note:
        out["note"] = snap.note
    attrs = {k: v for k, v in snap.attributes if _small(v)}
    if attrs:
        out["attributes"] = attrs
    return out


class TelemetryLog:
    def __init__(self, bus, store, folder: Path, *, now=utcnow, snapshot_interval_s: float = SNAPSHOT_INTERVAL_S,
                 min_position_interval_s: float = MIN_POSITION_INTERVAL_S, clock=None):
        import time

        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._clock = clock or time.monotonic
        self._store = store
        self._lock = threading.Lock()
        self._last_pos: dict[str, float] = {}
        self._min_pos = min_position_interval_s
        self.path = self.folder / f"telemetry-{now():%Y%m%d}.jsonl"
        self._subs = [bus.subscribe(DeviceStateChanged, self._device),
                      bus.subscribe(CommandLifecycleEvent, self._command)]
        self._stop = threading.Event()
        self._interval = snapshot_interval_s
        self.write("session", message="telemetry started")
        self.snapshot()
        self._thread = threading.Thread(target=self._run, name="telemetry-log", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        for s in self._subs:
            s.unsubscribe()
        self._subs.clear()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        self.snapshot()
        self.write("session", message="telemetry stopped")

    def write(self, kind: str, *, timestamp: datetime | None = None, **data) -> None:
        row = {"t": (timestamp or self._now()).isoformat(timespec="milliseconds"), "kind": kind,
               **{k: v for k, v in data.items() if v not in (None, "", {})}}
        line = json.dumps(row, default=str, ensure_ascii=False)
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # a full or removed disk never stops the instrument

    def snapshot(self) -> None:
        try:
            devices = [describe(s) for _, s in sorted(self._store.snapshot_all().items())]
        except Exception:  # noqa: BLE001 - a bad snapshot never stops the recorder
            return
        self.write("snapshot", devices=devices)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.snapshot()

    def _device(self, e: DeviceStateChanged) -> None:
        prev, cur = e.previous, e.current
        condition_changed = prev is None or any(getattr(prev, f) != getattr(cur, f) for f in CONDITION) \
            or prev.attributes != cur.attributes
        if not condition_changed:
            if prev is not None and prev.observed_position == cur.observed_position:
                return
            t = self._clock()
            if t - self._last_pos.get(e.device_id, -1e9) < self._min_pos:
                return  # a position-only update, rate-limited per device
            self._last_pos[e.device_id] = t
        self.write("state", timestamp=e.timestamp, reason=e.reason, **describe(cur))

    def _command(self, e: CommandLifecycleEvent) -> None:
        result = e.result.as_dict() if e.result is not None else None
        self.write("command", timestamp=e.timestamp, device=e.device_id, command=e.command_id, status=_v(e.status),
                   previous=_v(e.previous_status), message=e.message, result=result)
