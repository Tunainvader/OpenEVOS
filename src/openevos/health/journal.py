"""Local SQLite event journal behind a bounded, non-blocking queue (ADR-0068 §3).

* `offer()` never blocks and never raises: it puts the event on a bounded queue (`queue.Queue.put_nowait`) or, when
  the queue is full, the journal has failed or it is closed, counts the event as dropped and returns False. EventBus
  subscribers call it on the publisher's thread (a device worker, or the RuntimeStateStore with its lock held), so
  nothing here may wait for the disk.
* One writer thread owns the SQLite connection: it drains the queue in batches (one transaction each). When it has
  caught up after drops, it writes one `journal.overflow` row saying how many events were lost, so a gap is on record
  rather than silent.
* If SQLite fails (cannot open, disk full, locked), the journal goes to state "failed", records the error, empties its
  queue and stops persisting. Events are still counted. Nothing outside the Health page changes: the journal is not on
  any command or safety path.
* `path=None`: memory only (state "off"): events go nowhere and are not counted as dropped.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openevos.core.models import new_id, utcnow
from openevos.health.model import HealthEvent, Provenance

MAX_QUEUE = 4096
BATCH = 256
RETENTION_DAYS = 180
JOURNAL_FILE = "health-journal.sqlite3"
SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    provenance TEXT NOT NULL,
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_observed ON events (observed_at);
CREATE INDEX IF NOT EXISTS events_subsystem ON events (subsystem, observed_at);
"""
_INSERT = ("INSERT INTO events (event_id, observed_at, received_at, subsystem, kind, severity, device_id, message, "
           "provenance, source, session_id, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_STOP = object()


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _row(ev: HealthEvent, session_id: str) -> tuple[Any, ...]:
    return (ev.event_id, _iso(ev.observed_at), _iso(ev.received_at), ev.subsystem, ev.kind, ev.severity, ev.device_id,
            ev.message, ev.provenance.value, ev.source, session_id, json.dumps(ev.data, default=str,
                                                                              ensure_ascii=False))


class HealthJournal:
    def __init__(self, path: Path | str | None, *, max_queue: int = MAX_QUEUE, batch: int = BATCH,
                 connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
                 retention_days: float | None = RETENTION_DAYS):
        self.path = Path(path) if path is not None else None
        self.max_queue = max_queue
        self._batch = max(1, batch)
        self._connect = connect
        self._retention = retention_days
        self._q: queue.Queue[object] = queue.Queue(maxsize=max_queue)
        self._lock = threading.Lock()  # counters only
        self.session_id = new_id()
        self.state = "off" if self.path is None else "new"
        self.error = ""
        self.offered = 0
        self.written = 0
        self.dropped = 0
        self._dropped_reported = 0
        self.high_water = 0
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ any thread
    def start(self) -> None:
        if self.path is None or self._thread is not None:
            return
        self.state = "starting"
        self._thread = threading.Thread(target=self._run, name="health-journal", daemon=True)
        self._thread.start()

    def offer(self, ev: HealthEvent) -> bool:
        """Queue one event for the journal. Never blocks, never raises. False = not queued (dropped or journal off)."""
        try:
            with self._lock:
                self.offered += 1
            if self.state == "off":
                return False
            if self.state in ("failed", "closed"):
                self._drop()
                return False
            self._q.put_nowait(ev)
            self.high_water = max(self.high_water, self._q.qsize())
            return True
        except queue.Full:
            self._drop()
            return False
        except Exception:  # noqa: BLE001 - a journal problem never reaches the publisher
            self._drop()
            return False

    def _drop(self) -> None:
        with self._lock:
            self.dropped += 1

    def stats(self) -> dict[str, Any]:
        return {"state": self.state, "path": str(self.path) if self.path else "", "written": self.written,
                "dropped": self.dropped, "queued": self._q.qsize(), "max_queue": self.max_queue,
                "high_water": self.high_water, "error": self.error, "offered": self.offered}

    def close(self, timeout: float = 3.0) -> None:
        """Write what is queued (up to `timeout`), then close. Idempotent."""
        t = self._thread
        if t is None:
            if self.state not in ("off",):
                self.state = "closed"
            return
        if self.state != "failed":
            try:
                self._q.put(_STOP, timeout=timeout)
            except queue.Full:
                self.state = "closed"  # the writer stops at its next batch
        t.join(timeout=timeout)
        self._thread = None
        if self.state != "failed":
            self.state = "closed"

    def query(self, sql: str = "SELECT * FROM events ORDER BY id", params: tuple[Any, ...] = ()) -> list[dict]:
        """Read rows back (tests, diagnostics). Opens its own read connection; never used on the write path."""
        if self.path is None or not self.path.exists():
            return []
        con = sqlite3.connect(str(self.path))
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    # ------------------------------------------------------------------ writer thread
    def _open(self) -> sqlite3.Connection:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect(str(self.path), timeout=5.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(_SCHEMA)
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        if self._retention is not None:
            cutoff = _iso(utcnow() - timedelta(days=self._retention))
            con.execute("DELETE FROM events WHERE received_at < ?", (cutoff,))
        con.commit()
        return con

    def _run(self) -> None:
        con: sqlite3.Connection | None = None
        try:
            con = self._open()
            self.state = "ok"
            stop = False
            while not stop:
                try:
                    first = self._q.get(timeout=0.5)
                except queue.Empty:
                    if self.state == "closed":
                        break
                    self._report_overflow(con)
                    continue
                batch: list[HealthEvent] = []
                for item in self._take(first):
                    if item is _STOP:
                        stop = True
                    elif isinstance(item, HealthEvent):
                        batch.append(item)
                if batch:
                    with con:
                        con.executemany(_INSERT, [_row(e, self.session_id) for e in batch])
                    self.written += len(batch)
                self._report_overflow(con)
                if self.state == "closed":
                    break
        except Exception as exc:  # noqa: BLE001 - the journal fails alone; Health and safety carry on
            self.state, self.error = "failed", f"{type(exc).__name__}: {exc}"
            self._drain_failed()
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001, S110 - closing a broken connection
                    pass

    def _take(self, first: object) -> list[object]:
        items = [first]
        while len(items) < self._batch:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items

    def _report_overflow(self, con: sqlite3.Connection) -> None:
        with self._lock:
            lost = self.dropped - self._dropped_reported
            if lost <= 0:
                return
            self._dropped_reported = self.dropped
        now = utcnow()
        ev = HealthEvent(new_id(), "journal.overflow", "host", "warning",
                         f"{lost} health event(s) dropped: the journal queue ({self.max_queue}) was full",
                         now, now, Provenance.MEASURED, "HealthJournal", data={"dropped": lost,
                                                                               "total_dropped": self.dropped})
        with con:
            con.execute(_INSERT, _row(ev, self.session_id))
        self.written += 1

    def _drain_failed(self) -> None:
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            if item is not _STOP:
                self._drop()
