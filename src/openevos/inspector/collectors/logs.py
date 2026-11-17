"""PnP / driver-framework event log entries for a user-selected time window."""

from __future__ import annotations

from openevos.inspector.collectors import Collector, CollectorContext, CollectorSkipped, ScanResults
from openevos.inspector.schemas import EventRecord


class EventLogCollector(Collector):
    name = "event_logs"
    description = "Kernel-PnP / UserPnp / DriverFrameworks events (Get-WinEvent)"

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        if not ctx.events_hours:
            raise CollectorSkipped("disabled (--events-hours 0)")
        rows = ctx.query("pnp_events", hours=ctx.events_hours)
        for i, row in enumerate(rows):
            key = f"{row.get('Time')}|{row.get('LogName')}|{row.get('Id')}|{i}"
            eid = ctx.add_evidence(self, "evt", key, "powershell:pnp_events", row)
            results.events.append(
                EventRecord(
                    time=row.get("Time"),
                    log=row.get("LogName") or "?",
                    provider=row.get("ProviderName"),
                    event_id=row.get("Id"),
                    level=row.get("LevelDisplayName"),
                    message=(row.get("Message") or "")[:2000] or None,
                    evidence_ids=[eid],
                )
            )
        return None
