"""Physical subsystem readiness, from this repository's gate evidence (not from the simulator).

The UI uses this table on the Hardware page and to explain why a control is gated on hardware. Each
entry states what has been *shown on the instrument*, the evidence, and what must be shown before the
restriction can be lifted. Nothing here enables a physical command: physical commands still need
VERIFIED capability records with evidence IDs (ADR-0009, ADR-0024).

Merged UI (docs/ui/UI_MERGE_ANALYSIS.md): the v0.4.0 table was hand-written from a bring-up log and
marked Z, the F carriage and the LED board "verified"; here every entry is tied to the gate records,
and "read-only" means telemetry verified but no motion/output command ever sent.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class Readiness(str, Enum):
    VERIFIED_LIMITED = "verified-limited"  # commanded on the instrument, inside an explicit envelope
    READ_ONLY = "read-only"  # state/telemetry verified on the instrument; never commanded
    LOCKED = "locked"  # hardware present, not safe to command yet
    NOT_INTEGRATED = "not-integrated"  # no OpenEVOS driver yet
    UNAVAILABLE = "unavailable"  # no telemetry / not present
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {"verified-limited": "Verified (limited)", "read-only": "Read-only (verified)", "locked": "Locked",
                "not-integrated": "Not integrated", "unavailable": "Unavailable", "unknown": "Unknown"}[self.value]


class SubsystemReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subsystem: str
    axis: str | None = None
    readiness: Readiness
    known: str
    gating: str
    unlock_requires: str
    source: str  # evidence: ADR / gate records


PHYSICAL_READINESS: tuple[SubsystemReadiness, ...] = (
    SubsystemReadiness(
        subsystem="XY stage", axis="X/Y", readiness=Readiness.VERIFIED_LIMITED,
        known="Homes at power-on; closed-loop. Moves and returns with 0 counts error (X 1 mm, Y 12 mm); controlled "
              "stop verified at rest and in motion. +X = right, +Y = away from the operator.",
        gating="Moves only with --motor-control, through the safety checks, inside the 0-22 mm power-up box "
               "(the drive clamps to its own limits too).",
        unlock_requires="Gate 7 run (calibrated limits) and a review widening the PC travel box.",
        source="ADR-0016..ADR-0021, ADR-0024; motor-gate4/5/6 pass records"),
    SubsystemReadiness(
        subsystem="Focus drive", axis="Z", readiness=Readiness.READ_ONLY,
        known="Homes at power-on (homed marker read); position and limits read live. Never commanded to move.",
        gating="Focus controls drive the simulated focus only.",
        unlock_requires="A Z move gate with a per-objective safe-Z envelope (parfocality, tpco).",
        source="ADR-0016, ADR-0017; motor-gate1/2 records"),
    SubsystemReadiness(
        subsystem="Filter-cube carriage", axis="F", readiness=Readiness.READ_ONLY,
        known="Homes at power-on (homed marker read); position read live. Never commanded to move.",
        gating="Channel changes move the simulated carriage only.",
        unlock_requires="An F move gate; carriage positions from lico/mcba confirmed against cube detection.",
        source="ADR-0016, ADR-0017"),
    SubsystemReadiness(
        subsystem="Objective turret", axis="O", readiness=Readiness.LOCKED,
        known="Open-loop; moved by raw TML with no limits anywhere (decoded offline). State read only.",
        gating="Objective buttons act on the simulated turret only.",
        unlock_requires="Homing/index method established; per-slot calibration (tpco) verified; own review.",
        source="motor subsystem L0 §3a; ADR-0016"),
    SubsystemReadiness(
        subsystem="Phase / condenser wheel", axis="P", readiness=Readiness.LOCKED,
        known="Open-loop; vendor positioning methods are stubs. State read only.",
        gating="Phase channel and 'Block condenser in fluorescence' act on the simulator only.",
        unlock_requires="Homing/index method established; annulus positions verified per objective.",
        source="motor subsystem L0 §3a"),
    SubsystemReadiness(
        subsystem="LED board", readiness=Readiness.READ_ONLY,
        known="Bootloader 0DAD:0057 -> StartApp -> application 0DAD:0051 verified; calibration sections read and "
              "backed up. No light command has ever been sent.",
        gating="Light controls drive the simulated light only.",
        unlock_requires="Intensity/channel command protocol characterised and a reviewed light gate.",
        source="ADR-0011..ADR-0015"),
    SubsystemReadiness(
        subsystem="Monochrome camera", readiness=Readiness.NOT_INTEGRATED,
        known="Present; no OpenEVOS acquisition driver yet.",
        gating="Live/capture use simulator frames only, labelled SIMULATED.",
        unlock_requires="Camera driver with frame metadata; dark/flat characterisation.",
        source="inventory"),
    SubsystemReadiness(
        subsystem="Color camera", readiness=Readiness.NOT_INTEGRATED,
        known="Present; no OpenEVOS acquisition driver yet.",
        gating="Simulator frames only.",
        unlock_requires="Camera driver; colour gains from lico/coca confirmed.",
        source="inventory"),
    SubsystemReadiness(
        subsystem="Onstage incubator (AMC1000)", readiness=Readiness.UNAVAILABLE,
        known="No telemetry path established; never contacted.",
        gating="Setpoints can be planned; measured temperature/CO2/O2/humidity shown as 'Unavailable'.",
        unlock_requires="Controller protocol read-only telemetry first; control later with its own safety case.",
        source="ADR-0006"),
    SubsystemReadiness(
        subsystem="Calibration record (lico)", readiness=Readiness.READ_ONLY,
        known="Read and parsed field by field: coca, moca, mcba (23 settings), moco, tpco (5 turret selections); "
              "X/Y/Z spans cross-checked (119.1 / 78.1 / 8.4 mm).",
        gating="Read-only; values are displayed as 'recovered', never written back.",
        unlock_requires="Use in calibration-aware workflows after field-by-field confirmation on the unit.",
        source="ADR-0015; fl1_v1.6_pearl_persistent_schema"),
)


def readiness_for(subsystem: str) -> SubsystemReadiness | None:
    return next((r for r in PHYSICAL_READINESS if r.subsystem == subsystem), None)
