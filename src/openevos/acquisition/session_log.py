"""Session fault log on disk (operator, 2026-11-18: "every time I see some or other fault"; the camera's fault text
was lost when the app closed).

Appends one JSON line per event to `<config>/Logs/session-YYYYMMDD.jsonl`:
* hardware and storage faults (with the device's message and error code);
* commands that were rejected, failed or timed out (with the router's / drive's reason);
* device condition changes: connection, health, firmware mode, recovery flag, with the reason the backend gave
  (e.g. "camera stream restarted after: ...").
Position updates and successful commands are left out. Reads the event bus only; sends nothing anywhere.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from openevos.core.event_bus import (
    CommandLifecycleEvent,
    DeviceStateChanged,
    HardwareFaultEvent,
    StorageFaultEvent,
)
from openevos.core.models import utcnow

BAD = {"rejected", "failed", "timed_out"}
CONDITION = ("connection", "health", "firmware_mode", "recovery_required")


def _v(x):
    return x.value if hasattr(x, "value") else x


class SessionLog:
    def __init__(self, bus, folder: Path, *, now=utcnow):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.Lock()
        self.path = self.folder / f"session-{now():%Y%m%d}.jsonl"
        self._subs = [bus.subscribe(HardwareFaultEvent, self._fault), bus.subscribe(StorageFaultEvent, self._storage),
                      bus.subscribe(CommandLifecycleEvent, self._command),
                      bus.subscribe(DeviceStateChanged, self._device)]
        self.write("session", message="app started")

    def close(self) -> None:
        self.write("session", message="app closed")
        for s in self._subs:
            s.unsubscribe()
        self._subs.clear()

    def write(self, kind: str, *, timestamp: datetime | None = None, **data) -> None:
        row = {"t": (timestamp or self._now()).isoformat(timespec="milliseconds"), "kind": kind,
               **{k: v for k, v in data.items() if v not in (None, "", {})}}
        line = json.dumps(row, default=str, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _fault(self, e: HardwareFaultEvent) -> None:
        self.write("fault", timestamp=e.timestamp, device=e.device_id, message=e.message, severity=_v(e.severity),
                   code=e.error_code, command=e.command_id)

    def _storage(self, e: StorageFaultEvent) -> None:
        self.write("storage_fault", timestamp=e.timestamp, acquisition=e.acquisition_id, message=e.message)

    def _command(self, e: CommandLifecycleEvent) -> None:
        if _v(e.status) in BAD:
            self.write("command", timestamp=e.timestamp, device=e.device_id, status=_v(e.status), message=e.message,
                       command=e.command_id)

    def _device(self, e: DeviceStateChanged) -> None:
        prev, cur = e.previous, e.current
        changed = {f: {"from": _v(getattr(prev, f)) if prev else None, "to": _v(getattr(cur, f))}
                   for f in CONDITION if prev is None or getattr(prev, f) != getattr(cur, f)}
        if changed:
            self.write("device", timestamp=e.timestamp, device=e.device_id, changed=changed, reason=e.reason,
                       note=cur.note)
