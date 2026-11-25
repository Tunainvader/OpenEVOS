"""Health page (ADR-0068, Phase 0 of the Instrument Health kit): read-only status of the instrument.

An overview (overall state, freshness, counts), the operating-capability list kept apart from physical health, one card
per subsystem with its source, provenance and age, "Why is operation restricted?" quoting the StageMonitor's lock and
the SafetyManager's refusal, and the recent Health events.

The page never commands anything and never waits on a device: it reads `HealthService.latest` (an immutable report the
sampler thread swaps in) and the recorder's recent events once a second. States are shown as shape + text + colour,
never colour alone; a source that is missing reads NOT MONITORED, one that is stale reads UNKNOWN.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openevos.core.models import utcnow
from openevos.health.assess import overall_reasons
from openevos.health.model import (
    CAPABILITIES,
    SUBSYSTEMS,
    CapabilityState,
    HealthReport,
    HealthState,
    Provenance,
    SubsystemHealth,
)
from openevos.ui import theme
from openevos.ui.common import empty_hint, label, panel

#: health state -> (theme state for the colour, glyph): the glyph differs per state, so colour is never alone
STATE_LOOK: dict[str, tuple[str, str]] = {
    "HEALTHY": ("ok", "●"), "WATCH": ("warn", "▲"), "RESTRICTED": ("locked", "■"), "FAULT": ("fault", "✕"),
    "UNKNOWN": ("unknown", "?"), "OFFLINE": ("unavailable", "○"), "NOT MONITORED": ("unavailable", "–"),
    "PERMITTED": ("ok", "●"), "AT RISK": ("warn", "▲"), "UNAVAILABLE": ("unavailable", "○"),
}
EVENT_HEADERS = ["Time", "Subsystem", "Severity", "Event", "Message", "Source · provenance"]
SEVERITY_STATE = {"critical": "fault", "error": "fault", "warning": "warn", "info": "unknown"}
REFRESH_MS = 1000
CARD_COLUMNS = 3


def ago(t: datetime | None, now: datetime | None = None) -> str:
    if t is None:
        return "never"
    s = max(0.0, ((now or utcnow()) - t).total_seconds())
    if s < 1.0:
        return "just now"
    if s < 90:
        return f"{s:.0f} s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    return f"{s / 3600:.1f} h ago"


def short_age(t: datetime | None, now: datetime | None = None) -> str:
    if t is None:
        return ""
    s = max(0.0, ((now or utcnow()) - t).total_seconds())
    return "now" if s < 1.0 else (f"{s:.0f} s" if s < 90 else (f"{s / 60:.0f} min" if s < 5400 else
                                                                 f"{s / 3600:.1f} h"))


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


LOCK_REF = "[the StageMonitor's lock, quoted under 'Why is operation restricted?']"


def without_lock_text(text: str, locks: list[str], ref: str = LOCK_REF) -> str:
    """`text` with any StageMonitor refusal it quotes replaced by a short reference (the refusal is shown once)."""
    for lock in locks:
        if lock and lock in text:
            text = text.replace(lock, ref)
    return text


class StateChip(QLabel):
    """'● HEALTHY': glyph + text in the state's colour, on a bordered chip."""

    def __init__(self, text: str = "UNKNOWN", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("healthchip")
        self.state = ""
        self.set(text)

    def set(self, text: str, tooltip: str = "") -> None:
        look, glyph = STATE_LOOK.get(text, ("unknown", "?"))
        self.state = text
        c = theme.state_color(look)
        self.setText(f"{glyph} {text}")
        self.setStyleSheet(f"QLabel#healthchip {{ color: {c}; border: 1px solid {c}; border-radius: 9px; "
                           f"padding: 1px 8px; font-weight: 600; }}")
        self.setAccessibleName(f"state {text}")
        self.setToolTip(tooltip)


class SubsystemCard(QWidget):
    def __init__(self, key: str, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.key = key
        f, lay = panel(None, sub=True, margins=10, spacing=4)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(f)
        top = QHBoxLayout()
        self.title = label(title, "value")
        self.chip = StateChip()
        self.prov = label("", "caption")
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.prov)
        top.addWidget(self.chip)
        lay.addLayout(top)
        self.fresh = label("", "caption", wrap=True)
        self.reasons = label("", "", wrap=True)
        self.metrics = label("", "", wrap=True)
        self.metrics.setTextFormat(Qt.TextFormat.RichText)
        self.source = label("", "caption", wrap=True)
        self.unmonitored = label("", "caption", wrap=True)
        for w in (self.fresh, self.reasons, self.metrics, self.source, self.unmonitored):
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(w)
        lay.addStretch(1)

    def show_health(self, s: SubsystemHealth, now: datetime) -> None:
        text = s.state.value if s.monitored else "NOT MONITORED"
        self.chip.set(text, "; ".join(s.reasons))
        self.prov.setText(s.provenance.value.upper() if s.provenance is Provenance.DEMO else s.provenance.value)
        self.prov.setToolTip("Demo: from the simulator, not the instrument" if s.provenance is Provenance.DEMO
                             else f"Provenance of this card's state: {s.provenance.value}")
        if not s.monitored:
            self.fresh.setText("No source in this session")
        elif s.stale:
            self.fresh.setText(f"STALE: last observation {ago(s.observed_at, now)}; expected every "
                               f"{s.expected_period_s:g} s")
        elif s.expected_period_s is not None:
            verb = "Measured" if s.key in ("storage", "host") else "Observed"
            self.fresh.setText(f"{verb} {ago(s.observed_at, now)} · expected every {s.expected_period_s:g} s")
        else:
            self.fresh.setText(f"Reports on change · last {ago(s.observed_at, now)}" if s.observed_at
                               else "Reports on change")
        self.reasons.setText("\n".join(f.text for f in s.findings[:4]))
        rows = []
        for m in s.metrics:
            v = "—" if m.value is None or m.value == "" else _esc(m.value)
            shown = "µm" if m.unit == "um" else m.unit  # the store's unit name, written as the page writes it
            unit = f" {_esc(shown)}" if shown and m.value not in (None, "") else ""
            age = f" · {short_age(m.observed_at, now)}" if m.observed_at else ""
            note = f" ({_esc(m.note)})" if m.note else ""
            rows.append(f"<tr><td style='padding-right:10px'>{_esc(m.name)}</td><td><b>{v}{unit}</b>{note}</td>"
                        f"<td style='padding-left:10px; color:{theme.MUTED}'>{m.provenance.value}{age}</td></tr>")
        self.metrics.setText(f"<table cellspacing='0'>{''.join(rows)}</table>" if rows else "")
        self.metrics.setVisible(bool(rows))
        self.source.setText(f"Source: {s.source}" + (f" · affects: {', '.join(s.capabilities)}" if s.capabilities
                                                     else ""))
        self.unmonitored.setText("Not monitored: " + "; ".join(s.not_monitored) if s.not_monitored else "")
        self.unmonitored.setVisible(bool(s.not_monitored))


class HealthPage(QWidget):
    def __init__(self, model: Any, parent: QWidget | None = None):
        super().__init__(parent)
        self.model = model
        self.service = model.health_service()
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ------------------------------------------------------------------ overview
        g, gl = panel("Instrument health")
        top = QHBoxLayout()
        self.overall = StateChip()
        self.updated = label("", "muted")
        self.counts = label("", "muted")
        top.addWidget(self.overall)
        top.addSpacing(8)
        top.addWidget(self.updated)
        top.addStretch(1)
        top.addWidget(self.counts)
        gl.addLayout(top)
        self.overall_why = label("", "", wrap=True)
        gl.addWidget(self.overall_why)
        gl.addWidget(label("Read-only. Health observes and recommends: the StageMonitor and the SafetyManager decide "
                           "what may move, and nothing on this page sends a command. Unknown is never shown as "
                           "healthy; a value from the simulator is marked DEMO.", "caption", wrap=True))
        self.journal = label("", "caption", wrap=True)
        gl.addWidget(self.journal)
        root.addWidget(g)

        # ------------------------------------------------------------------ capability | why restricted
        row = QHBoxLayout()
        row.setSpacing(12)
        c, cl = panel("Operating capability")
        cl.addWidget(label("What may run now, apart from physical health. RESTRICTED means the SafetyManager refuses "
                           "it (quoted); AT RISK is Health's advice only.", "caption", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.cap_rows: dict[str, tuple[StateChip, QLabel, QLabel]] = {}
        for i, cap in enumerate(CAPABILITIES):
            chip = StateChip()
            name = label(cap.title, "")
            why = label("", "caption", wrap=True)
            why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(chip, i * 2, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(name, i * 2, 1)
            grid.addWidget(why, i * 2 + 1, 1)
            self.cap_rows[cap.key] = (chip, name, why)
        grid.setColumnStretch(1, 1)
        cl.addLayout(grid)
        cl.addStretch(1)
        row.addWidget(c, 1)
        w, wl = panel("Why is operation restricted?")
        self.why = QLabel()
        self.why.setWordWrap(True)
        self.why.setTextFormat(Qt.TextFormat.RichText)
        self.why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.why.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        wl.addWidget(self.why)
        wl.addStretch(1)
        row.addWidget(w, 1)
        root.addLayout(row)

        # ------------------------------------------------------------------ subsystem cards
        s, sl = panel("Subsystems")
        cards = QGridLayout()
        cards.setHorizontalSpacing(10)
        cards.setVerticalSpacing(10)
        self.cards: dict[str, SubsystemCard] = {}
        for i, d in enumerate(SUBSYSTEMS):
            card = SubsystemCard(d.key, d.title)
            cards.addWidget(card, i // CARD_COLUMNS, i % CARD_COLUMNS)
            self.cards[d.key] = card
        for col in range(CARD_COLUMNS):
            cards.setColumnStretch(col, 1)
        sl.addLayout(cards)
        root.addWidget(s)

        # ------------------------------------------------------------------ recent events
        e, el = panel("Recent events")
        self.events = QTableWidget(0, len(EVENT_HEADERS))
        self.events.setHorizontalHeaderLabels(EVENT_HEADERS)
        self.events.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.events.horizontalHeader().setStretchLastSection(True)
        self.events.verticalHeader().setVisible(False)
        self.events.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.events.setAlternatingRowColors(True)
        self.events.setMinimumHeight(240)
        empty_hint(self.events, "No Health events yet")
        el.addWidget(self.events)
        root.addWidget(e)
        root.addStretch(1)

        self._last_event_id = ""
        self.report: HealthReport | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh()

    # ------------------------------------------------------------------ refresh (GUI thread; reads only)
    def _tick(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        rep = self.service.latest
        self.report = rep
        now = utcnow()
        self.overall.set(rep.overall.value)
        self.updated.setText(f"Assessed {ago(rep.generated_at, now)} · every {self.service.sample_period_s:g} s")
        if rep.error:
            self.overall_why.setText(f"No assessment: {rep.error}. The state stays UNKNOWN until one succeeds.")
        else:
            why = overall_reasons(rep)
            self.overall_why.setText("; ".join(why[:3]) if rep.overall is not HealthState.HEALTHY
                                     else "Every monitored subsystem is within its normal state.")
        counts = rep.counts
        order = ("FAULT", "RESTRICTED", "OFFLINE", "WATCH", "UNKNOWN", "HEALTHY", "NOT MONITORED")
        self.counts.setText(" · ".join(f"{counts[k]} {k.lower()}" for k in order if counts.get(k)))
        j = self.service.journal.stats()
        where = j["path"] or "memory only (no settings folder)"
        self.journal.setText(f"Journal: {j['state']} · {j['written']} written · {j['dropped']} dropped · "
                             f"{j['queued']}/{j['max_queue']} queued · {where}"
                             + (f" · {j['error']}" if j.get("error") else ""))
        locks = [r.refusal for r in rep.restrictions if r.refusal]
        for cap in CAPABILITIES:
            chip, _name, why_l = self.cap_rows[cap.key]
            st = rep.capability(cap.key)
            if st is None:
                chip.set("UNKNOWN")
                why_l.setText("not assessed")
                continue
            chip.set(st.state.value)
            who = "SafetyManager" if st.authority == "SafetyManager" else "Health (derived)"
            full = "; ".join(st.why) if st.why else ""
            why_l.setText(without_lock_text(full, locks) + f"  [{who}]")
            why_l.setToolTip(full)
        self.why.setText(self._why_html(rep, now))
        for key, card in self.cards.items():
            sub = rep.subsystem(key)
            if sub is not None:
                card.show_health(sub, now)
            else:
                card.chip.set("UNKNOWN")
                card.reasons.setText(rep.error or "not assessed")
        self._fill_events(now)

    def _why_html(self, rep: HealthReport, now: datetime) -> str:
        if rep.error:
            return f"<p>Not known: the assessment failed ({_esc(rep.error)}).</p>"
        if not rep.restrictions:
            open_caps = [c.title for c in rep.capabilities if c.state is CapabilityState.PERMITTED]
            return ("<p><b>Nothing is restricted.</b> The SafetyManager refuses none of the probed operations and no "
                    "StageMonitor lock is set.</p>"
                    + (f"<p style='color:{theme.MUTED}'>Permitted: {_esc(', '.join(open_caps))}.</p>"
                       if open_caps else ""))
        parts = []
        for r in rep.restrictions:
            caps = ", ".join(c.title for c in rep.capabilities if c.key in r.capabilities) or "—"
            p = [f"<p><b>{_esc(r.summary)}</b><br>Restricted: {_esc(caps)}<br>Decided by: {_esc(r.source)}"]
            if r.since_utc:
                p.append(f"<br>Since: {_esc(r.since_utc[:19].replace('T', ' '))} UTC"
                         + (" (carried over from an earlier session)" if r.carried_over else ""))
            same = "[the lock above]"
            if r.refusal:
                p.append(f"<br>StageMonitor: <i>{_esc(r.refusal)}</i>")
            for reason in r.safety_reasons:
                p.append(f"<br>SafetyManager refuses: <i>{_esc(without_lock_text(reason, [r.refusal], same))}</i>")
            if r.last_router_refusal:
                p.append(f"<br>Last refused command ({ago(r.last_router_refusal_at, now)}): "
                         f"<i>{_esc(without_lock_text(r.last_router_refusal, [r.refusal], same))}</i>")
            if r.releasable is not None:
                p.append("<br>Release: " + ("a small drift or landing lock: the operator may release it after fresh "
                                            "reads (Microscope › Release stage lock, ADR-0057), or re-home it"
                                            if r.releasable else "not releasable from the app: Microscope › Re-home "
                                            "stage (ADR-0058) or the recovery below"))
            if r.auto_recovery:
                p.append("<br>Automatic recovery (ADR-0067, an automatic action the operator chose; recorded as a "
                         f"departure from the Health kit): {_esc(r.auto_recovery)}")
            if r.recovery_attempts and r.recovery_attempts != "none":
                p.append(f"<br>Automatic attempts this session: {_esc(r.recovery_attempts)}")
            if r.last_recovery and r.last_recovery != "none recorded":
                p.append(f"<br>Last automatic recovery: {_esc(r.last_recovery)}")
            if r.recovery and r.recovery not in r.refusal:
                p.append(f"<br>Recovery: {_esc(r.recovery)}")
            if r.evidence:
                p.append(f"<br>Evidence: {_esc(r.evidence)}")
            p.append("</p>")
            parts.append("".join(p))
        open_caps = [c.title for c in rep.capabilities if c.state in (CapabilityState.PERMITTED,
                                                                       CapabilityState.WATCH)]
        if open_caps:
            parts.append(f"<p>Still permitted: {_esc(', '.join(open_caps))}.</p>")
        parts.append(f"<p style='color:{theme.MUTED}'>Health does not release locks. Acknowledging an event, "
                     "dismissing it and releasing a lock are different things; only the StageMonitor's own release "
                     "or re-home path clears a lock.</p>")
        return "".join(parts)

    def _fill_events(self, now: datetime) -> None:
        evs = self.service.recent_events(200)
        newest = evs[0].event_id if evs else ""
        if newest == self._last_event_id and self.events.rowCount() == len(evs):
            return
        self._last_event_id = newest
        t = self.events
        t.setRowCount(len(evs))
        for r, ev in enumerate(evs):
            local = ev.observed_at.astimezone().strftime("%H:%M:%S")
            col = theme.state_color(SEVERITY_STATE.get(ev.severity, "unknown"))
            cells = [local, ev.subsystem, ev.severity.upper(), ev.kind, ev.message,
                     f"{ev.source} · {ev.provenance.value}"]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c == 2:
                    it.setForeground(QColor(col))
                if c == 4:
                    it.setToolTip(text)
                t.setItem(r, c, it)
        if evs:
            t.resizeColumnsToContents()
            t.setColumnWidth(4, min(t.columnWidth(4), 620))
