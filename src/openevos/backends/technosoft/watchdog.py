"""X/Y runaway watchdog, Phase 1: shadow mode (ADR-0047; spec docs/evidence/fl1_v1.6_stage_watchdog_spec.md).

While OpenEVOS moves an axis, the move loop feeds a `LegWatch` with the moving axis' encoder (every tick) and, on
alternate ticks, the drive's own position error `POSERR` and its current `IQ`. The watch evaluates the plan v3 §6.3
"Axis Moving" rule against PROVISIONAL thresholds and records what it WOULD have tripped. In this phase it never
stops, latches or switches anything off: the move loop's existing guards (wrong way at 100 counts, drift monitor,
flags, landing) are unchanged. At the end of each leg one row goes to `motion-profile.csv`; those rows calibrate
the Phase 2 thresholds (spec §4). Qt-free; reads only.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

PROFILE_FILE = "motion-profile.csv"
#: provisional thresholds (spec §4.3): never acted on in Phase 1
FE_LIMIT_COUNTS = 1000  # ERRMAX on X and Y (Gate 9)
BACK_LIMIT_COUNTS = 50  # plan §6.3
IMAXPROT = 17117  # X and Y, set by the drive program (Gate 9)
IQ_LIMIT = round(0.8 * IMAXPROT)  # 13,694
IQ_CONSECUTIVE = 2  # ~130 ms at the alternating read schedule (plan: > 100 ms)
CROSSCHECK_COUNTS = 5  # POSERR at rest against CPOS - APOS at rest
THRESHOLDS = {"fe_limit_counts": FE_LIMIT_COUNTS, "back_limit_counts": BACK_LIMIT_COUNTS, "iq_limit": IQ_LIMIT,
              "iq_consecutive": IQ_CONSECUTIVE, "crosscheck_counts": CROSSCHECK_COUNTS}
COLUMNS = ("utc", "axis", "speed_preset", "start", "target", "distance_counts", "duration_s", "ticks", "tick_hz",
           "fe_reads", "fe_max_abs", "fe_sign_vs_direction", "fe_rest", "cmd_minus_apos_rest", "fe_trusted",
           "iq_reads", "iq_max_abs", "iq_over_limit_s", "back_max", "outcome", "would_trip")


def signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


class LegWatch:
    """One axis, one leg. `t` arguments are monotonic seconds."""

    def __init__(self, axis: str, start: int, target: int, speed_preset: str, t: float):
        self.axis, self.start, self.target, self.speed_preset = axis, start, target, speed_preset
        self.t0 = self.t_last = t
        self.ticks = 0
        self.direction = (target > start) - (target < start)
        self.min_dist = abs(target - start)
        self.back_max = 0
        self.fe_reads = 0
        self.fe_peak = 0  # signed value of the largest |POSERR|
        self.iq_reads = 0
        self.iq_max_abs = 0
        self._iq_run = 0
        self._iq_over_since: float | None = None
        self.iq_over_limit_s = 0.0
        self.would_trip: list[str] = []  # first detection per rule, as "rule: detail"

    def _flag(self, rule: str, detail: str) -> None:
        if not any(w.startswith(rule + ":") for w in self.would_trip):
            self.would_trip.append(f"{rule}: {detail}")

    def apos(self, apos: int, t: float) -> None:
        self.ticks += 1
        self.t_last = t
        dist = abs(self.target - apos)
        self.min_dist = min(self.min_dist, dist)
        back = dist - self.min_dist  # away from the closest approach; an overshoot and its return do not count
        self.back_max = max(self.back_max, back)
        if back > BACK_LIMIT_COUNTS:
            self._flag("runaway_against_command", f"{back} counts back from the closest approach (> "
                                                  f"{BACK_LIMIT_COUNTS}) at {apos}, target {self.target}")

    def poserr(self, raw: int, t: float) -> None:
        fe = signed16(raw)
        self.fe_reads += 1
        if abs(fe) > abs(self.fe_peak):
            self.fe_peak = fe
        if abs(fe) > FE_LIMIT_COUNTS:
            self._flag("runaway_following_error", f"POSERR {fe:+d} (> {FE_LIMIT_COUNTS})")

    def iq(self, raw: int, t: float) -> None:
        iq = signed16(raw)
        self.iq_reads += 1
        self.iq_max_abs = max(self.iq_max_abs, abs(iq))
        if abs(iq) > IQ_LIMIT:
            self._iq_run += 1
            if self._iq_over_since is None:
                self._iq_over_since = t
            self.iq_over_limit_s = max(self.iq_over_limit_s, t - self._iq_over_since)
            if self._iq_run >= IQ_CONSECUTIVE:
                self._flag("runaway_overcurrent", f"|IQ| {abs(iq)} > {IQ_LIMIT} on {self._iq_run} reads")
        else:
            self._iq_run, self._iq_over_since = 0, None

    def row(self, outcome: str, rest: dict | None = None) -> dict:
        """The profile row. `rest`: POSERR, CPOS and APOS read at rest after the leg (None when the leg did not end
        normally): the cross-check that decides whether POSERR can be trusted as the following error."""
        dur = max(self.t_last - self.t0, 0.0)
        fe_rest = cmd_rest = ""
        trusted = False
        if rest is not None:
            fe_rest = signed16(rest["POSERR"])
            cmd_rest = rest["CPOS"] - rest["APOS"]
            trusted = self.fe_reads > 0 and abs(fe_rest - cmd_rest) <= CROSSCHECK_COUNTS
        sign = ("+" if (self.fe_peak > 0) == (self.direction > 0) else "-") if self.fe_peak and self.direction else "0"
        return {"utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "axis": self.axis,
                "speed_preset": self.speed_preset, "start": self.start, "target": self.target,
                "distance_counts": abs(self.target - self.start), "duration_s": round(dur, 3), "ticks": self.ticks,
                "tick_hz": round(self.ticks / dur, 1) if dur > 0 else "",
                "fe_reads": self.fe_reads, "fe_max_abs": abs(self.fe_peak), "fe_sign_vs_direction": sign,
                "fe_rest": fe_rest, "cmd_minus_apos_rest": cmd_rest, "fe_trusted": trusted,
                "iq_reads": self.iq_reads, "iq_max_abs": self.iq_max_abs,
                "iq_over_limit_s": round(self.iq_over_limit_s, 3), "back_max": self.back_max, "outcome": outcome,
                "would_trip": "; ".join(self.would_trip)}


def append(folder: Path | str, row: dict) -> Path:
    p = Path(folder) / PROFILE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)
    return p


def profile(folder: Path | str) -> list[dict]:
    p = Path(folder) / PROFILE_FILE
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
