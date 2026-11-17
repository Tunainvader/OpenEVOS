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
  Only an observed re-initialisation of both X and Y clears it: IINITONCEDONE leaves 12345 and comes back, which is
  the power-on homing seen while OpenEVOS is connected. With an evidence folder the latch survives app restarts;
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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openevos.backends.technosoft import protocol as tp

#: protected thresholds (plan §6.3): recorded with every snapshot; changing one needs an ADR
IDLE_DRIFT_MAX_COUNTS = 2
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
RECOVERY = ("power-cycle the microscope while OpenEVOS stays running and connected: it watches X and Y re-home, "
            "then clears the lock")
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
    def __init__(self, evidence_dir: Path | str | None = None, *, clock=time.monotonic, record: bool = False):
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
        self._record = None
        self.record_path: Path | None = None
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
                   "fast_samples": self.sample_count, "recording": str(self.record_path or "")}
            if self.latch is not None:
                out.update(rule=self.latch.rule, axis=self.latch.axis, message=self.latch.message,
                           since_utc=self.latch.at_utc, evidence=self.latch.evidence, recovery=RECOVERY)
            return out

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
                if abs(d) > IDLE_DRIFT_MAX_COUNTS:
                    others = sorted(a for a in self.moving if a != ax)
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
                        self.commands.append((round(t, 3), f"{ax} re-initialising (IINITONCEDONE {v.get('IINITONCEDONE')})"))
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

    def end_move(self, axes, why: str = "done") -> None:
        t = self._now()
        with self._lock:
            for ax in axes:
                self.moving.discard(ax)
                self.ref[ax] = None
                self.settle_until[ax] = t + SETTLE_S
            self.commands.append((round(t, 3), f"move {'+'.join(axes)} ended: {why}"))

    def pre_move(self, axis: str, apos: int, cpos: int) -> str:
        """Rule 2 on an axis about to move: refuse (and latch) when its encoder is off its command."""
        d = apos - cpos
        if abs(d) <= MOVE_DRIFT_MAX_COUNTS:
            return ""
        msg = f"{axis} encoder drift {d:+d} counts between position and command (> {MOVE_DRIFT_MAX_COUNTS}) before a move"
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
                record = {"kind": "openevos.stage_monitor_snapshot", "version": 1, "monitor_started_utc": self.started_utc,
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
    def _flag_rule(self, ax: str, isf: int | None, srl: int | None):
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
        self.commands.append((0.0, f"latch carried over from an earlier session: {self.latch.rule} on {self.latch.axis}"))

    def _open_record(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            self.record_path = self.evidence_dir / f"stage-log-{stamp}.csv"
            self._record_fh = open(self.record_path, "w", newline="", encoding="utf-8")  # noqa: SIM115 - kept open
            self._record = _RowWriter(csv.writer(self._record_fh))
            self._record.writerow(CSV_COLUMNS, raw=True)
        except OSError:
            self._record, self.record_path = None, None

    def _trim(self, t: float) -> None:
        while self.samples and self.samples[0][0] < t - RING_S:
            self.samples.popleft()
        while self.polls and self.polls[0][0] < t - RING_S:
            self.polls.popleft()


class _RowWriter:
    def __init__(self, writer):
        self._w = writer

    def writerow(self, row, raw: bool = False) -> None:
        self._w.writerow(row if raw else _csv_row(row))


def _csv_row(row: tuple) -> tuple:
    return tuple("" if v is None else v for v in row)


def _software_version() -> str:
    try:
        from importlib.metadata import version

        return version("openevos")
    except Exception:  # noqa: BLE001 - not installed as a package
        return "unknown"
