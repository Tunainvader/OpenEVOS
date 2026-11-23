"""Stage drift monitor: the X/Y cross-axis feedback integrity monitor (Y-axis plan v3 §6, P0; ADR-0040).

Why: Y is a closed-loop stepper commutated from the same encoder that gives APOS (6.25 deg electrical per count).
While only X moved, Y's count changed 0 -> -3 -> -8 -> -9 (2026-11-13). Past ~14 counts Y's torque reverses, and its
next move runs the wrong way or trips. The monitor cannot fix that; it contains it.

What it does (no serial I/O and no Qt; the TelemetryService feeds it from its own thread):
* keeps the last 40 s of fast X/Y encoder samples (~20 Hz) and of the slow status polls (~1 Hz) in memory (plan T0);
* latches on (plan §6.3):
    - idle_encoder_drift    an axis the host is not moving changes more than 2 counts from its reference
    - command_mismatch      |APOS - CPOS| > 3 counts on an axis about to move
    - overcurrent_trip      ISTATUSFLAGS bit 0 (the drive program's over-current handler; MER reads 0 after it)
    - oscillation_axis_off  ISTATUSFLAGS bit 13 (the drive switched the axis off after a move)
    - comm_error_abort      ISTATUSFLAGS bit 4
    - power_stage_off       SRL bit 15 clear on a homed axis (OpenEVOS never switches X or Y off)
    - wrong_direction, landing_error, cross_axis_encoder_drift: reported by the move loop through `trip()`
* while latched, every X/Y move is refused, whichever axis;
* the latch never clears itself, not even when APOS and CPOS agree again (the electrical phase can still be wrong).
  An observed re-initialisation of both X and Y clears it: IINITONCEDONE leaves 12345 and comes back, which is
  the power-on homing seen while OpenEVOS is connected. A small drift or landing latch can also be released by the
  operator (`release`, ADR-0057, ADR-0061) after fresh reads pass. With an evidence folder the latch survives app
  restarts;
* 10 s after a latch it writes the evidence: a JSON (latch, thresholds, status polls, commands, extra drive reads)
  and a CSV in the `y_drift_correlate.py` format (t, x_apos, y_apos, x_cpos, y_cpos, x_cmd, y_cmd).

It only inhibits: stopping a running move is the service's job. It never homes, resets or switches anything.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openevos.backends.technosoft import protocol as tp

#: protected thresholds (plan §6.3): recorded with every snapshot; changing one needs an ADR
IDLE_DRIFT_MAX_COUNTS = 2
#: a still axis knocked by the OTHER axis' move up to this far is tracked and put back, not latched (operator
#: 2026-11-19: "when we already know the creep, can't we keep track and correct it"). The same bound as the operator
#: release (ADR-0057), well under the ~14-count commutation reversal (ADR-0030). At rest the 2-count rule stays.
CROSS_AXIS_TOLERANCE_COUNTS = 10
MOVE_DRIFT_MAX_COUNTS = 3
SETTLE_S = 0.5  # after OpenEVOS ends a move, the axis' reference is taken this much later
RING_S = 40.0  # 30 s before an event + the 10 s written after it
POST_EVENT_S = 10.0
FAST_PERIOD_S = 0.05  # X and Y APOS at ~20 Hz
ISF_OVERCURRENT, ISF_COMM_ABORT, ISF_OSCILLATION = 0x0001, 0x0010, 0x2000
SRL_AXIS_ON = 0x8000
AXES = ("X", "Y")
LATCH_FILE = "stage-latch.json"
CSV_COLUMNS = ("t", "x_apos", "y_apos", "x_cpos", "y_cpos", "x_cmd", "y_cmd")
THRESHOLDS = {"idle_drift_max_counts": IDLE_DRIFT_MAX_COUNTS, "move_drift_max_counts": MOVE_DRIFT_MAX_COUNTS,
              "settle_s": SETTLE_S, "fast_period_s": FAST_PERIOD_S, "ring_s": RING_S, "post_event_s": POST_EVENT_S}
RECOVERY = ("power-cycle the microscope: with OpenEVOS running it watches X and Y re-home and clears the lock; with "
            "OpenEVOS closed, the next launch clears it when it finds the drives freshly powered up and homed")
#: Operator release without a power cycle (ADR-0057, amends ADR-0040; operator 2026-11-18: "can't we manually control
#: the stage even after lock"). Only drift-type latches, only a small recorded drift (well under the ~14-count
#: commutation reversal), and only when fresh reads show both axes homed, idle, fault-free, powered and within
#: MOVE_DRIFT_MAX_COUNTS of their command (else the next move's pre-move check would latch again).
#: ADR-0061 (operator 2026-11-22): a landing lock too, by its recorded miss (encoder vs target). The move loop now
#: lets a landing settle and re-approaches once before it locks, so such a lock is a repeated small miss.
RELEASABLE_RULES = frozenset({"idle_encoder_drift", "command_mismatch", "cross_axis_encoder_drift", "landing_error"})
RELEASE_MAX_DRIFT_COUNTS = 10
#: ADR-0061: the third automatic re-approach of one axis in a session raises a standing warning (never a lock)
REAPPROACH_WARN_AFTER = 3
#: ADR-0061: one row per settled miss, re-approach or landing lock, next to the evidence (where each axis drags)
LANDING_LOG_FILE = "landing-log.csv"
LANDING_COLUMNS = ("utc", "axis", "speed_preset", "start", "target", "position_mm", "miss_at_complete",
                   "miss_after_settle", "settle_s", "iq", "action", "detail")
RELEASE_READS = ("IINITONCEDONE", "ESTATEV", "SRH", "SRL", "ISTATUSFLAGS", "APOS", "CPOS")
#: Software re-home (ADR-0058, amends ADR-0040): the latch also clears when the stage service sent X and Y the drives'
#: own home request (ESTATE 19, the power-up homing routine) and fresh reads show both homed, idle, fault-free,
#: powered and at 0. ESTATE 19 leaves IINITONCEDONE at 12345, so the power-cycle rule above cannot see it.
REHOME_READS = ("IINITONCEDONE", "ESTATEV", "HOMEDONE", "ISTATUSFLAGS", "SRL", "SRH", "MER", "APOS", "CPOS")
REHOME_HOME_BAND = 5
#: Since 2026-11-19 Y's homing completes fault-free but Y stops 14-31 counts short of 0 against its command 0 (something
#: at its home edge, where most of its locks are; it read -1 that morning). A homed, fault-free axis whose command is 0
#: and whose encoder stands off on the travel side by up to this many counts (26 um) is a stand-off, not a failure: the
#: service parks it away from home with a verified leg while still locked, and the latch clears only if that leg lands
#: in its band (the axis follows its command), with a standing warning. Further out, or past 0, the latch stays.
REHOME_STANDOFF_MAX_COUNTS = 60
STANDOFF_WARNING = "rehome_standoff"
#: extra reads taken the moment a latch is set, before anything else is sent (plan T0 read order)
EXTRA_READS = ("ISTATUSFLAGS", "SRL", "MER", "IIQMAX", "IIQMIN", "IQ", "LCOMMERRPOS", "APOS", "CPOS")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Latch:
    rule: str
    axis: str
    message: str
    at_utc: str
    values: dict = field(default_factory=dict)
    evidence: str = ""


class StageMonitor:
    def __init__(self, evidence_dir: Path | str | None = None, *, clock: Callable[[], float] = time.monotonic,
                 record: bool = False):
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self._clock = clock
        self._t0 = clock()
        self.started_utc = _utc()
        self._lock = threading.RLock()  # the UI thread reads state() while the service thread feeds samples
        self.samples: deque[tuple] = deque()
        self.polls: deque[tuple[float, dict]] = deque()
        self.commands: deque[tuple[float, str]] = deque(maxlen=500)
        self.ref: dict[str, int | None] = dict.fromkeys(AXES)
        self.cpos: dict[str, int | None] = dict.fromkeys(AXES)
        self.moving: set[str] = set()
        self.settle_until = dict.fromkeys(AXES, 0.0)
        self.homed = dict.fromkeys(AXES, False)
        self.status_seen = False
        self.warnings: dict[str, str] = {}
        self.latch: Latch | None = None
        self.extra: dict = {}
        self.sample_count = 0
        self.evidence_written: list[Path] = []
        self._rehomed = dict.fromkeys(AXES, False)
        self._evidence_due: float | None = None
        self._record: _RowWriter | None = None
        self.record_path: Path | None = None
        self.carried_over = False  # the latch came from the latch file (an earlier session), not from this one
        #: still axes knocked (within CROSS_AXIS_TOLERANCE_COUNTS) by the move in progress: axis -> largest change
        self.disturbed: dict[str, int] = {}
        #: axis -> where (mm) each automatic re-approach after a landing miss happened this session (ADR-0061)
        self.reapproaches: dict[str, list[float]] = {}
        self._load_latch()
        if record and self.evidence_dir is not None:
            self._open_record()

    # ------------------------------------------------------------------ queries (any thread)
    @property
    def latched(self) -> bool:
        return self.latch is not None

    def refusal(self) -> str:
        with self._lock:
            if self.latch is None:
                return ""
            return (f"stage motion locked by the drift monitor since {self.latch.at_utc[:19]}Z "
                    f"({self.latch.rule}, {self.latch.axis}: {self.latch.message}); {RECOVERY}")

    def state(self) -> dict:
        with self._lock:
            out = {"latched": self.latch is not None, "warnings": list(self.warnings.values()),
                   "fast_samples": self.sample_count, "recording": str(self.record_path or ""),
                   "reapproaches": {ax: list(v) for ax, v in self.reapproaches.items()}}  # ADR-0061, mm per axis
            if self.latch is not None:
                out.update(rule=self.latch.rule, axis=self.latch.axis, message=self.latch.message,
                           since_utc=self.latch.at_utc, evidence=self.latch.evidence, recovery=RECOVERY,
                           releasable=self._release_blocker() == "")
            return out

    def latch_drift(self) -> int | None:
        """The drift (counts) the latch recorded: encoder vs its reference (idle) or vs its command (pre-move)."""
        v = self.latch.values if self.latch else {}
        if "apos" in v and "reference" in v:
            return int(v["apos"]) - int(v["reference"])
        if "apos" in v and "target" in v:  # a landing (or wrong-way) lock: the encoder against the leg's target
            return int(v["apos"]) - int(v["target"])
        if "apos" in v and "start" in v:  # cross-axis drift during the other axis' move
            return int(v["apos"]) - int(v["start"])
        if "apos" in v and "cpos" in v:
            return int(v["apos"]) - int(v["cpos"])
        if "drift" in v:
            return int(v["drift"])
        return None

    def _release_blocker(self) -> str:
        if self.latch is None:
            return "the stage is not locked"
        if self.latch.rule not in RELEASABLE_RULES:
            return f"a {self.latch.rule} lock cannot be released from the app: {RECOVERY}"
        d = self.latch_drift()
        if d is None:
            return f"the lock recorded no drift value: {RECOVERY}"
        if abs(d) > RELEASE_MAX_DRIFT_COUNTS:
            return (f"the recorded drift {d:+d} counts is over {RELEASE_MAX_DRIFT_COUNTS} (commutation risk near ~14): "
                    f"{RECOVERY}")
        return ""

    def release(self, values: dict[str, dict[str, int]], who: str = "operator") -> str:
        """Operator release of a small drift latch (see RELEASABLE_RULES). `values`: fresh RELEASE_READS of X and Y,
        read by the service just now. '' = released (evidence written); otherwise why not (the latch stays)."""
        with self._lock:
            why = self._release_blocker()
            if why:
                return why
            problems = []
            for ax in AXES:
                v = values.get(ax) or {}
                if any(n not in v for n in RELEASE_READS):
                    problems.append(f"{ax}: reads missing")
                    continue
                if v["IINITONCEDONE"] != tp.INIT_DONE_MARKER:
                    problems.append(f"{ax} not homed")
                if v["ESTATEV"] not in (0, 13) or not v["SRH"] & tp.SRH_MOTION_COMPLETE_BIT:
                    problems.append(f"{ax} not idle (state {v['ESTATEV']})")
                hit = self._flag_rule(ax, v["ISTATUSFLAGS"], v["SRL"])
                if hit:
                    problems.append(hit[2])
                d = v["APOS"] - v["CPOS"]
                if abs(d) > MOVE_DRIFT_MAX_COUNTS:
                    problems.append(f"{ax} is {d:+d} counts from its command (> {MOVE_DRIFT_MAX_COUNTS}): its next move "  # noqa: E501
                                    "would lock again")
            if problems:
                return "; ".join(problems) + f": {RECOVERY}"
            latch = self.latch
            if latch is None:  # _release_blocker refuses this already; kept so the release cannot act on no lock
                return "the stage is not locked"
            latch.values["release_reads"] = {ax: dict(values[ax]) for ax in AXES}
            self._clear(f"{who} release: {latch.rule} on {latch.axis}, recorded drift "
                        f"{self.latch_drift():+d} counts; X and Y read homed, idle, fault-free, powered and within "
                        f"{MOVE_DRIFT_MAX_COUNTS} counts of their command")
            return ""

    # ------------------------------------------------------------------ software re-home (ADR-0058)
    def rehome_blocker(self) -> str:
        """'' when the latch may be cleared by an app-started re-home of X and Y (any lock: it re-runs the drives'
        power-up homing, ESTATE 19); otherwise why not."""
        with self._lock:
            if self.latch is None:
                return "the stage is not locked"
            return ""

    def begin_rehome(self, axis: str) -> None:
        """The stage service is about to send `axis` its home request: the axis moves with no move command, so the
        idle-drift rule must not watch it."""
        with self._lock:
            self.moving.add(axis)
            self.ref[axis] = None
            self.commands.append((round(self._now(), 3), f"software re-home of {axis} requested (ESTATE 19)"))

    def end_rehome(self, axis: str, why: str) -> None:
        self.end_move([axis], f"re-home: {why}")

    def _rehome_problems(self, values: dict[str, dict[str, int]],
                         parked: dict[str, int]) -> tuple[list[str], dict[str, int]]:
        """What stops the latch clearing after a re-home, and the stand-offs (axis -> APOS at home, command 0) found.
        An axis in `parked` has since been moved off home by the service: it must be idle and on its command."""
        problems: list[str] = []
        standoff: dict[str, int] = {}
        for ax in AXES:
            v = values.get(ax) or {}
            if any(n not in v for n in REHOME_READS):
                problems.append(f"{ax}: reads missing")
                continue
            if v["IINITONCEDONE"] != tp.INIT_DONE_MARKER:
                problems.append(f"{ax} not initialised")
            idle = v["ESTATEV"] in (0, 13) if ax in parked else v["ESTATEV"] == 0
            if not v["HOMEDONE"] & 1 or not idle:
                problems.append(f"{ax} not homed (HOMEDONE {v['HOMEDONE']}, state {v['ESTATEV']})")
            if v["ISTATUSFLAGS"] & tp.ISF_HOME_INTERRUPTED:
                problems.append(f"{ax} homing was interrupted (ISTATUSFLAGS 0x{v['ISTATUSFLAGS']:04X})")
            if v["MER"]:
                problems.append(f"{ax} drive fault MER 0x{v['MER']:04X}")
            hit = self._flag_rule(ax, v["ISTATUSFLAGS"], v["SRL"])
            if hit:
                problems.append(hit[2])
            apos, cpos = v["APOS"], v["CPOS"]
            if ax in parked:
                if abs(apos - cpos) > MOVE_DRIFT_MAX_COUNTS:
                    problems.append(f"{ax} parked off its command (APOS {apos}, CPOS {cpos})")
            elif abs(cpos) <= REHOME_HOME_BAND < apos <= REHOME_STANDOFF_MAX_COUNTS:
                standoff[ax] = apos
            elif abs(apos) > REHOME_HOME_BAND or abs(cpos) > REHOME_HOME_BAND:
                problems.append(f"{ax} not at home after homing (APOS {apos}, CPOS {cpos})")
        return problems, standoff

    def rehome_standoffs(self, values: dict[str, dict[str, int]]) -> tuple[str, dict[str, int]]:
        """The reads at home (REHOME_READS) right after both axes homed: ('' or why the latch must stay, the axes that
        stand off home within REHOME_STANDOFF_MAX_COUNTS, which the service must park before `rehome_done`)."""
        with self._lock:
            problems, standoff = self._rehome_problems(values, {})
            return "; ".join(problems), standoff

    def rehome_done(self, values: dict[str, dict[str, int]], who: str = "operator",
                    parked: dict[str, int] | None = None) -> str:
        """Both axes' fresh reads (REHOME_READS) right after the service saw each one home (HOMEDONE cleared by the
        request, then set again with ESTATEV 0), or, for the axes in `parked` (axis -> its stand-off at home), after
        the service moved them off home with a verified leg. '' = the latch is cleared (evidence written); else why
        it stays."""
        parked = dict(parked or {})
        with self._lock:
            problems, standoff = self._rehome_problems(values, parked)
            problems += [f"{ax} not at home after homing (APOS {values[ax]['APOS']}, CPOS {values[ax]['CPOS']})"
                         for ax in standoff]  # a stand-off clears only once parked
            if problems:
                return "; ".join(problems)
            if self.latch is not None:
                self.latch.values["rehome_reads"] = {ax: dict(values[ax]) for ax in AXES}
                how = "both homed, idle, fault-free, powered and at 0"
                if parked:
                    self.latch.values["rehome_standoff"] = parked
                    how = ("both homed, idle, fault-free and powered; " + ", ".join(
                        f"{ax} stood {d} counts short of 0 and was parked with a verified leg" for ax, d in
                        parked.items()))
                self._clear(f"software re-home of X and Y by {who} (ESTATE 19, ADR-0058): {how}")
            if parked:
                self.warnings[STANDOFF_WARNING] = "; ".join(
                    f"{ax} homed {d} counts ({d * 1.27e3 / 2880:.0f} um) short of 0 at {_utc()[:19]}Z: something "
                    f"may be at {ax}'s home edge; check it by hand. Moving {ax} back to 0 may lock the stage again"
                    for ax, d in parked.items())
            else:
                self.warnings.pop(STANDOFF_WARNING, None)
            for ax in AXES:
                self.homed[ax] = True
            return ""

    # ------------------------------------------------------------------ feeding (service thread)
    def _now(self) -> float:
        return self._clock() - self._t0

    def note(self, text: str) -> None:
        with self._lock:
            self.commands.append((round(self._now(), 3), text))

    def warn(self, key: str, text: str) -> None:
        """A standing warning shown on the stage (not a latch), e.g. a power-up index jump (ADR-0040 §9)."""
        with self._lock:
            self.warnings[key] = text

    def landing_reapproached(self, axis: str, where_mm: float) -> int:
        """The move loop re-approached `axis` after a landing miss and it landed (ADR-0061). Returns how many times
        this session; from the REAPPROACH_WARN_AFTER-th on, a standing warning names where (not a lock)."""
        with self._lock:
            seen = self.reapproaches.setdefault(axis, [])
            seen.append(round(where_mm, 1))
            if len(seen) >= REAPPROACH_WARN_AFTER:
                where = ", ".join(f"{m:.1f}" for m in seen[-6:])
                self.warnings[f"landing {axis}"] = (
                    f"{axis} needed {len(seen)} automatic re-approaches this session (at {where} mm): it lands short "
                    f"of its target there; check {axis}'s guide and cable by hand")
            return len(seen)

    def fast(self, x_apos: int, y_apos: int) -> Latch | None:
        """One fast X/Y encoder sample. Returns the latch if this sample set it."""
        t = self._now()
        with self._lock:
            if not self.status_seen:
                return None  # nothing to compare with yet (and no CPOS for the log)
            row = (round(t, 3), x_apos, y_apos, self.cpos["X"], self.cpos["Y"], int("X" in self.moving),
                   int("Y" in self.moving))
            self.samples.append(row)
            self.sample_count += 1
            self._trim(t)
            if self._record is not None:
                self._record.writerow(row)
            for ax, v in zip(AXES, (x_apos, y_apos), strict=True):
                if self.latch is not None or not self.homed[ax] or ax in self.moving:
                    continue
                ref = self.ref[ax]
                if ref is None:  # the settle time only delays taking a reference; one set by begin_move counts at once
                    if t >= self.settle_until[ax]:
                        self.ref[ax] = v
                    continue
                d = v - ref
                others = sorted(a for a in self.moving if a != ax)
                if others and IDLE_DRIFT_MAX_COUNTS < abs(d) <= CROSS_AXIS_TOLERANCE_COUNTS:
                    # a known, bounded knock from the other axis' move (2026-11-18: Y -2..-6 counts each time X passed
                    # ~33-41 mm at full speed): tracked, and corrected by the stage service after the move
                    if abs(d) > abs(self.disturbed.get(ax, 0)):
                        self.disturbed[ax] = d
                        self.commands.append((round(t, 3), (f"{ax} knocked {d:+d} counts while {'+'.join(others)} "
                                                            "moved (tracked; corrected after the move)")))
                    continue
                if abs(d) > IDLE_DRIFT_MAX_COUNTS:
                    when = f"while {'+'.join(others)} moved" if others else "while the stage was at rest"
                    return self._trip("idle_encoder_drift", ax,
                                      f"{ax} encoder changed {d:+d} counts {when} with no {ax} command ({ref} -> {v})",
                                      {"reference": ref, "apos": v, "moving": others})
        return None

    def status(self, values: dict[str, dict[str, int]]) -> Latch | None:
        """One slow status poll (any subset of ESTATEV, IINITONCEDONE, SRH, SRL, ISTATUSFLAGS, APOS, CPOS, ...)."""
        t = self._now()
        new = None
        with self._lock:
            self.polls.append((round(t, 3), {ax: dict(values[ax]) for ax in AXES if ax in values}))
            self._trim(t)
            self.status_seen = True
            for ax in AXES:
                v = values.get(ax)
                if not v:
                    continue
                if "CPOS" in v:
                    self.cpos[ax] = v["CPOS"]
                if v.get("IINITONCEDONE") != tp.INIT_DONE_MARKER:
                    if self.homed[ax]:
                        self.commands.append((round(t, 3), f"{ax} re-initialising (IINITONCEDONE {v.get('IINITONCEDONE')})"))  # noqa: E501
                    self.homed[ax], self.ref[ax] = False, None
                    self.warnings.pop(ax, None)
                    if self.latch is not None:
                        self._rehomed[ax] = True
                    continue
                if not self.homed[ax]:
                    self.homed[ax], self.ref[ax] = True, None
                    self.settle_until[ax] = t + SETTLE_S
                if self.latch is None:
                    hit = self._flag_rule(ax, v.get("ISTATUSFLAGS"), v.get("SRL"))
                    if hit:
                        new = self._trip(*hit)
                if "APOS" in v and "CPOS" in v and ax not in self.moving:
                    d = v["APOS"] - v["CPOS"]
                    if abs(d) > MOVE_DRIFT_MAX_COUNTS:
                        self.warnings[ax] = (f"{ax} is {d:+d} counts from its command: a move of {ax} would be "
                                             "refused and lock the stage")
                    else:
                        self.warnings.pop(ax, None)
            if self.latch is not None and all(self._rehomed.values()) and all(self.homed.values()):
                self._clear("re-initialisation of X and Y observed (power-on homing)")
        return new

    def begin_move(self, targets: dict[str, int], positions: dict[str, int]) -> None:
        """OpenEVOS is about to move the axes in `targets` (counts). A still axis without a reference takes its
        fresh position (`positions`, read just before) now."""
        t = self._now()
        with self._lock:
            for ax in AXES:
                if ax in targets:
                    self.moving.add(ax)
                    self.ref[ax] = None
                elif self.homed[ax] and self.ref[ax] is None and ax in positions:
                    self.ref[ax] = positions[ax]
            self.commands.append((round(t, 3), "move " + ", ".join(f"{a} {positions.get(a)} -> {c}"
                                                                    for a, c in targets.items())))

    def end_move(self, axes: Iterable[str], why: str = "done") -> None:
        t = self._now()
        with self._lock:
            for ax in axes:
                self.moving.discard(ax)
                self.ref[ax] = None
                self.settle_until[ax] = t + SETTLE_S
            if not self.moving:  # a knocked still axis takes a fresh reference (the service puts it back)
                for ax in self.disturbed:
                    self.ref[ax] = None
                    self.settle_until[ax] = t + SETTLE_S
            self.commands.append((round(t, 3), f"move {'+'.join(axes)} ended: {why}"))

    def record_disturbed(self, axis: str, d: int) -> None:
        """The stage service's end-of-leg backstop saw `axis` knocked by `d` counts (within the tolerance)."""
        with self._lock:
            if abs(d) > abs(self.disturbed.get(axis, 0)):
                self.disturbed[axis] = d

    def take_disturbed(self) -> dict[str, int]:
        """The still axes knocked by the move just finished (and forget them)."""
        with self._lock:
            out, self.disturbed = dict(self.disturbed), {}
            return out

    def pre_move(self, axis: str, apos: int, cpos: int) -> str:
        """Rule 2 on an axis about to move: refuse (and latch) when its encoder is off its command."""
        d = apos - cpos
        if abs(d) <= MOVE_DRIFT_MAX_COUNTS:
            return ""
        msg = f"{axis} encoder drift {d:+d} counts between position and command (> {MOVE_DRIFT_MAX_COUNTS}) before a move"  # noqa: E501
        self.trip("command_mismatch", axis, msg, {"apos": apos, "cpos": cpos})
        return msg

    def after_move(self, axis: str, istatusflags: int, srl: int) -> Latch | None:
        """The drive's own record right after a move (plan §6.7: read ISTATUSFLAGS after every move)."""
        with self._lock:
            if self.latch is not None:
                return None
            hit = self._flag_rule(axis, istatusflags, srl)
            return self._trip(*hit) if hit else None

    def trip(self, rule: str, axis: str, message: str, values: dict | None = None) -> Latch | None:
        with self._lock:
            return self._trip(rule, axis, message, values or {})

    def link_lost(self) -> None:
        """No reference survives a gap in the samples: the next status poll starts afresh (CPOS still catches a
        drift that happened meanwhile, through the pre-move check)."""
        with self._lock:
            self.ref = dict.fromkeys(AXES)
            self.status_seen = False
            self.commands.append((round(self._now(), 3), "link lost"))

    def add_extra(self, reads: dict) -> None:
        with self._lock:
            self.extra = {"read_at_s": round(self._now(), 3), **reads}
            if self.latch is not None:
                self.latch.values.setdefault("drive", reads)
                self._save_latch()

    # ------------------------------------------------------------------ evidence
    def evidence_due(self) -> bool:
        return self._evidence_due is not None and self._now() >= self._evidence_due

    def write_evidence(self, *, force: bool = False) -> Path | None:
        """The snapshot JSON + CSV, once 10 s have passed after the latch (or at once with `force`, on exit)."""
        with self._lock:
            if self._evidence_due is None or (not force and self._now() < self._evidence_due):
                return None
            self._evidence_due = None
            if self.evidence_dir is None or self.latch is None:
                return None
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            base = self.evidence_dir / f"stage-monitor-{stamp}-{self.latch.rule}"
            try:
                self.evidence_dir.mkdir(parents=True, exist_ok=True)
                self.export_csv(base.with_suffix(".csv"))
                record = {"kind": "openevos.stage_monitor_snapshot", "version": 1, "monitor_started_utc": self.started_utc,  # noqa: E501
                          "written_utc": _utc(), "latch": asdict(self.latch), "thresholds": THRESHOLDS,
                          "recovery": RECOVERY, "csv": base.with_suffix(".csv").name,
                          "csv_columns": list(CSV_COLUMNS),
                          "csv_note": "x_cpos/y_cpos are the last values read by the ~1 Hz status poll; x_cmd/y_cmd "
                                      "= 1 while OpenEVOS was moving that axis",
                          "extra_drive_reads": self.extra, "status_polls": list(self.polls),
                          "commands": list(self.commands), "software": _software_version()}
                base.with_suffix(".json").write_text(json.dumps(record, indent=1), encoding="utf-8")
            except OSError:
                return None
            self.latch.evidence = str(base.with_suffix(".json"))
            self._save_latch()
            self.evidence_written.append(base.with_suffix(".json"))
            return base.with_suffix(".json")

    def export_csv(self, path: Path) -> Path:
        with self._lock:
            rows = list(self.samples)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLUMNS)
            w.writerows(_csv_row(r) for r in rows)
        return path

    def close(self) -> None:
        with self._lock:
            if self._record is not None:
                self._record_fh.close()
                self._record = None

    # ------------------------------------------------------------------ internals
    def _flag_rule(self, ax: str, isf: int | None, srl: int | None) -> tuple[str, str, str, dict[str, int]] | None:
        if isf is not None and isf & ISF_OVERCURRENT:
            msg = (f"{ax} drive over-current trip recorded (ISTATUSFLAGS 0x{isf:04X} bit 0; the drive switched {ax} "
                   "off; MER reads 0 after this)")
            return ("overcurrent_trip", ax, msg, {"ISTATUSFLAGS": isf})
        if isf is not None and isf & ISF_OSCILLATION:
            msg = f"{ax} oscillated after a move and the drive switched it off (ISTATUSFLAGS 0x{isf:04X} bit 13)"
            return ("oscillation_axis_off", ax, msg, {"ISTATUSFLAGS": isf})
        if isf is not None and isf & ISF_COMM_ABORT:
            msg = f"{ax} drive aborted on a communication error (ISTATUSFLAGS 0x{isf:04X} bit 4)"
            return ("comm_error_abort", ax, msg, {"ISTATUSFLAGS": isf})
        if srl is not None and not srl & SRL_AXIS_ON:
            msg = f"{ax} power stage is off (SRL 0x{srl:04X}, bit 15 clear) although OpenEVOS never switches {ax} off"
            return ("power_stage_off", ax, msg, {"SRL": srl})
        return None

    def _trip(self, rule: str, axis: str, message: str, values: dict) -> Latch | None:
        t = round(self._now(), 3)
        if self.latch is not None:
            self.commands.append((t, f"also {rule} on {axis}: {message}"))
            return None
        self.latch = Latch(rule, axis, message, _utc(), dict(values))
        self._rehomed = dict.fromkeys(AXES, False)
        self._evidence_due = self._now() + POST_EVENT_S
        self.commands.append((t, f"LATCH {rule} on {axis}: {message}"))
        self._save_latch()
        return self.latch

    def _clear(self, why: str) -> None:
        self.commands.append((round(self._now(), 3), f"latch cleared: {why}"))
        old, self.latch = self.latch, None
        self.ref = dict.fromkeys(AXES)
        if self.evidence_dir is not None and old is not None:
            p = self.evidence_dir / LATCH_FILE
            try:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                cleared = {**asdict(old), "cleared_utc": _utc(), "cleared_by": why}
                (self.evidence_dir / f"stage-latch-cleared-{stamp}.json").write_text(json.dumps(cleared, indent=1),
                                                                                     encoding="utf-8")
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def _save_latch(self) -> None:
        if self.evidence_dir is None or self.latch is None:
            return
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            (self.evidence_dir / LATCH_FILE).write_text(json.dumps(asdict(self.latch), indent=1), encoding="utf-8")
        except OSError:
            pass  # the latch still holds in memory

    def _load_latch(self) -> None:
        if self.evidence_dir is None:
            return
        p = self.evidence_dir / LATCH_FILE
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            self.latch = Latch(**{k: d[k] for k in ("rule", "axis", "message", "at_utc")},
                               values=d.get("values", {}), evidence=d.get("evidence", ""))
        except (OSError, ValueError, KeyError, TypeError) as exc:  # fail closed
            self.latch = Latch("latch_file_unreadable", "XY", f"{p} could not be read ({exc})", _utc())
        self.carried_over = True
        self.commands.append((0.0, f"latch carried over from an earlier session: {self.latch.rule} on {self.latch.axis}"))  # noqa: E501

    def power_cycle_seen(self, evidence: str) -> bool:
        """The launch found the drives freshly powered up and homed (Motor Gate 7 wrote the calibrated limits over the
        power-up ones, which it does only with X and Y at home; a drive keeps those limits until it loses power). That
        is the same proof as watching X and Y re-home (RECOVERY), taken while OpenEVOS was closed (operator 2026-11-18:
        power cycles happened with the app closed and the lock carried over). Clears only a latch carried over from an
        earlier session; a latch set in this session needs the live re-home. The drive-flag rules still run on every
        status poll, so a fault that survived the power cycle locks the stage again. True when cleared."""
        with self._lock:
            if self.latch is None or not self.carried_over:
                return False
            self._clear(f"power cycle found at launch ({evidence})")
            self.carried_over = False
            return True

    def _open_record(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        folder = self.evidence_dir
        if folder is None:  # memory only: nothing to record to
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.record_path = folder / f"stage-log-{stamp}.csv"
            self._record_fh = open(self.record_path, "w", newline="", encoding="utf-8")  # noqa: SIM115 - kept open
            writer = _RowWriter(csv.writer(self._record_fh))
            writer.writerow(CSV_COLUMNS, raw=True)
            self._record = writer
        except OSError:
            self._record, self.record_path = None, None

    def _trim(self, t: float) -> None:
        while self.samples and self.samples[0][0] < t - RING_S:
            self.samples.popleft()
        while self.polls and self.polls[0][0] < t - RING_S:
            self.polls.popleft()


def append_landing(folder: Path | str, row: dict) -> Path:
    """One row of the landing log (ADR-0061), with the header on a new file. Unknown keys are dropped."""
    p = Path(folder) / LANDING_LOG_FILE
    new = not p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LANDING_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({"utc": _utc(), **row})
    return p


class _RowWriter:
    def __init__(self, writer):
        self._w = writer

    def writerow(self, row: tuple[Any, ...], raw: bool = False) -> None:
        self._w.writerow(row if raw else _csv_row(row))


def _csv_row(row: tuple) -> tuple:
    return tuple("" if v is None else v for v in row)


def _software_version() -> str:
    try:
        from importlib.metadata import version

        return version("openevos")
    except Exception:  # noqa: BLE001 - not installed as a package
        return "unknown"
