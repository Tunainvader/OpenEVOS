"""Audit trail for device commands (plan §12.1 AuditEvent, §12.2 event logging).

Every command the gateway evaluates produces one `AuditEvent`, whether it was allowed, rejected,
failed or timed out. Events are kept in memory (bounded) for the UI and, optionally, appended to
a JSON-Lines file. v0.2 uses JSONL rather than the planned SQLite store: it is append-only,
human-readable and needs no schema migration while the event shape settles.

Parameters are logged after validation, in canonical units. Nothing secret is ever a command
parameter, so no redaction is needed at this layer.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openevos

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    timestamp: str  # UTC ISO-8601
    monotonic_s: float
    duration_ms: float
    actor: str
    owner: str | None
    mode: str
    command: str
    role: str | None
    device: str | None  # display name
    backend: str | None
    simulated: bool | None
    params: dict[str, Any]
    state_changing: bool
    risk: str | None
    decision: str  # "allowed" | "rejected"
    outcome: str  # "ok" | "rejected" | "failed" | "timeout" | "aborted"
    reason: str = ""
    checks: list[str] = field(default_factory=list)
    confirmed: bool = False
    software_version: str = openevos.__version__
    config_ref: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> Any:
    if hasattr(o, "value"):
        return o.value
    return repr(o)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditTrail:
    """Thread-safe, bounded in-memory trail with an optional append-only JSONL file."""

    def __init__(self, path: Path | None = None, maxlen: int = 20000):
        self._lock = threading.Lock()
        self._events: deque[AuditEvent] = deque(maxlen=maxlen)
        self._seq = 0
        self._listeners: list[Callable[[AuditEvent], None]] = []
        self.path = Path(path) if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)
            listeners = list(self._listeners)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(event.to_json() + "\n")
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                log.exception("audit listener failed")

    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def add_listener(self, cb: Callable[[AuditEvent], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[AuditEvent], None]) -> None:
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)
