"""Health states, provenance labels, subsystems and operating capabilities (ADR-0068; kit docs/HEALTH_MODEL.md).

Physical health ("does this subsystem behave?") is kept apart from operational capability ("may this operation run
now?"). A subsystem has one of six states; a capability has its own states, decided first by the SafetyManager (the
authority) and only then by the health of what it depends on. Connected does not mean healthy, and healthy does not
mean calibrated or safe for every operation.

Every value carries a provenance label. Simulator values are DEMO, never MEASURED.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class HealthState(str, Enum):
    HEALTHY = "HEALTHY"  # within the validated envelope: normal checks apply
    WATCH = "WATCH"  # early deviation or a standing warning: watch it
    RESTRICTED = "RESTRICTED"  # a specific operation is inhibited (e.g. the StageMonitor's drift lock)
    FAULT = "FAULT"  # critical: a drive trip, a device fault, recovery required
    UNKNOWN = "UNKNOWN"  # evidence insufficient or stale: never assumed healthy
    OFFLINE = "OFFLINE"  # the device or its source is unavailable

    @property
    def label(self) -> str:
        return self.value


#: worst first; the overall state is the worst monitored subsystem. UNKNOWN ranks above HEALTHY: unknown is not healthy
SEVERITY: dict[HealthState, int] = {HealthState.FAULT: 5, HealthState.RESTRICTED: 4, HealthState.OFFLINE: 3,
                                    HealthState.WATCH: 2, HealthState.UNKNOWN: 1, HealthState.HEALTHY: 0}


def worst(states: Iterable[HealthState], default: HealthState = HealthState.UNKNOWN) -> HealthState:
    """The most severe state; `default` (UNKNOWN) when there is none: no evidence is not good evidence."""
    return max(states, key=SEVERITY.__getitem__, default=default)


class Provenance(str, Enum):
    MEASURED = "Measured"  # read from the instrument or the host by an adapter, or by Health itself (free disk space)
    DERIVED = "Derived"  # computed deterministically from measured values, or the SafetyManager's own decision
    INFERRED = "Inferred"  # a rule's assessment from a trend or a limit (Phase 1); never confirmed
    UNAVAILABLE = "Unavailable"  # nothing exposes it: NOT MONITORED
    DEMO = "Demo"  # from the simulator or demo data, not from the instrument


class CapabilityState(str, Enum):
    PERMITTED = "PERMITTED"  # the SafetyManager would accept it and what it depends on is healthy
    WATCH = "WATCH"  # permitted; something it depends on is in WATCH
    AT_RISK = "AT RISK"  # the SafetyManager would accept it, but Health sees a fault it depends on (advisory only)
    RESTRICTED = "RESTRICTED"  # the SafetyManager refuses it now (the authority's decision, quoted)
    UNAVAILABLE = "UNAVAILABLE"  # no device for it in this session, or the device is offline
    UNKNOWN = "UNKNOWN"  # what it depends on is unknown or stale


@dataclass(frozen=True)
class SubsystemDef:
    key: str
    title: str
    #: True when the subsystem's state comes from device snapshots (False: derived across sources, e.g. storage)
    device_backed: bool = True


SUBSYSTEMS: tuple[SubsystemDef, ...] = (
    SubsystemDef("xy_stage", "X/Y stage"),
    SubsystemDef("focus", "Z focus"),
    SubsystemDef("objective_turret", "Objective turret"),
    SubsystemDef("filter_carriage", "Cube carriage & phase wheel"),
    SubsystemDef("mono_camera", "Mono camera"),
    SubsystemDef("colour_camera", "Colour camera"),
    SubsystemDef("illumination", "Illumination (LED board)"),
    SubsystemDef("incubator", "Incubator (AMC1000)"),
    SubsystemDef("communications", "Communications", device_backed=False),
    SubsystemDef("storage", "Storage & data", device_backed=False),
    SubsystemDef("host", "Host & application", device_backed=False),
)
SUBSYSTEM_TITLES = {s.key: s.title for s in SUBSYSTEMS}


@dataclass(frozen=True)
class CapabilityDef:
    key: str
    title: str
    #: the subsystems whose health this operation depends on
    depends_on: tuple[str, ...]
    #: the subsystems whose device the SafetyManager is asked about (a probe command, validated and never sent)
    probes: tuple[str, ...] = ()


CAPABILITIES: tuple[CapabilityDef, ...] = (
    CapabilityDef("xy_positioning", "Move the X/Y stage", ("xy_stage",), ("xy_stage",)),
    CapabilityDef("multi_position", "Multi-position runs (Scan, Multi-D positions)", ("xy_stage", "focus", "storage"),
                  ("xy_stage", "focus")),
    CapabilityDef("focus_z", "Focus (Z), autofocus and Z-stacks", ("focus",), ("focus",)),
    CapabilityDef("objective_change", "Change objective", ("objective_turret", "focus"), ("objective_turret",)),
    CapabilityDef("mono_imaging", "Mono camera imaging at the current position", ("mono_camera", "illumination"),
                  ("mono_camera", "illumination")),
    CapabilityDef("fluorescence_imaging", "Fluorescence channels", ("mono_camera", "illumination", "filter_carriage"),
                  ("mono_camera", "illumination")),
    CapabilityDef("colour_imaging", "Colour camera imaging (brightfield, phase)",
                  ("colour_camera", "illumination", "filter_carriage"), ("colour_camera", "illumination")),
    CapabilityDef("data_saving", "Save images and experiment data", ("storage",)),
    CapabilityDef("environment_control", "Incubator control (temperature, CO2)", ("incubator",)),
)
CAPABILITY_TITLES = {c.key: c.title for c in CAPABILITIES}


def capabilities_affected_by(subsystem: str) -> tuple[str, ...]:
    """The operating capabilities a subsystem affects (the inverse of CapabilityDef.depends_on)."""
    return tuple(c.key for c in CAPABILITIES if subsystem in c.depends_on)


@dataclass(frozen=True)
class Metric:
    """One value on a card: always with its unit, source, provenance and the time it was observed."""

    name: str
    value: Any
    unit: str
    provenance: Provenance
    source: str
    observed_at: datetime | None = None
    note: str = ""


@dataclass(frozen=True)
class Finding:
    """One reason behind a subsystem's state."""

    state: HealthState
    text: str
    provenance: Provenance
    source: str


@dataclass(frozen=True)
class SubsystemHealth:
    key: str
    title: str
    state: HealthState
    #: False: no source in this session (shown as NOT MONITORED, never green); left out of the overall state
    monitored: bool
    provenance: Provenance
    source: str
    observed_at: datetime | None = None
    #: the source's expected update period (s); None = it reports only on change (no staleness rule)
    expected_period_s: float | None = None
    stale: bool = False
    findings: tuple[Finding, ...] = ()
    metrics: tuple[Metric, ...] = ()
    not_monitored: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    simulated: bool = False
    capabilities: tuple[str, ...] = ()

    @property
    def reasons(self) -> tuple[str, ...]:
        """The findings that set the state (the worst ones), most important first."""
        return tuple(f.text for f in self.findings if f.state is self.state)


@dataclass(frozen=True)
class CapabilityStatus:
    key: str
    title: str
    state: CapabilityState
    why: tuple[str, ...] = ()
    #: who decided: "SafetyManager" (a refusal, quoted verbatim) or "Health" (derived from subsystem states)
    authority: str = "Health"
    depends_on: tuple[str, ...] = ()
    provenance: Provenance = Provenance.DERIVED


@dataclass(frozen=True)
class Restriction:
    """Why an operation is restricted, from the authority that restricts it (the StageMonitor's latch, quoted)."""

    source: str  # e.g. "StageMonitor (X/Y drift monitor, COM3)"
    capabilities: tuple[str, ...]
    summary: str
    rule: str = ""
    axis: str = ""
    since_utc: str = ""
    refusal: str = ""  # StageMonitor.refusal(), verbatim: the text the SafetyManager quotes when it refuses a move
    recovery: str = ""
    releasable: bool | None = None
    evidence: str = ""
    carried_over: bool = False
    last_router_refusal: str = ""
    last_router_refusal_at: datetime | None = None
    safety_reasons: tuple[str, ...] = ()
    #: ADR-0067: whether the automatic recovery will run for this lock, and why not (an automatic action the operator
    #: chose on 2026-11-26, recorded as a departure from the kit; shown as such, never as a rule breach)
    auto_recovery: str = ""
    recovery_attempts: str = ""
    last_recovery: str = ""


@dataclass(frozen=True)
class Assessment:
    """One rule's finding (Phase 1, ADR-0069), shaped like the kit's schemas/fault_assessment.example.json.

    Phase 1 rules only ever say WATCH: they inform, they restrict nothing. A rule returning to normal resolves its
    WATCH (the kit's "Watch -> Resolved"); it never releases a lock, which stays the StageMonitor's and the
    SafetyManager's."""

    assessment_id: str
    detector: str  # e.g. "comm.error_burst"
    detector_version: str
    key: str  # what it is about, e.g. a device id or an axis
    subsystem: str
    title: str
    detail: str
    provenance: Provenance  # INFERRED; DEMO when the evidence came from the simulator
    first_seen: datetime
    last_seen: datetime
    state: HealthState = HealthState.WATCH
    severity: str = "medium"
    evidence_event_ids: tuple[str, ...] = ()
    affected_capabilities: tuple[str, ...] = ()
    #: the baseline it compared against ({"id", "version", "regime"}), None for a fixed provisional limit
    baseline: dict[str, Any] | None = None
    limit: str = ""  # the threshold, in words, e.g. "3 failures within 60 s (provisional, not qualified)"
    hypotheses: tuple[str, ...] = ()  # never confirmed by Health
    checks: tuple[str, ...] = ()  # recommended operator checks
    data: dict[str, Any] = field(default_factory=dict)

    SCHEMA_VERSION = "1.0"

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, "assessment_id": self.assessment_id, "state": self.state.value,
                "severity": self.severity, "title": self.title, "detail": self.detail,
                "provenance": self.provenance.value.lower(), "confidence": None,
                "detector": {"name": self.detector, "version": self.detector_version}, "key": self.key,
                "subsystem": self.subsystem, "baseline": self.baseline, "limit": self.limit,
                "evidence_event_ids": list(self.evidence_event_ids),
                "affected_capabilities": list(self.affected_capabilities),
                "hypotheses": [{"label": h, "confirmed": False} for h in self.hypotheses],
                "recommended_operator_checks": list(self.checks), "first_seen": self.first_seen.isoformat(),
                "last_seen": self.last_seen.isoformat(), "data": self.data}


@dataclass(frozen=True)
class DetectorStatus:
    """Whether a rule runs in this session, and why not (e.g. "inactive: no baseline")."""

    name: str
    version: str
    subsystem: str
    title: str
    active: bool
    note: str = ""


@dataclass(frozen=True)
class HealthReport:
    generated_at: datetime
    overall: HealthState
    subsystems: tuple[SubsystemHealth, ...] = ()
    capabilities: tuple[CapabilityStatus, ...] = ()
    restrictions: tuple[Restriction, ...] = ()
    error: str = ""
    #: Phase 1 (ADR-0069): the rules' open WATCH assessments and whether each rule runs
    assessments: tuple[Assessment, ...] = ()
    detectors: tuple[DetectorStatus, ...] = ()

    def subsystem(self, key: str) -> SubsystemHealth | None:
        return next((s for s in self.subsystems if s.key == key), None)

    def capability(self, key: str) -> CapabilityStatus | None:
        return next((c for c in self.capabilities if c.key == key), None)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.subsystems:
            k = s.state.value if s.monitored else "NOT MONITORED"
            out[k] = out.get(k, 0) + 1
        return out


@dataclass(frozen=True)
class HealthEvent:
    """One journal record (kit schemas/health_event.example.json, extended with kind/severity/message)."""

    event_id: str
    kind: str  # e.g. device.condition, command.rejected, stage_monitor.latched, health.state_changed
    subsystem: str
    severity: str  # info | warning | error | critical
    message: str
    observed_at: datetime  # the source's own timestamp
    received_at: datetime  # when the recorder took it
    provenance: Provenance
    source: str  # component.signal, e.g. "EventBus.CommandLifecycleEvent"
    device_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    SCHEMA_VERSION = "1.0"
