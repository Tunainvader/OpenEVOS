"""Load the calibrated X/Y limits at app launch when the drives still hold their power-up limits (ADR-0056, amends
ADR-0022; operator decision 2026-11-18 after "stage not moving and objectives" following a power-up).

After every power-on the X and Y drives start with LPOSMIN/LPOSMAX = 0..50,000 counts (0-22 mm), so the stage cannot
reach the centre and objective changes (which need a centred stage) are refused. Motor Gate 7 loads the calibrated
limits into drive RAM. This runs Gate 7's `run()` unchanged, before the app opens the motor port:

* the same four frames only (LPOSMIN/LPOSMAX on X and Y, the values validated from the git-ignored parsed calibration),
  each read back; no motion; Z/F limits checked untouched;
* the same refusals: exactly one FTDI port, normal driver state, line sync, gateway axis 1, every axis homed and idle,
  X and Y at home, current limits either the power-up pair or already the calibration ("already_loaded": nothing
  written);
* evidence written whenever the port was opened.

The launch path supplies the operator confirmations (instrument on, stage clear, phrase): choosing this at launch is
the operator's standing approval. Never retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openevos.maintenance import motor_gate7_xy_limits as g7


@dataclass(frozen=True)
class AutolimitsResult:
    outcome: str  # Gate 7's outcome: "pass" | "already_loaded" | "refused" | "baseline_refused" | ... | "error"
    message: str
    evidence: str = ""

    @property
    def stage_free(self) -> bool:
        return self.outcome in ("pass", "already_loaded")


def _limits_match(res, scans: Path) -> bool:
    """True when Gate 7's baseline read shows X and Y already holding the calibrated limits it planned."""
    base = getattr(res, "baseline", None) or {}
    before, src = base.get("before") or {}, base.get("calibration_file")
    p = Path(scans) / src if src else None
    if not before or p is None or not p.exists():
        return False
    lim, problems = g7.load_limits(p)
    if problems:
        return False
    return all((before.get(ax, {}).get("LPOSMIN"), before.get(ax, {}).get("LPOSMAX")) == tuple(lim[ax])
               for ax, _ in g7.AXES)


def autoload(*, scans: Path, run=g7.run) -> AutolimitsResult:
    dry = run(send=False, scans=scans)
    if dry.outcome != "dry_run" or dry.port is None:
        return AutolimitsResult(dry.outcome, f"calibrated X/Y limits not loaded: {dry.message}")
    res = run(send=True, confirm=g7.confirm_phrase(dry.port.device), on_for_2_min=True, stage_clear=True, scans=scans)
    ev = g7.write_evidence(res, scans).name if res.port_state_read_back else ""
    if res.outcome == "pass":
        return AutolimitsResult("pass", "calibrated X/Y limits loaded and read back (no motion)", ev)
    if res.outcome == "already_loaded" or _limits_match(res, scans):
        # Gate 7 refuses "not at home" before it looks at the limits; limits that already equal the calibration
        # (the stage was moved since they were loaded) need nothing either
        return AutolimitsResult("already_loaded", "calibrated X/Y limits already loaded; nothing written", ev)
    return AutolimitsResult(res.outcome, f"calibrated X/Y limits not loaded: {res.message}", ev)
