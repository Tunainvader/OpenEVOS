"""Event correlation for Instrument Health, Phase 1 (ADR-0069).

Groups journal events into incidents: a warning, error or critical event opens one, and the later events join it while
they come within `gap_s` of the incident's last event and are related to it. Related means the same subsystem, the same
device, or a pair the instrument is known to link (a link problem and the devices on it; a drive fault, the X/Y lock
and the automatic recovery that follows). Info events inside an incident's span and related to it are attached as
context. Deterministic: the same events always give the same incidents. Correlation says "these happened together",
never "this caused that".
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openevos.health.model import HealthEvent

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
GAP_S = 30.0
#: subsystems the instrument links: a problem in one often shows in the other
RELATED = {frozenset(p) for p in (("communications", "xy_stage"), ("communications", "focus"),
                                  ("communications", "objective_turret"), ("communications", "filter_carriage"),
                                  ("communications", "mono_camera"), ("communications", "colour_camera"),
                                  ("communications", "illumination"), ("xy_stage", "focus"),
                                  ("xy_stage", "objective_turret"), ("storage", "mono_camera"),
                                  ("storage", "colour_camera"), ("storage", "host"))}
#: bookkeeping events that never open or join an incident
IGNORED_KINDS = frozenset({"recorder.started", "recorder.stopped", "health.assessed", "command.succeeded",
                           "journal.overflow"})


@dataclass(frozen=True)
class Incident:
    incident_id: str
    title: str
    severity: str
    started_at: datetime
    ended_at: datetime
    subsystems: tuple[str, ...]
    devices: tuple[str, ...]
    event_ids: tuple[str, ...]
    #: the subset of event_ids at warning or worse
    key_event_ids: tuple[str, ...] = ()
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    def to_json(self) -> dict[str, Any]:
        return {"incident_id": self.incident_id, "title": self.title, "severity": self.severity,
                "started_at": self.started_at.isoformat(), "ended_at": self.ended_at.isoformat(),
                "subsystems": list(self.subsystems), "devices": list(self.devices),
                "event_ids": list(self.event_ids), "key_event_ids": list(self.key_event_ids), "kinds": self.kinds,
                "note": "events grouped by time and relation; correlation is not cause"}


def related(a: HealthEvent, subsystems: set[str], devices: set[str]) -> bool:
    if a.device_id and a.device_id in devices:
        return True
    if a.subsystem in subsystems:
        return True
    return any(frozenset((a.subsystem, s)) in RELATED for s in subsystems)


@dataclass
class _Open:
    events: list[HealthEvent]
    subsystems: set[str]
    devices: set[str]
    last: datetime


def correlate(events: Iterable[HealthEvent], gap_s: float = GAP_S) -> list[Incident]:
    """Incidents from events (any order), oldest first."""
    evs = sorted((e for e in events if e.kind not in IGNORED_KINDS), key=lambda e: (e.observed_at, e.event_id))
    opened: list[_Open] = []
    pending_info: list[HealthEvent] = []
    for e in evs:
        bad = SEVERITY_RANK.get(e.severity, 0) >= 1
        live = [o for o in opened if (e.observed_at - o.last).total_seconds() <= gap_s]
        home = next((o for o in reversed(live) if related(e, o.subsystems, o.devices)), None)
        if home is not None:
            home.events.append(e)
            if bad:
                home.last = e.observed_at
                home.subsystems.add(e.subsystem)
                if e.device_id:
                    home.devices.add(e.device_id)
            continue
        if not bad:
            pending_info.append(e)
            continue
        # context: info events just before, related to this one
        ctx = [i for i in pending_info if 0 <= (e.observed_at - i.observed_at).total_seconds() <= gap_s
               and related(i, {e.subsystem}, {e.device_id} if e.device_id else set())]
        opened.append(_Open(ctx + [e], {e.subsystem}, {e.device_id} if e.device_id else set(), e.observed_at))
    return [_incident(o) for o in opened]


def _incident(o: _Open) -> Incident:
    evs = sorted(o.events, key=lambda e: (e.observed_at, e.event_id))
    key = [e for e in evs if SEVERITY_RANK.get(e.severity, 0) >= 1]
    worst = max(key, key=lambda e: (SEVERITY_RANK.get(e.severity, 0), -evs.index(e)))
    kinds: dict[str, int] = {}
    for e in evs:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    ids = tuple(e.event_id for e in evs)
    iid = "inc-" + hashlib.sha1("|".join(ids).encode(), usedforsecurity=False).hexdigest()[:12]
    others = len(key) - 1
    title = worst.message + (f" (+{others} related)" if others > 0 else "")
    return Incident(iid, title, worst.severity, evs[0].observed_at, evs[-1].observed_at,
                    tuple(sorted({e.subsystem for e in evs})), tuple(sorted({e.device_id for e in evs if e.device_id})),
                    ids, tuple(e.event_id for e in key), kinds)
