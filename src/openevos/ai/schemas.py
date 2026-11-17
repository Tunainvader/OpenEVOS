"""Structured AI triage response (plan §7.4) and its validation against an evidence register.

Two layers of checking:

1. `AITriageResponse` — pydantic schema. Rejects malformed output: bad labels, confidence
   outside the label's band, hypotheses without alternatives, disallowed risk classes,
   AI-minted 'verified' findings.
2. `validate_against_register()` — semantic checks that need the scan: every cited evidence ID
   must exist; claims of "working/operational" need a stage >= RESPONSIVE; recommended checks
   must not describe actuation, transmission, driver changes or disk repair.

The application shows AI output only if both layers pass (or shows it with the violations).
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from openevos.inspector.evidence import (
    EvidenceLabel,
    EvidenceRegister,
    EvidenceStage,
    check_confidence,
)


class RiskClass(str, Enum):
    READ_ONLY = "read_only"  # software inspection, no state change anywhere
    OPERATOR_OBSERVATION = "operator_observation"  # human looks/photographs/records; no actuation


#: v0.1 allows only these. Future classes (e.g. "validated_control") need a new ADR.
ALLOWED_RISK_CLASSES = frozenset({RiskClass.READ_ONLY, RiskClass.OPERATOR_OBSERVATION})

#: AI may use these labels on findings. VERIFIED is reserved for deterministic sources.
AI_ALLOWED_FINDING_LABELS = frozenset(
    {
        EvidenceLabel.STRONGLY_SUPPORTED,
        EvidenceLabel.PROBABLE,
        EvidenceLabel.UNKNOWN,
        EvidenceLabel.NOT_TESTED,
        EvidenceLabel.NOT_APPLICABLE,
    }
)


class AIFinding(BaseModel):
    component: str
    claim: str = Field(min_length=3)
    label: EvidenceLabel
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    cites_findings: list[str] = Field(default_factory=list)  # deterministic finding IDs relied on

    @model_validator(mode="after")
    def _check(self) -> AIFinding:
        if self.label not in AI_ALLOWED_FINDING_LABELS:
            raise ValueError(
                "AI may not label a finding 'verified'. Reference the deterministic finding "
                "via cites_findings instead."
            )
        check_confidence(self.label, self.confidence)
        if self.label.is_assertive and not (self.evidence_ids or self.cites_findings):
            raise ValueError(f"'{self.label.value}' finding must cite evidence or findings")
        return self


class Hypothesis(BaseModel):
    claim: str = Field(min_length=3)
    confidence: float = Field(gt=0.0, lt=1.0)  # a hypothesis is never certain
    alternatives: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class RecommendedCheck(BaseModel):
    action: str = Field(min_length=3)
    risk: RiskClass
    requires_operator: bool
    reduces_uncertainty_about: str | None = None

    @field_validator("risk")
    @classmethod
    def _allowed(cls, v: RiskClass) -> RiskClass:
        if v not in ALLOWED_RISK_CLASSES:
            raise ValueError(f"risk class '{v.value}' not permitted in v0.1")
        return v

    @model_validator(mode="after")
    def _operator(self) -> RecommendedCheck:
        if self.risk is RiskClass.OPERATOR_OBSERVATION and not self.requires_operator:
            raise ValueError("operator_observation checks must set requires_operator=true")
        return self


class AITriageResponse(BaseModel):
    summary: str
    findings: list[AIFinding] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    recommended_checks: list[RecommendedCheck] = Field(default_factory=list)
    prohibited_inferences: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Semantic validation
# --------------------------------------------------------------------------------------------
_FORBIDDEN_ACTION_PATTERNS = [
    r"\bsend\b.*\b(bytes?|packets?|commands?|data)\b",
    r"\bwrite\b.*\b(to|on)\b.*\b(port|endpoint|device|register|eeprom|firmware)\b",
    r"\b(open|connect to)\b.*\bCOM\d+\b",
    r"\b(flash|update|upgrade)\b.*\bfirmware\b",
    r"\b(move|jog|home|drive)\b.*\b(stage|turret|focus|objective|motor)\b",
    r"\b(turn on|enable|switch on|set)\b.*\b(led|illumination|laser|light source)\b",
    r"\b(set|change)\b.*\b(temperature|co2|co₂|setpoint)\b",
    r"\b(install|uninstall|remove|update|roll ?back)\b.*\bdrivers?\b",
    r"\b(chkdsk|fsck|diskpart|format|initiali[sz]e)\b",
    r"\b(regedit|reg add|reg delete|registry edit)\b",
    r"\bstart\b.*\bacquisition\b",
]
_FORBIDDEN_ACTION_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_ACTION_PATTERNS]

_WORKING_CLAIM_RE = re.compile(
    r"\b(is|are)\s+(working|operational|functional|functioning|healthy|ok)\b", re.IGNORECASE
)


class Violation(BaseModel):
    where: str
    message: str


def validate_against_register(
    resp: AITriageResponse, register: EvidenceRegister
) -> list[Violation]:
    out: list[Violation] = []
    finding_ids = {f.id for f in register.findings}

    def _cite(where: str, ids: list[str]) -> None:
        for e in ids:
            if not register.has(e):
                out.append(Violation(where=where, message=f"cites unknown evidence '{e}'"))

    for i, f in enumerate(resp.findings):
        where = f"findings[{i}]"
        _cite(where, f.evidence_ids)
        for fid in f.cites_findings:
            if fid not in finding_ids:
                out.append(Violation(where=where, message=f"cites unknown finding '{fid}'"))
        if _WORKING_CLAIM_RE.search(f.claim):
            stage = register.component_stage(f.component)
            if stage is None or stage < EvidenceStage.RESPONSIVE:
                out.append(
                    Violation(
                        where=where,
                        message=(
                            "claims the component is working, but its evidence stage is "
                            f"{stage.name if stage else 'none'} (needs RESPONSIVE or higher)"
                        ),
                    )
                )

    for i, h in enumerate(resp.hypotheses):
        _cite(f"hypotheses[{i}]", h.evidence_ids)

    for i, c in enumerate(resp.recommended_checks):
        for rx in _FORBIDDEN_ACTION_RE:
            if rx.search(c.action):
                out.append(
                    Violation(
                        where=f"recommended_checks[{i}]",
                        message=f"action describes a prohibited operation (matched /{rx.pattern}/)",
                    )
                )
                break
    return out
