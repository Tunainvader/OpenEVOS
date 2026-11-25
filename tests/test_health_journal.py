"""Health journal (ADR-0068 §3): a bounded, non-blocking queue in front of SQLite. Under load and with the disk stalled
`offer` returns at once and drops (counted, then recorded as an overflow row); when SQLite fails the journal fails
alone."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

from openevos.core.models import new_id, utcnow
from openevos.health.journal import HealthJournal
from openevos.health.model import HealthEvent, Provenance


def ev(i: int = 0, kind: str = "test.event", at=None) -> HealthEvent:
    t = at or utcnow()
    return HealthEvent(new_id(), kind, "xy_stage", "info", f"event {i}", t, t, Provenance.DEMO, "test",
                       "motor.stage", {"i": i})


def wait_state(j: HealthJournal, *states: str, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if j.state in states:
            return True
        time.sleep(0.01)
    return False


class _Gate:
    """A sqlite3 connection factory whose writes wait on an event: a stalled disk."""

    def __init__(self) -> None:
        self.open = threading.Event()
        self.entered = threading.Event()
        gate = self

        class Slow(sqlite3.Connection):
            def executemany(self, *a, **k):  # type: ignore[override]
                gate.entered.set()
                gate.open.wait(30)
                return super().executemany(*a, **k)

        self.factory = Slow

    def connect(self, path: str, timeout: float = 5.0) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=timeout, factory=self.factory)


def test_offer_never_blocks_while_the_disk_is_stalled(tmp_path):
    gate = _Gate()
    j = HealthJournal(tmp_path / "j.sqlite3", max_queue=100, connect=gate.connect)
    j.start()
    assert wait_state(j, "ok")
    assert j.offer(ev(-1))
    assert gate.entered.wait(5)  # the writer is now stuck inside its first write
    n_threads, per = 4, 5000
    worst: list[float] = []

    def flood() -> None:
        mx = 0.0
        for i in range(per):
            t0 = time.perf_counter()
            j.offer(ev(i))
            mx = max(mx, time.perf_counter() - t0)
        worst.append(mx)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=flood) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    elapsed = time.perf_counter() - t0
    offered = n_threads * per + 1
    assert elapsed < 5.0, f"{offered} offers took {elapsed:.2f} s with the disk stalled"
    assert max(worst) < 0.25, f"one offer took {max(worst) * 1000:.0f} ms"
    st = j.stats()
    assert st["queued"] <= 100 and j.high_water <= 100  # bounded
    assert st["dropped"] >= offered - 101
    gate.open.set()
    j.close(timeout=10)
    rows = j.query()
    kept = [r for r in rows if r["kind"] != "journal.overflow"]
    overflow = [r for r in rows if r["kind"] == "journal.overflow"]
    assert len(kept) + j.dropped == j.offered == offered  # every event is either written or counted as dropped
    assert overflow and sum(json.loads(r["data"])["dropped"] for r in overflow) == j.dropped
    assert j.state == "closed"


def test_rows_follow_the_health_event_schema(tmp_path):
    j = HealthJournal(tmp_path / "j.sqlite3")
    j.start()
    e = ev(7, "stage_monitor.latched")
    assert j.offer(e)
    j.close()
    (row,) = j.query()
    for col in ("event_id", "observed_at", "received_at", "subsystem", "kind", "severity", "device_id", "message",
                "provenance", "source", "session_id", "data"):
        assert col in row
    assert row["event_id"] == e.event_id and row["provenance"] == "Demo" and row["subsystem"] == "xy_stage"
    assert json.loads(row["data"]) == {"i": 7}
    assert row["observed_at"].endswith("+00:00")


def test_journal_that_cannot_open_fails_alone(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    j = HealthJournal(blocker / "j.sqlite3")
    j.start()
    assert wait_state(j, "failed")
    assert j.error
    t0 = time.perf_counter()
    results = [j.offer(ev(i)) for i in range(1000)]
    assert time.perf_counter() - t0 < 1.0
    assert not any(results) and j.dropped == 1000
    j.close()  # no exception


def test_write_error_midway_fails_the_journal_and_keeps_offers_non_blocking(tmp_path):
    class Broken(sqlite3.Connection):
        def executemany(self, *a, **k):  # type: ignore[override]
            raise sqlite3.OperationalError("disk I/O error")

    j = HealthJournal(tmp_path / "j.sqlite3",
                      connect=lambda p, timeout=5.0: sqlite3.connect(p, timeout=timeout, factory=Broken))
    j.start()
    assert wait_state(j, "ok", "failed")
    j.offer(ev(0))
    assert wait_state(j, "failed")
    assert "disk I/O error" in j.error
    assert not j.offer(ev(1))
    j.close()


def test_memory_only_journal_is_off_not_failing(tmp_path):
    j = HealthJournal(None)
    j.start()
    assert j.state == "off" and not j.offer(ev(0)) and j.dropped == 0
    j.close()
    assert j.query() == []


def test_retention_prunes_old_rows_at_open(tmp_path):
    p = tmp_path / "j.sqlite3"
    j = HealthJournal(p, retention_days=None)
    j.start()
    old = utcnow() - timedelta(days=400)
    j.offer(ev(1, at=old))
    j.offer(ev(2))
    j.close()
    con = sqlite3.connect(str(p))
    con.execute("UPDATE events SET received_at = ? WHERE message = 'event 1'", (old.isoformat(),))
    con.commit()
    con.close()
    j2 = HealthJournal(p, retention_days=180)
    j2.start()
    assert wait_state(j2, "ok")
    j2.close()
    assert [r["message"] for r in j2.query()] == ["event 2"]
    assert Path(p).exists()
