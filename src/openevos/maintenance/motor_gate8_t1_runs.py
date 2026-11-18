"""Motor Gate 8 rev 6: T1 correlation runs, X-only with Y switched off (ADR-0045; L4 reviewed).

Spec: docs/evidence/fl1_v1.6_motor_gate8_rev6_t1_runs_spec.md (Y-axis plan v3, T1 Runs C and A).
Y is switched off with Gate 15's verified AXISOFF before any X write (and must read off within 100 ms, else nothing
is written to X); X then runs Gate 8's full-range cycles at a Gate 10 speed preset with a lowered acceleration
(0.5 s ramps), while X APOS, Y APOS and X IQ are logged at ~20 Hz for `y_drift_correlate.py`.

Writes (constants only): Gate 15's W1; Gate 10's X CSPDCMD slow/fast; three X CACCCMD values (Run C 190, Run A
1,573, restore 1.0: the only new frames); Gate 8's X targets, Gate 4's move request and Gate 3's stop. Nothing on
Y but the one AXISOFF; nothing can switch Y on. Speed and acceleration are restored and read back on every exit;
Y stays off: the run always ends with "power-cycle". Dry run by default.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openevos.maintenance import motor_gate2 as g2
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate7_xy_limits as g7
from openevos.maintenance import motor_gate8_full_range as g8
from openevos.maintenance import motor_gate10_speed as g10
from openevos.maintenance import motor_gate15_y_axisoff as g15
from openevos.maintenance.motor_gate1 import (
    ID_QUERY,
    INIT_DONE_MARKER,
    Exchange,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate8_rev6_t1_runs_spec.md; ADR-0045"
SAMPLE_S = 0.0008  # drive sample period (Gate 4/10; speed Verified, acceleration scale Probable)
COUNTS_PER_MM = g7.COUNTS_PER_MM


def cacc_frame(raw: int) -> bytes:
    """X CACCCMD (0x03D8, 32-bit 16.16 fixed, vendor order). Only the three reviewed values."""
    if raw not in (190, 1573, 0x10000):
        raise g4.MotorGate4Refused(f"no X CACCCMD write for raw {raw}")
    body = bytes([0x08, 0x00, 0x10, 0x25, 0xD8]) + g7.vendor32(raw)
    return body + bytes([sum(body) & 0xFF])


@dataclass(frozen=True)
class Run:
    name: str
    preset: str  # Gate 10 speed preset
    cacc_raw: int
    cycles: int
    check_mm: float  # ramp-check move

    @property
    def speed_counts_s(self) -> float:
        return g10.tp.SPEED_PRESETS_MM_S[self.preset] * COUNTS_PER_MM


RUNS = {"C": Run("C", "slow", 190, 4, 2.0), "A": Run("A", "fast", 1573, 20, 10.0)}
CACC_RESTORE = 0x10000  # 1.0, power-up
RAW_1_0, RAW_15_0 = g4.RAW_1_0, g4.RAW_15_0
RAMP_OK_S = (0.3, 1.0)
OFF_WITHIN_S = g15.OFF_WITHIN_S
Y_FAR_COUNTS = 100  # 44 um: ten times the largest drift seen; more = possibly real carriage movement
WRONG_WAY_COUNTS = 100
PAUSE_S, SETTLE_S = 1.5, 0.5
SLOW_EVERY = 10  # X CPOS and Y SRL every 10th sample
SRH_EVERY = 3  # X SRH (completion) every 3rd sample during a leg

XR = g4.XR | g15.R["X"]  # Gate 4's X reads plus Gate 15's status reads (SRL, ISTATUSFLAGS, IQ, peaks, ...)
YR = g4.axis_reads("Y", 2) | g15.R["Y"]
HOME_READS = {ax: {"HOMEDONE": g15._read(ax, "HOMEDONE", "INT", 0x03B0),
                   "LHOMEINDEXDELTA": g15._read(ax, "LHOMEINDEXDELTA", "LONG", 0x03F4)} for ax in ("X", "Y")}
READS_ALL = (*g4.READINESS, *XR.values(), *YR.values(), *(r for d in HOME_READS.values() for r in d.values()))
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in READS_ALL)})
SPEED = {p: g10.SPEED[p] for p in ("slow", "fast")}
CSV_COLUMNS = ("t", "x_apos", "y_apos", "x_cpos", "y_cpos", "x_cmd", "y_cmd", "x_iq")


def allowed_writes(x_far: int, check: int) -> frozenset[bytes]:
    return frozenset({g15.W1_Y_AXISOFF, *SPEED.values(), cacc_frame(190), cacc_frame(1573), cacc_frame(CACC_RESTORE),
                      g8._w(1, x_far), g8._w(1, check), g8._w(1, 0), g4.M3_ESTATE_MOVE, *(f for _n, f in g4.STOP)})


class T1Refused(RuntimeError):
    pass


@dataclass
class T1Result:
    # dry_run | refused | calibration_refused | driver_state | stopped | not_idle | baseline_refused | y_not_off |
    # ramp_check_failed | stopped_y_on | stopped_y_far | drift_refused | fault_stopped | complete | fail
    outcome: str
    message: str
    started_utc: str
    host: str
    run: str = ""
    port: PortInfo | None = None
    port_state_read_back: dict | None = None
    driver_state_problems: list[str] = field(default_factory=list)
    baud: int | None = None
    bytes_written: int = 0
    writes_sent: list[str] = field(default_factory=list)
    gateway_axis_id: int | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    home_data: dict = field(default_factory=dict)
    still: list = field(default_factory=list)
    off_reads: list = field(default_factory=list)
    off_after_ms: float | None = None
    readback: dict = field(default_factory=dict)
    ramp: dict = field(default_factory=dict)
    legs: dict = field(default_factory=dict)
    restore: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    fast_resyncs: list = field(default_factory=list)
    samples: int = 0
    sample_rate_hz: float | None = None
    csv: str = ""
    fault: str = ""
    spec: str = SPEC
    rows: list = field(default_factory=list, repr=False)  # the CSV rows; not written to the JSON

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("rows")
        return json.dumps(d, indent=2, ensure_ascii=False)


def confirm_phrase(run: str, device: str) -> str:
    return f"RUN T1 {run} X ONLY Y OFF ON {device.upper()}"


class _YOn(g4._Fault):
    pass


class _YFar(g4._Fault):
    pass


class _RampFailed(g4._Fault):
    pass


# ---------------------------------------------------------------------------------- logging
class _Log:
    """X APOS, Y APOS, X IQ back to back (~48 ms on COM3 -> ~20 Hz); X CPOS and Y SRL every SLOW_EVERY samples."""

    def __init__(self, link, res: T1Result, t0: float, y0: int, y_cpos: int, x_cpos: int):
        self.link, self.res, self.t0, self.y0, self.y_cpos, self.x_cpos = link, res, t0, y0, y_cpos, x_cpos
        self.n = 0

    def sample(self, x_cmd: int) -> list:
        lk = self.link
        x, y, iq = lk.fast(XR["APOS"]), lk.fast(YR["APOS"]), lk.fast(XR["IQ"])
        self.n += 1
        if self.n % SLOW_EVERY == 0:
            self.x_cpos = lk.fast(XR["CPOS"])
            srl = lk.fast(YR["SRL"])
            if srl & g15.SRL_AXIS_ON:
                raise _YOn(f"Y power stage came back on (SRL 0x{srl:04X})")
        row = [round(lk.monotonic() - self.t0, 3), x, y, self.x_cpos, self.y_cpos, x_cmd, 0, iq]
        self.res.rows.append(row)
        if abs(y - self.y0) > Y_FAR_COUNTS:
            raise _YFar(f"Y encoder {y} is {y - self.y0:+d} counts from {self.y0} (> {Y_FAR_COUNTS})")
        return row

    def idle(self, seconds: float) -> None:
        end = self.link.monotonic() + seconds
        while self.link.monotonic() < end:
            self.sample(0)


def _leg(link, res: T1Result, log: _Log, leg: str, frame: bytes, target: int, band: int, timeout_s: float) -> None:
    apos0, cpos0 = link.fast(XR["APOS"]), link.fast(XR["CPOS"])
    if abs(apos0 - cpos0) > g4.DRIFT_MAX_COUNTS:
        raise g4.DriftRefused(f"X encoder {apos0} vs command {cpos0}: {apos0 - cpos0:+d} counts; X not moved")
    y_start = log.res.rows[-1][2] if log.res.rows else log.y0
    first = len(res.rows)
    link.write(f"X CPOSCMD {leg}", frame)
    link.write("X ESTATE=8", g4.M3_ESTATE_MOVE)
    deadline = link.monotonic() + timeout_s
    k = 0
    while True:
        row = log.sample(1)
        k += 1
        if abs(target - row[1]) > abs(target - apos0) + WRONG_WAY_COUNTS:
            raise g4._Fault(f"{leg}: X moved away from its target ({apos0} -> {row[1]}, target {target})")
        if (k % SRH_EVERY == 0 and link.fast(XR["SRH"]) & g4.SRH_NOT_MOVING_BIT
                and link.fast(XR["ESTATEV"]) == g4.ESTATEV_AFTER_MOVE and link.fast(XR["CPOS"]) == target):
            break
        if link.monotonic() > deadline:
            raise g4._Fault(f"{leg} did not complete within {timeout_s:.0f} s")
    apos, cpos = link.fast(XR["APOS"]), link.fast(XR["CPOS"])
    log.x_cpos = cpos
    hi, lo = link.fast(XR["IIQMAX"]), link.fast(XR["IIQMIN"])
    y_end = link.fast(YR["APOS"])  # fresh: the last logged sample can predate the move's completion
    res.legs[leg] = {"target": target, "APOS": apos, "CPOS": cpos, "error": apos - target, "IIQMAX": hi, "IIQMIN": lo,
                     "y_start": y_start, "y_end": y_end, "y_total_from_baseline": y_end - log.y0,
                     "rows": [first, len(res.rows)]}
    if abs(apos - target) > band or cpos != target:
        raise g4._Fault(f"{leg} ended at APOS {apos}, CPOS {cpos}; target {target}, band {band}")


def measure_ramp(rows: list, speed_counts_s: float) -> dict:
    """Time from 10 % to 90 % of the expected speed on the first acceleration of `rows` (one move out)."""
    t10 = t90 = None
    vmax = 0.0
    for (ta, xa, *_), (tb, xb, *_) in itertools.pairwise(rows):
        if tb <= ta:
            continue
        v = abs(xb - xa) / (tb - ta)
        tm = (ta + tb) / 2
        vmax = max(vmax, v)
        if t10 is None and v >= 0.1 * speed_counts_s:
            t10 = tm
        if t10 is not None and t90 is None and v >= 0.9 * speed_counts_s:
            t90 = tm
            break
    ramp = None if t10 is None or t90 is None else round(t90 - t10, 3)
    return {"ramp_s": ramp, "speed_expected": round(speed_counts_s), "speed_max_seen": round(vmax),
            "ok": ramp is not None and RAMP_OK_S[0] <= ramp <= RAMP_OK_S[1]}


# ---------------------------------------------------------------------------------- run
def run(*, run_name: str = "", send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        power_cycled_gate7_no_move: bool = False, x_travel_clear: bool = False, watching_main_switch_second: bool = False,  # noqa: E501
        calibration: Path | None = None, scans: Path = Path("openevos-scans"),
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> T1Result:
    res = T1Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node(),
                   run=run_name)
    r = RUNS.get(run_name)
    if r is None:
        res.message = "choose --run C or --run A; nothing opened"
        return res
    src = calibration or g7.latest_parsed(scans)
    if src is None:
        res.message = "no parsed calibration found; nothing opened"
        return res
    lim, problems = g7.load_limits(src)
    if problems:
        res.outcome, res.message = "calibration_refused", "; ".join(problems)
        return res
    x_far = g8.plan_targets(lim)["X"]
    check = round(r.check_mm * COUNTS_PER_MM)
    leg_s = x_far / r.speed_counts_s
    res.plan = {"run": r.name, "speed_preset": r.preset, "speed_mm_s": g10.tp.SPEED_PRESETS_MM_S[r.preset],
                "cacc_raw": r.cacc_raw, "cycles": r.cycles, "x_far_counts": x_far,
                "x_far_mm": round(x_far / COUNTS_PER_MM, 3), "ramp_check_mm": r.check_mm,
                "expected_minutes": round(r.cycles * 2 * (leg_s + 0.5 + PAUSE_S) / 60, 1)}
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run, Run {r.name}: Y AXISOFF (off within 100 ms or stop), X {r.preset} speed and CACCCMD "
                       f"{r.cacc_raw}, a {r.check_mm:g} mm ramp check, then {r.cycles} x (0 -> {res.plan['x_far_mm']} mm "  # noqa: E501
                       f"-> 0), ~{res.plan['expected_minutes']} min, logging at ~20 Hz; nothing opened or sent")
        res.exchanges = [Exchange(n, f.hex(" ")) for n, f in (
            ("W1 AXISOFF Y", g15.W1_Y_AXISOFF), (f"X CSPDCMD {r.preset}", SPEED[r.preset]),
            (f"X CACCCMD {r.cacc_raw}", cacc_frame(r.cacc_raw)), ("X CACCCMD restore 1.0", cacc_frame(CACC_RESTORE)))]
        return res
    missing = [why for ok, why in (
        (on_for_2_min, "the instrument has been on for >= 2 minutes"),
        (power_cycled_gate7_no_move, "a fresh power cycle, then Gate 7, and no move since"),
        (x_travel_clear, "the whole X travel is clear, Z retracted, nothing in Y's path"),
        (watching_main_switch_second, "watching, main switch within reach, a second person present"),
    ) if not ok]
    if missing:
        res.message = "operator has not confirmed: " + "; ".join(missing) + "; nothing opened"
        return res
    if confirm != confirm_phrase(r.name, dev):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(r.name, dev)!r}); nothing opened"
        return res
    try:
        port = open_port(dev)
    except Exception as exc:  # noqa: BLE001
        res.message = f"could not open {dev}: {exc} (close the OpenEVOS app first); nothing sent"
        return res
    try:
        res.port_state_read_back = port.inspect()
        res.driver_state_problems = driver_state_problems(res.port_state_read_back)
        if res.driver_state_problems:
            res.outcome, res.message = "driver_state", "driver state not normal; nothing sent"
            return res
        link = g15._Link(port, res, monotonic, sleep, allowed_writes=allowed_writes(x_far, check),
                         allowed_reads=ALLOWED_READS)
        try:
            _procedure(link, res, r, lim, x_far, check)
        except g4._Fault as f:  # a link fault outside the guarded phases
            res.fault = str(f)
            if res.writes_sent:
                _restore(link, res)
                res.outcome = "fail"
                res.message = f"link fault: {f}. {_END}"
            else:
                res.outcome, res.message = "stopped", f"link fault: {f}; nothing written"
    finally:
        port.close()
    if res.rows:
        span = res.rows[-1][0] - res.rows[0][0]
        res.samples = len(res.rows)
        res.sample_rate_hz = round((len(res.rows) - 1) / span, 2) if span > 0 else None
    return res


_END = "Y is left switched off: power-cycle the microscope before anything else (it also re-phases Y)"


def _procedure(link, res: T1Result, r: Run, lim, x_far: int, check: int) -> None:
    # Phase A: readiness and baseline (read-only)
    if not g15._connect(link, res):
        return
    for rd in g4.READINESS:
        v = link.value(rd)
        if (rd.var == "ESTATEV" and v != 0) or (rd.var == "IINITONCEDONE" and v != INIT_DONE_MARKER):
            res.outcome, res.message = "not_idle", f"{rd.axis} {rd.var} = {v}: not idle/homed; nothing written"
            return
    xb = {n: link.read(XR[n]) for n in g4.BASELINE}
    status = {ax: {v: link.value(R[v]) for v in g15.BASELINE_VARS} for ax, R in (("X", g15.R["X"]), ("Y", g15.R["Y"]))}
    y_lim = (link.value(YR["LPOSMIN"]), link.value(YR["LPOSMAX"]))
    res.home_data = {ax: {n: link.value(rd) for n, rd in d.items()} for ax, d in HOME_READS.items()}
    res.baseline = {"X": {k: v.get("i32", v.get("u16")) for k, v in xb.items()}, "status": status,
                    "Y_limits": list(y_lim)}
    problems = g4.baseline_problems(xb, target=x_far, axis="X") + g15.baseline_problems(status)
    if (xb["LPOSMIN"]["i32"], xb["LPOSMAX"]["i32"]) != lim["X"] or y_lim != lim["Y"]:
        problems.append("the drives do not hold the calibrated limits (run Gate 7 first)")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    t_still, first = link.monotonic(), None
    while link.monotonic() - t_still < g15.STILL_S:
        v = link.fast(YR["APOS"])
        res.still.append([round((link.monotonic() - t_still) * 1000, 1), v])
        first = v if first is None else first
        if abs(v - first) > g15.STILL_COUNTS:
            res.outcome, res.message = "baseline_refused", f"Y is not still ({first} -> {v}); nothing written"
            return
    y0, y_cpos, band = status["Y"]["APOS"], status["Y"]["CPOS"], xb["LCONVERGEBAND"]["i32"]
    # Phase B1: Y off; X is written only if Y reads off within 100 ms
    try:
        link.write("W1 AXISOFF Y", g15.W1_Y_AXISOFF)
    except g4._Fault as f:
        res.fault = str(f)
        res.outcome = "y_not_off" if res.writes_sent else "stopped"
        res.message = f"{f}; nothing written to X. " + (_END if res.writes_sent else "Nothing written")
        return
    t0 = link.monotonic()
    link.synced = True
    while True:
        srl = link.fast(YR["SRL"])
        t = round((link.monotonic() - t0) * 1000, 1)
        res.off_reads.append([t, srl])
        if not srl & g15.SRL_AXIS_ON:
            res.off_after_ms = t
            break
        if t > OFF_WITHIN_S * 1000:
            break
    if res.off_after_ms is None or res.off_after_ms > OFF_WITHIN_S * 1000:
        res.outcome = "y_not_off"
        res.message = f"Y still on {res.off_reads[-1][0]:.0f} ms after the AXISOFF; nothing written to X. {_END}"
        return
    # Phase B2: speed and acceleration (restored on every exit from here)
    log = _Log(link, res, t0, y0, y_cpos, xb["CPOS"]["i32"])
    try:
        if xb["CSPDCMD"]["raw"] != _raw(SPEED[r.preset]):
            link.write(f"X CSPDCMD {r.preset}", SPEED[r.preset])
        link.write(f"X CACCCMD {r.cacc_raw}", cacc_frame(r.cacc_raw))
        res.readback = {"CSPDCMD": link.read(XR["CSPDCMD"])["raw"], "CACCCMD": link.read(XR["CACCCMD"])["raw"]}
        if res.readback != {"CSPDCMD": _raw(SPEED[r.preset]), "CACCCMD": _raw(cacc_frame(r.cacc_raw))}:
            raise g4._Fault(f"speed/acceleration read back {res.readback}, not the values written")
        def timeout(counts: int) -> float:
            return (counts / r.speed_counts_s + 2 * RAMP_OK_S[1]) * 1.2 + 6.0

        # Phase B3: ramp check
        _leg(link, res, log, "ramp check out", g8._w(1, check), check, band, timeout(check))
        out = res.legs["ramp check out"]["rows"]
        res.ramp = measure_ramp(res.rows[max(0, out[0] - 1):out[1]], r.speed_counts_s)
        log.idle(SETTLE_S)
        _leg(link, res, log, "ramp check home", g8._w(1, 0), 0, band, timeout(check))
        for name, frame in g4.STOP:
            link.write(f"X {name}", frame)
        if not res.ramp["ok"]:
            raise _RampFailed(f"measured ramp {res.ramp['ramp_s']} s, not {RAMP_OK_S[0]}-{RAMP_OK_S[1]} s "
                              f"(max speed seen {res.ramp['speed_max_seen']} of {res.ramp['speed_expected']} counts/s)")
        # Phase C: the cycles
        for c in range(1, r.cycles + 1):
            _leg(link, res, log, f"c{c} out", g8._w(1, x_far), x_far, band, timeout(x_far))
            log.idle(PAUSE_S)
            _leg(link, res, log, f"c{c} home", g8._w(1, 0), 0, band, timeout(x_far))
            for name, frame in g4.STOP:
                link.write(f"X {name}", frame)
            log.idle(SETTLE_S)
    except g4.DriftRefused as f:
        res.outcome, res.fault = "drift_refused", str(f)
        _restore(link, res)
        res.message = f"{f}. {_END}"
        return
    except _RampFailed as f:
        res.outcome, res.fault = "ramp_check_failed", str(f)
        _restore(link, res)
        res.message = f"{f}; X restored, no cycle run. {_END}"
        return
    except (_YOn, _YFar, g4._Fault) as f:
        kind = "stopped_y_on" if isinstance(f, _YOn) else "stopped_y_far" if isinstance(f, _YFar) else "fault_stopped"
        g4._fault_stop(link, res, str(f))
        _restore(link, res)
        res.outcome = kind
        res.message = f"{res.message} {_END}"
        return
    # Phase D: restore and final reads
    _restore(link, res)
    res.final = {"X": {n: link.value(XR[n]) for n in ("ESTATEV", "APOS", "CPOS", "ISTATUSFLAGS", "MER", "IIQMAX",
                                                     "IIQMIN")},
                 "Y": {n: link.value(YR[n]) for n in ("SRL", "APOS", "CPOS", "ISTATUSFLAGS", "MER", "ESTATEV")},
                 "ESTATEV_all": {rd.axis: link.value(rd) for rd in g2.READS if rd.var == "ESTATEV"}}
    f = res.final
    res.checks = {
        "x_home_within_band": abs(f["X"]["APOS"]) <= band and abs(f["X"]["CPOS"]) <= band,  # stop: CPOS = APOS
        "speed_restored": res.restore.get("CSPDCMD") == RAW_15_0,
        "accel_restored": res.restore.get("CACCCMD") == RAW_1_0,
        "y_still_off": not f["Y"]["SRL"] & g15.SRL_AXIS_ON,
        "all_axes_idle": all(v == 0 for v in f["ESTATEV_all"].values()),
    }
    ok = all(res.checks.values())
    res.outcome = "complete" if ok else "fail"
    ends = [d["y_total_from_baseline"] for k, d in res.legs.items() if k.startswith("c")]
    res.message = ((f"Run {r.name} complete: {r.cycles} cycles, ramp {res.ramp['ramp_s']} s; Y encoder at the leg "
                    f"ends (counts from {y0}): {ends}" if ok else "FAILED checks: "
                    + ", ".join(k for k, v in res.checks.items() if not v)) + f". {_END}")


def _raw(frame: bytes) -> str:
    """The value bytes of a 32-bit write, as a read returns them ('raw' in vendor order)."""
    return frame[5:9].hex(" ")


def _restore(link, res: T1Result) -> None:
    """X speed and acceleration back to power-up, each read back. Never raises; records what happened."""
    for var, frame in (("CSPDCMD", SPEED["fast"]), ("CACCCMD", cacc_frame(CACC_RESTORE))):
        try:
            if link.read(XR[var])["raw"] != _raw(frame):
                link.write(f"X {var} restore", frame)
            res.restore[var] = link.read(XR[var])["raw"]
        except g4._Fault as f:
            res.restore[var] = f"NOT RESTORED: {f} (a power cycle restores it)"


# ---------------------------------------------------------------------------------- CLI
def write_evidence(result: T1Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    base = out_dir / f"motor-gate8-{stamp}-t1{result.run.lower()}"
    if result.rows:
        result.csv = str(base.with_suffix(".csv"))
        with open(result.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLUMNS)
            w.writerows(result.rows)
    path = Path(f"{base}-{result.outcome}.json")
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate8_t1_runs",
                                description="Motor Gate 8 rev 6: T1 Run C or A, X-only with Y switched off. "
                                            "Dry run by default.")
    p.add_argument("--run", choices=tuple(RUNS), required=True)
    p.add_argument("--send", action="store_true", help="run it (phrase: RUN T1 <C|A> X ONLY Y OFF ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true")
    p.add_argument("--power-cycled-gate7-no-move", action="store_true",
                   help="fresh power cycle (Y re-phased), then Gate 7, and nothing moved since")
    p.add_argument("--x-travel-clear", action="store_true", help="the whole X travel clear, Z retracted")
    p.add_argument("--watching-main-switch-second-person", action="store_true")
    p.add_argument("--confirm", default="")
    p.add_argument("--calibration", type=Path, default=None)
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 8 rev 6 needs Windows.", file=sys.stderr)
        return 2
    res = run(run_name=a.run, send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              power_cycled_gate7_no_move=a.power_cycled_gate7_no_move, x_travel_clear=a.x_travel_clear,
              watching_main_switch_second=a.watching_main_switch_second_person, calibration=a.calibration,
              scans=a.evidence_dir, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  {ex.what:24s} {ex.request_hex}")
    if res.port_state_read_back or res.outcome == "dry_run":
        path = write_evidence(res, a.evidence_dir)
        print(f"evidence (git-ignored): {path}")
        if res.csv:
            print(f"log: {res.csv} ({res.samples} samples, {res.sample_rate_hz} Hz)")
            print(f"analyse: python y_drift_correlate.py \"{res.csv}\" --x-iq x_iq --y-cpos y_cpos --plot "
                  f"\"{Path(res.csv).with_suffix('.png')}\"")
    return 0 if res.outcome in ("dry_run", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
