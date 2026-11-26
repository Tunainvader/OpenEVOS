"""Diagnostic export for Instrument Health, Phase 1 (ADR-0069).

One zip a technician or a later analysis can read without the app: the latest report, the rules' assessments (open
and their raised/resolved history), the correlated incidents, the journal's events for the window, the baselines, the
inventory with every NOT MONITORED reason, the detectors' status and the recorded metric series. It only reads: the
journal through its own read connection, everything else from the service's immutable report and in-memory buffers.
Nothing is sent anywhere; the file stays where it is written.
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from openevos import __version__
from openevos.health import inventory
from openevos.health.correlate import correlate
from openevos.health.inject import physical_parts
from openevos.health.journal import _iso
from openevos.health.model import HealthEvent, Provenance

EXPORT_SCHEMA = "openevos-health-diagnostics/1.0"
MAX_EVENTS = 50_000


def _plain(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, datetime):
        return o.isoformat()
    if is_dataclass(o) and not isinstance(o, type):
        return {k: _plain(v) for k, v in asdict(o).items()}
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [_plain(v) for v in o]
    return o


def _dump(o: Any) -> str:
    return json.dumps(_plain(o), indent=1, ensure_ascii=False, default=str)


def _event_from_row(r: dict[str, Any]) -> HealthEvent | None:
    try:
        return HealthEvent(r["event_id"], r["kind"], r["subsystem"], r["severity"], r["message"],
                           datetime.fromisoformat(r["observed_at"]), datetime.fromisoformat(r["received_at"]),
                           Provenance(r["provenance"]), r["source"], r.get("device_id") or "",
                           json.loads(r["data"]) if r.get("data") else {})
    except Exception:  # noqa: BLE001 - a malformed row is left out of the incidents, kept in events.jsonl
        return None


def window_events(health: Any, since: datetime) -> tuple[list[dict[str, Any]], list[HealthEvent]]:
    """(rows as written, events) since `since`: from the journal file, or from memory for a memory-only journal."""
    rows = health.journal.query("SELECT * FROM events WHERE observed_at >= ? ORDER BY observed_at, id LIMIT ?",
                                (_iso(since), MAX_EVENTS))
    if rows:
        return rows, [e for e in (_event_from_row(r) for r in rows) if e is not None]
    mem = {e.event_id: e for e in (*health.rule_events(), *health.recent_events(10_000))}
    evs = sorted((e for e in mem.values() if e.observed_at >= since), key=lambda e: e.observed_at)
    return [_plain(e) for e in evs], evs


def export_diagnostics(health: Any, dest_dir: Path | str, *, hours: float = 24.0) -> Path:
    """Write health-diagnostics-<UTC time>.zip into dest_dir and return its path."""
    now = health._now()
    since = now - timedelta(hours=hours)
    rows, events = window_events(health, since)
    report = health.latest
    parts = physical_parts(health.session)
    history = [r for r in rows if str(r.get("kind", "")).startswith(("assessment.", "baseline.", "simulation."))]
    manifest = {
        "schema": EXPORT_SCHEMA, "created_utc": now.isoformat(), "window_hours": hours, "since_utc": since.isoformat(),
        "openevos_version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
        "simulated": not parts, "physical_parts": parts, "fingerprint": health.fingerprint(),
        "journal": health.journal.stats(), "events": len(rows), "events_capped_at": MAX_EVENTS,
        "notes": ["Read-only export of what Health recorded; provenance labels are as recorded.",
                  "Rules say WATCH only; their limits are provisional unless a baseline says qualified.",
                  "Hypotheses are never confirmed by Health; incidents group events, they do not prove a cause."],
    }
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"health-diagnostics-{now.strftime('%Y%m%dT%H%M%SZ')}.zip"
    series = {k: health.series.points(k) for k in health.series.keys(prefix="")}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", _dump(manifest))
        z.writestr("report.json", _dump(report))
        z.writestr("assessments.json", _dump({"open": [a.to_json() for a in report.assessments],
                                              "history": history}))
        z.writestr("detectors.json", _dump(report.detectors))
        z.writestr("incidents.json", _dump([i.to_json() for i in correlate(events)]))
        z.writestr("events.jsonl", "\n".join(json.dumps(_plain(r), ensure_ascii=False, default=str) for r in rows))
        z.writestr("baselines.json", _dump(health.baselines.to_json()))
        z.writestr("inventory.json", _dump([{**_plain(s), "monitored": s.monitored} for s in inventory.INVENTORY]))
        z.writestr("series.json", _dump(series))
    health._emit("diagnostics.exported", "host", "info", f"diagnostic bundle written: {path}",
                 provenance=Provenance.DERIVED, source="export_diagnostics",
                 data={"path": str(path), "events": len(rows), "hours": hours})
    return path
