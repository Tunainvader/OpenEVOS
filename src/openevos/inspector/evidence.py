"""Evidence governance: labels, evidence stages, evidence items, findings and the register.

This module encodes Sections 3.4 and 8.1 of the OpenEVOS technical plan as code so that the
rules are enforced by validation, not by convention:

* Six evidence labels (`EvidenceLabel`). Each label has a permitted confidence band.
* Five monotonic evidence stages (`EvidenceStage`). The Inspector can never assign a stage
  above `IDENTIFIED` because it never opens a device (enforced by `EvidenceRegister.max_stage`).
* Every non-trivial claim must cite evidence IDs that exist in the register.
* Only deterministic sources (collectors, rules, operator observations) may label a finding
  `VERIFIED`. AI output may cite verified findings but never mint new ones (ADR-0003).

Runtime/operational state (ready, busy, faulted...) is a *different axis* and lives in
the Connection/Health/Activity axes in `openevos.core.models` (ADR-0007).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------------------------
# Labels and confidence bands
# --------------------------------------------------------------------------------------------
class EvidenceLabel(str, Enum):
    VERIFIED = "verified"
    STRONGLY_SUPPORTED = "strongly_supported"
    PROBABLE = "probable"
    UNKNOWN = "unknown"
    NOT_TESTED = "not_tested"
    NOT_APPLICABLE = "not_applicable"

    @property
    def display(self) -> str:
        return self.value.replace("_", " ").capitalize()

    @property
    def is_assertive(self) -> bool:
        """Labels that assert something is true and therefore require evidence."""
        return self in ASSERTIVE_LABELS


ASSERTIVE_LABELS = frozenset(
    {EvidenceLabel.VERIFIED, EvidenceLabel.STRONGLY_SUPPORTED, EvidenceLabel.PROBABLE}
)

# Proposed numeric bands (ADR-0003). Half-open intervals so labels never overlap.
# Labels mapped to None must carry confidence=None.
CONFIDENCE_BANDS: dict[EvidenceLabel, tuple[float, float] | None] = {
    EvidenceLabel.VERIFIED: (1.0, 1.0),  # exactly 1.0
    EvidenceLabel.STRONGLY_SUPPORTED: (0.75, 1.0),  # [0.75, 1.0)
    EvidenceLabel.PROBABLE: (0.40, 0.75),  # [0.40, 0.75)
    EvidenceLabel.UNKNOWN: None,
    EvidenceLabel.NOT_TESTED: None,
    EvidenceLabel.NOT_APPLICABLE: None,
}

LABEL_RANK: dict[EvidenceLabel, int] = {
    EvidenceLabel.UNKNOWN: 0,
    EvidenceLabel.PROBABLE: 1,
    EvidenceLabel.STRONGLY_SUPPORTED: 2,
    EvidenceLabel.VERIFIED: 3,
}


def check_confidence(label: EvidenceLabel, confidence: float | None) -> None:
    """Raise ValueError if `confidence` is inconsistent with `label`."""
    band = CONFIDENCE_BANDS[label]
    if band is None:
        if confidence is not None:
            raise ValueError(f"label '{label.value}' must not carry a numeric confidence")
        return
    if confidence is None:
        raise ValueError(f"label '{label.value}' requires a numeric confidence")
    lo, hi = band
    if label is EvidenceLabel.VERIFIED:
        if confidence != 1.0:
            raise ValueError("label 'verified' requires confidence == 1.0")
    elif not (lo <= confidence < hi):
        raise ValueError(
            f"confidence {confidence} outside band [{lo}, {hi}) for label '{label.value}'"
        )


def label_for_confidence(confidence: float) -> EvidenceLabel:
    """Map a numeric confidence to the strongest label it supports (never VERIFIED)."""
    if confidence >= 0.75:
        return EvidenceLabel.STRONGLY_SUPPORTED
    if confidence >= 0.40:
        return EvidenceLabel.PROBABLE
    return EvidenceLabel.UNKNOWN


# --------------------------------------------------------------------------------------------
# Evidence stages (health progression) — Section 8.1
# --------------------------------------------------------------------------------------------
class EvidenceStage(IntEnum):
    DETECTED = 1  # OS or bus sees a device/interface
    IDENTIFIED = 2  # manufacturer/model/role known with recorded evidence
    CONNECTED = 3  # approved driver/SDK/backend can open it
    RESPONSIVE = 4  # safe, documented read succeeds
    FUNCTIONALLY_VALIDATED = 5  # defined validation test succeeds

    @property
    def display(self) -> str:
        return self.name.replace("_", " ").capitalize()


#: The Inspector never opens devices, so it can never claim more than this.
INSPECTOR_MAX_STAGE = EvidenceStage.IDENTIFIED


class Origin(str, Enum):
    COLLECTOR = "collector"  # deterministic collector output
    RULE = "rule"  # deterministic rule over collected evidence
    OPERATOR = "operator"  # human observation recorded with evidence (photo, label)
    AI = "ai"  # model interpretation; never authoritative

    @property
    def is_deterministic(self) -> bool:
        return self is not Origin.AI


# --------------------------------------------------------------------------------------------
# Evidence items
# --------------------------------------------------------------------------------------------
def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_of(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def make_evidence_id(prefix: str, natural_key: str) -> str:
    """Stable ID: same prefix + natural key -> same ID across scans (enables diffing)."""
    digest = hashlib.sha256(natural_key.upper().encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Evidence(BaseModel):
    """One collected fact with provenance. Immutable once created."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*-[0-9a-f]{8,}$")
    collector: str
    collector_version: str
    source: str  # e.g. "powershell:pnp_devices", "file:/mnt/img/Windows/INF/oem12.inf"
    collected_at: datetime
    data: dict[str, Any]
    sha256: str
    redacted: bool = False

    @model_validator(mode="after")
    def _integrity(self) -> Evidence:
        if sha256_of(self.data) != self.sha256:
            raise ValueError(f"evidence {self.id}: sha256 does not match data (tampered?)")
        return self

    @classmethod
    def create(
        cls,
        *,
        prefix: str,
        natural_key: str,
        collector: str,
        collector_version: str,
        source: str,
        data: dict[str, Any],
        collected_at: datetime | None = None,
    ) -> Evidence:
        return cls(
            id=make_evidence_id(prefix, natural_key),
            collector=collector,
            collector_version=collector_version,
            source=source,
            collected_at=collected_at or utcnow(),
            data=data,
            sha256=sha256_of(data),
        )


# --------------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------------
class Finding(BaseModel):
    """A claim about a component, labelled and linked to evidence."""

    id: str
    component: str  # instance ID, file path, or logical role e.g. "role:camera_mono_ccd"
    claim: str = Field(min_length=3)
    label: EvidenceLabel
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    origin: Origin
    rule_id: str | None = None
    stage: EvidenceStage | None = None  # stage this finding supports, if any
    alternatives: list[str] = Field(default_factory=list)
    note: str = ""

    @field_validator("confidence")
    @classmethod
    def _bounds(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be within [0, 1]")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Finding:
        check_confidence(self.label, self.confidence)
        if self.label.is_assertive and not self.evidence_ids:
            raise ValueError(f"label '{self.label.value}' requires at least one evidence ID")
        if self.label is EvidenceLabel.VERIFIED and not self.origin.is_deterministic:
            raise ValueError(
                "AI-origin findings cannot be labelled 'verified'; cite the deterministic "
                "finding instead (ADR-0003)"
            )
        if self.label is EvidenceLabel.PROBABLE and not self.alternatives and self.origin is Origin.AI:
            raise ValueError("AI 'probable' findings must list at least one alternative")
        if self.stage is not None and self.label is not EvidenceLabel.VERIFIED:
            raise ValueError("a finding can only advance a device stage if it is 'verified'")
        return self


# --------------------------------------------------------------------------------------------
# Register
# --------------------------------------------------------------------------------------------
class RegisterError(ValueError):
    pass


class EvidenceRegister(BaseModel):
    scan_id: str
    max_stage: EvidenceStage = INSPECTOR_MAX_STAGE
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)

    # -- evidence -------------------------------------------------------------------------
    def add_evidence(self, ev: Evidence) -> str:
        existing = self.evidence.get(ev.id)
        if existing is not None:
            if existing.sha256 == ev.sha256:
                return ev.id
            raise RegisterError(
                f"evidence ID collision for {ev.id} with different content "
                f"({existing.source} vs {ev.source})"
            )
        self.evidence[ev.id] = ev
        return ev.id

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self.evidence

    # -- findings -------------------------------------------------------------------------
    def add_finding(self, f: Finding) -> Finding:
        missing = [e for e in f.evidence_ids if e not in self.evidence]
        if missing:
            raise RegisterError(f"finding {f.id} cites unknown evidence: {missing}")
        if f.stage is not None and f.stage > self.max_stage:
            raise RegisterError(
                f"finding {f.id} claims stage {f.stage.name} but this context is capped at "
                f"{self.max_stage.name}"
            )
        if any(x.id == f.id for x in self.findings):
            raise RegisterError(f"duplicate finding ID {f.id}")
        self.findings.append(f)
        return f

    def findings_for(self, component: str) -> list[Finding]:
        return [f for f in self.findings if f.component == component]

    def component_stage(self, component: str) -> EvidenceStage | None:
        stages = [f.stage for f in self.findings_for(component) if f.stage is not None]
        return max(stages) if stages else None

    def strongest_label(self, component: str) -> EvidenceLabel:
        ranked = [f.label for f in self.findings_for(component) if f.label in LABEL_RANK]
        if not ranked:
            return EvidenceLabel.UNKNOWN
        return max(ranked, key=lambda lab: LABEL_RANK[lab])
