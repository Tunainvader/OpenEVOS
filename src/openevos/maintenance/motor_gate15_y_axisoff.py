"""Motor Gate 15: host AXISOFF on the idle Y axis (ADR-0044; L4 reviewed).

Spec: docs/evidence/fl1_v1.6_motor_gate15_y_axisoff_spec.md (Y-axis plan v3, §4 step 3). Transport: Motor Gate 1 rev 2.
The ONLY write this module can send is W1 = AXISOFF (TML 0x0002) to axis 2 (Y), the vendor's HaltAllMotors frame
`04 00 20 00 02 26`, at most once per run. No AXISON, no other axis, no variable or state write: Y comes back on
only through a power cycle.

Phases: 0 refuse before opening; A port + readiness; B baseline (Y on and settled with its settle check over and
still for 1 s; X on and idle); C W1 + one 'O'; D Y SRL bit 15 off within 100 ms, a 10 s hold logging Y APOS/SRL,
final reads. E (`--check-recovered`, read-only): after the power cycle, X and Y on and homed again.
The timed reads (D, and B's stillness) use the app's read timing (exact reply length, no fixed wait; ADR-0040),
~16 ms per read on COM3 instead of the ~65 ms of Gate 4's reads. Dry run by default.
"""

from __future__ import annotations

import argparse
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
from openevos.maintenance import motor_gate9_drive_status as g9
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
    OP_READ16,
    OP_READ32,
    SYNC_BYTE,
    SYNC_REPLY,
    Exchange,
    Port,
    PortInfo,
    build_read,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
    parse_read_reply,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate15_y_axisoff_spec.md; ADR-0044"
W1_NAME = "W1 AXISOFF Y"
W1_Y_AXISOFF = bytes.fromhex("04 00 20 00 02 26")  # length 4, axis 2, TML 0x0002; checksum = byte sum
ALLOWED_WRITES = frozenset({W1_Y_AXISOFF})

SRL_AXIS_ON, SRL_MOTION_COMPLETE = 1 << 15, 1 << 10
ISF_OVERCURRENT, ISF_COMM_ABORT, ISF_OSCILLATION, ISF_WINDOW_OVER = 0x0001, 0x0010, 0x2000, 0x8000
OFF_WITHIN_S = 0.100  # plan v3 §6.3: the escalation limit
SLOW_DROP_S = 1.0  # keep reading this much longer to measure a late drop (then FAIL; no second AXISOFF)
HOLD_S, STILL_S = 10.0, 1.0
STILL_COUNTS = 1  # Phase B: Y within +-1 count for 1 s
APOS_CPOS_MAX = 3  # ADR-0030 / ADR-0040
X_APOS_TOL = 1
HOLD_FINDING_COUNTS = 2  # Y moving more than this while unpowered is reported (not a pass condition)
SRL_OFF_EXPECTED = 0x4400  # what Gate 9 read after the drive's own AXISOFF (Y trips) and on F at idle
AXIS_IDS = {"X": 1, "Y": 2}


def _read(axis: str, var: str, vtype: str, address: int) -> g2.Read:
    aid = AXIS_IDS[axis]
    return g2.Read(axis, aid, var, vtype, address, build_read(aid, OP_READ16 if vtype == "INT" else OP_READ32, address))


def axis_reads(axis: str) -> dict[str, g2.Read]:
    """Gate 2's state/position reads, Gate 9's status and program reads, and LCOMMERRPOS (0x03BC, 32-bit; the X and
    Y variables.cfg, never read on the unit before this gate)."""
    out = {r.var: r for r in g2.READS if r.axis == axis and r.var in ("ESTATEV", "IINITONCEDONE", "APOS", "CPOS")}
    out |= {v: g9.STATUS[axis][v] for v in ("SRL", "SRH", "MER", "ISTATUSFLAGS", "BDOAXISOFF", "IQ", "IIQMAX", "IIQMIN")}
    out["LCOMMERRPOS"] = _read(axis, "LCOMMERRPOS", "LONG", 0x03BC)
    return out


R = {ax: axis_reads(ax) for ax in AXIS_IDS}
READINESS = tuple(r for r in g2.READS if r.var in g2.READY)
ESTATEV_ALL = tuple(r for r in g2.READS if r.var == "ESTATEV")
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in READINESS), *(r.frame for rs in R.values() for r in rs.values())})
BASELINE_VARS = ("ESTATEV", "SRL", "SRH", "MER", "ISTATUSFLAGS", "BDOAXISOFF", "APOS", "CPOS", "IQ", "IIQMAX", "IIQMIN",
                 "LCOMMERRPOS")
HOLD_Y_EACH_S = ("IQ", "ISTATUSFLAGS", "ESTATEV", "CPOS", "MER")
HOLD_X_EACH_S = ("APOS", "SRL", "ESTATEV")


class MotorGate15Refused(RuntimeError):
    pass


def assert_allowed(request: bytes) -> None:
    if request == bytes([SYNC_BYTE]) or request in ALLOWED_READS or request in ALLOWED_WRITES:
        return
    raise MotorGate15Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 15 frames")


@dataclass
class MotorGate15Result:
    # dry_run | refused | driver_state | stopped | not_idle | baseline_refused | write_failed | fail | pass
    # Phase E: recovered | not_recovered
    outcome: str
    message: str
    started_utc: str
    host: str
    port: PortInfo | None = None
    port_state_read_back: dict | None = None
    driver_state_problems: list[str] = field(default_factory=list)
    baud: int | None = None
    bytes_written: int = 0
    writes_sent: list[str] = field(default_factory=list)
    gateway_axis_id: int | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    still: list = field(default_factory=list)  # [ms, Y APOS] over Phase B's 1 s
    ack_ms: float | None = None
    off_reads: list = field(default_factory=list)  # [ms after the ack, Y SRL] until off
    off_after_ms: float | None = None
    hold: list = field(default_factory=list)  # [ms after the ack, Y APOS, Y SRL]
    hold_each_s: list = field(default_factory=list)  # [ms after the ack, {"Y IQ": .., "X APOS": .., ...}]
    hold_stopped: str = ""
    final: dict = field(default_factory=dict)
    fast_resyncs: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    findings: dict = field(default_factory=dict)
    fault: str = ""
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def confirm_phrase(device: str) -> str:
    return f"SWITCH OFF IDLE Y AXIS ON {device.upper()}"


def _value(r: g2.Read, d: dict) -> int:
    """Plain number: 32-bit signed, IQ/IIQMAX/IIQMIN signed 16-bit, everything else unsigned 16-bit (flags)."""
    if "i32" in d:
        return d["i32"]
    return d["i16"] if r.var in ("IQ", "IIQMAX", "IIQMIN") else d["u16"]


# ---------------------------------------------------------------------------------- link
class _Link(g4._Link):
    """Gate 4's link (sync before each read and write, 'O' acknowledgement) restricted to Gate 15's frames, plus
    `fast()`: the app's read timing for the timed phases (ADR-0040 DriveLink, measured 16 ms per read on COM3, 0
    failures in 100): the exact reply length with no fixed wait; the line is synced once and again only after a
    defective reply, with one retry."""

    def __init__(self, port: Port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=ALLOWED_READS):
        """Other gates (Gate 8 rev 6) pass their own reviewed frame sets; nothing outside them can be sent."""
        super().__init__(port, res, monotonic, sleep, allowed_writes=allowed_writes, allowed_reads=allowed_reads)
        self.synced = False

    def value(self, r: g2.Read) -> int:
        return _value(r, self.read(r))

    def fast(self, r: g2.Read) -> int:
        if r.frame not in self.allowed_reads:
            raise MotorGate15Refused(f"read {r.frame.hex(' ')} is not one of this gate's reads")
        why = ""
        for attempt in (0, 1):
            if not self.synced or attempt:
                if not self.sync(f"line sync before fast {r.axis} {r.var}"):
                    raise g4._Fault(f"line sync lost before {r.axis} {r.var}")
                self.synced = True
            self.res.bytes_written += self.port.write(r.frame)
            raw = self.port.read(13 if r.bits == 16 else 15)
            for _ in range(4):  # a late sync reply (0D) can precede the real one; a real reply starts with 'O'
                if not raw or raw[0] != SYNC_REPLY:
                    break
                raw = raw[1:] + self.port.read(1)
            value, why = parse_read_reply(raw, r.axis_id, r.address, r.bits) if raw else (None, "no reply")
            if value is not None:
                return _value(r, g2.decode_value(value, r.vtype))
            self.synced = False
            self.res.fast_resyncs.append(f"{r.axis} {r.var}: {why}")
        raise g4._Fault(f"{r.axis} {r.var}: {why}")


# ---------------------------------------------------------------------------------- run
def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, power_cycled_no_move: bool = False,
        stage_clear_watching_y: bool = False, main_switch_second_person: bool = False, check_recovered: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> MotorGate15Result:
    res = MotorGate15Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                            platform.node())
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if check_recovered:  # Phase E: read-only, no phrase
        if not on_for_2_min:
            res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
            return res
        return _open_and(res, dev, open_port, monotonic, sleep, _phase_e)
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would open {dev}, check readiness, the X and Y baseline and that Y is still for "
                       f"1 s, then send W1 (AXISOFF to Y) once, expect 'O', confirm Y SRL bit 15 = 0 within 100 ms, "
                       f"log a 10 s hold and read back; nothing opened or sent")
        res.exchanges = [Exchange(W1_NAME, W1_Y_AXISOFF.hex(" "))]
        return res
    missing = [why for ok, why in (
        (on_for_2_min, "the instrument has been on for >= 2 minutes"),
        (power_cycled_no_move, "the microscope was power-cycled for this gate, with no move since"),
        (stage_clear_watching_y, "the stage is clear, Z retracted, nothing in Y's path, and they are watching Y"),
        (main_switch_second_person, "the main switch is within reach and a second person is present"),
    ) if not ok]
    if missing:
        res.message = "operator has not confirmed: " + "; ".join(missing) + "; nothing opened"
        return res
    if confirm != confirm_phrase(dev):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev)!r}); nothing opened"
        return res
    return _open_and(res, dev, open_port, monotonic, sleep, _procedure)


def _open_and(res, dev, open_port, monotonic, sleep, body) -> MotorGate15Result:
    try:
        port = open_port(dev)
    except Exception as exc:  # noqa: BLE001
        res.message = (f"could not open {dev}: {exc} (close the OpenEVOS app first: only one program can hold "
                       "the port); nothing sent")
        return res
    try:
        res.port_state_read_back = port.inspect()
        res.driver_state_problems = driver_state_problems(res.port_state_read_back)
        if res.driver_state_problems:
            res.outcome = "driver_state"
            res.message = "driver state is not normal (" + "; ".join(res.driver_state_problems) + "). Nothing sent"
            return res
        link = _Link(port, res, monotonic, sleep)
        try:
            body(link, res)
        except g4._Fault as f:
            res.fault = str(f)
            if res.writes_sent:
                res.outcome = "fail"
                res.message = f"link fault after W1: {f}. Nothing further sent. {_AFTER_W1}"
            else:
                res.outcome, res.message = "stopped", f"link fault: {f}; nothing written"
    finally:
        port.close()
    return res


_AFTER_W1 = ("Y is left switched off: power-cycle the microscope before any Y move or opening OpenEVOS, then run "
             "--check-recovered")


def _connect(link: _Link, res: MotorGate15Result) -> bool:
    for baud in BAUD_PASSES:
        link.port.configure(baud)
        if link.sync(f"line sync @ {baud}"):
            res.baud = baud
            break
    if res.baud is None:
        res.outcome, res.message = "stopped", "no line sync at either rate; nothing written"
        return False
    res.gateway_axis_id = link.query_id()
    if res.gateway_axis_id != 1:
        res.outcome, res.message = "stopped", f"gateway axis ID {res.gateway_axis_id}, not 1; nothing written"
        return False
    return True


def _procedure(link: _Link, res: MotorGate15Result) -> None:
    # Phase A: port and readiness
    if not _connect(link, res):
        return
    for r in READINESS:
        v = link.value(r)
        res.readiness[f"{r.axis} {r.var}"] = v
        if (r.var == "ESTATEV" and v != 0) or (r.var == "IINITONCEDONE" and v != INIT_DONE_MARKER):
            res.outcome = "not_idle"
            res.message = f"{r.axis} {r.var} = {v}: not idle/homed; nothing written"
            return
    # Phase B: baseline
    res.baseline = {ax: {v: link.value(R[ax][v]) for v in BASELINE_VARS} for ax in ("X", "Y")}
    problems = baseline_problems(res.baseline)
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    t_start = link.monotonic()
    first = None
    while link.monotonic() - t_start < STILL_S:
        v = link.fast(R["Y"]["APOS"])
        res.still.append([round((link.monotonic() - t_start) * 1000, 1), v])
        first = v if first is None else first
        if abs(v - first) > STILL_COUNTS:
            res.outcome = "baseline_refused"
            res.message = f"Y is not still: APOS {first} -> {v} within {STILL_S:.0f} s; nothing written"
            return
    # Phase C: the one write
    try:
        link.write(W1_NAME, W1_Y_AXISOFF)
    except g4._Fault as f:
        res.fault = str(f)
        if not res.writes_sent:  # the line sync before W1 failed: nothing reached the drive
            res.outcome, res.message = "stopped", f"{f}; nothing written"
            return
        why = f"{f}; no further write, not retried"
        link.synced = False
        try:
            _phase_d(link, res, link.monotonic())  # read-only: did it take effect anyway?
        except g4._Fault as g:
            why += f"; the reads afterwards stopped: {g}"
        res.checks, res.findings = evaluate(res)
        res.outcome = "write_failed"
        res.message = f"{why}. {_AFTER_W1}"
        return
    t0 = link.monotonic()
    res.ack_ms = res.exchanges[-1].ms
    link.synced = True  # an acknowledged frame leaves the line in step: the first off-check read needs no sync
    # Phase D
    _phase_d(link, res, t0)
    res.checks, res.findings = evaluate(res)
    ok = bool(res.checks) and all(res.checks.values())
    res.outcome = "pass" if ok else "fail"
    failed = [k for k, v in res.checks.items() if not v]
    head = (f"host AXISOFF switched Y off {res.off_after_ms:.0f} ms after the acknowledgement; Y state and X unchanged"
            if ok else "FAILED checks: " + ", ".join(failed))
    res.message = f"{head}. {res.findings.get('y_unpowered', '')} {_AFTER_W1}".replace("  ", " ")


def baseline_problems(b: dict) -> list[str]:
    y, x = b["Y"], b["X"]
    p = []
    if not y["SRL"] & SRL_AXIS_ON:
        p.append(f"Y power stage already off (SRL 0x{y['SRL']:04X})")
    if not y["SRL"] & SRL_MOTION_COMPLETE:
        p.append(f"Y motion not complete (SRL 0x{y['SRL']:04X})")
    if y["ISTATUSFLAGS"] & (ISF_OVERCURRENT | ISF_COMM_ABORT | ISF_OSCILLATION):
        p.append(f"Y drive flags set (ISTATUSFLAGS 0x{y['ISTATUSFLAGS']:04X})")
    if not y["ISTATUSFLAGS"] & ISF_WINDOW_OVER:
        p.append(f"Y settle window not over (ISTATUSFLAGS 0x{y['ISTATUSFLAGS']:04X})")
    if y["BDOAXISOFF"] != 0:
        p.append(f"Y settle check armed (BDOAXISOFF {y['BDOAXISOFF']})")
    if abs(y["APOS"] - y["CPOS"]) > APOS_CPOS_MAX:
        p.append(f"Y APOS {y['APOS']} is {y['APOS'] - y['CPOS']:+d} counts from CPOS {y['CPOS']}")
    if not x["SRL"] & SRL_AXIS_ON:
        p.append(f"X power stage off (SRL 0x{x['SRL']:04X})")
    if x["ESTATEV"] != 0 or y["ESTATEV"] != 0:
        p.append(f"not idle (X ESTATEV {x['ESTATEV']}, Y ESTATEV {y['ESTATEV']})")
    return p


def _ms(link: _Link, t0: float) -> float:
    return round((link.monotonic() - t0) * 1000, 1)


def _phase_d(link: _Link, res: MotorGate15Result, t0: float) -> None:
    """Read-only from here on: off check, hold, final reads. Never sends a write."""
    y, x = R["Y"], R["X"]
    while True:  # 1. off within 100 ms (keep reading up to 1 s more to measure a late drop)
        srl = link.fast(y["SRL"])
        t = _ms(link, t0)
        res.off_reads.append([t, srl])
        if not srl & SRL_AXIS_ON:
            res.off_after_ms = t
            break
        if t > (OFF_WITHIN_S + SLOW_DROP_S) * 1000:
            break
    hold_start, next_each = link.monotonic(), link.monotonic()
    base_x = res.baseline.get("X", {}).get("APOS")
    while link.monotonic() - hold_start < HOLD_S:  # 2. hold
        apos, srl = link.fast(y["APOS"]), link.fast(y["SRL"])
        res.hold.append([_ms(link, t0), apos, srl])
        if link.monotonic() >= next_each:
            each = {f"Y {v}": link.fast(y[v]) for v in HOLD_Y_EACH_S} | {f"X {v}": link.fast(x[v]) for v in HOLD_X_EACH_S}
            res.hold_each_s.append([_ms(link, t0), each])
            next_each += 1.0
            changed = _x_changed(each, base_x)
            if changed:  # spec §5: another axis changed state: stop reading and report; send nothing
                res.hold_stopped = changed
                return
    res.final = {ax: {v: link.value(R[ax][v]) for v in BASELINE_VARS} for ax in ("X", "Y")}  # 3. final reads
    res.final["ESTATEV_all"] = {r.axis: link.value(r) for r in ESTATEV_ALL}


def _x_changed(each: dict, base_x: int | None) -> str:
    if not each["X SRL"] & SRL_AXIS_ON:
        return f"X power stage dropped (SRL 0x{each['X SRL']:04X})"
    if each["X ESTATEV"] != 0:
        return f"X state changed (ESTATEV {each['X ESTATEV']})"
    if base_x is not None and abs(each["X APOS"] - base_x) > X_APOS_TOL:
        return f"X moved ({base_x} -> {each['X APOS']})"
    return ""


def evaluate(res: MotorGate15Result) -> tuple[dict[str, bool], dict]:
    b, f = res.baseline, res.final
    findings: dict = {}
    if res.hold:
        dev = max(abs(a - b["Y"]["APOS"]) for _t, a, _s in res.hold)
        findings["y_max_counts_from_baseline_while_off"] = dev
        findings["y_unpowered"] = (f"Y held position unpowered (at most {dev} counts)." if dev <= HOLD_FINDING_COUNTS
                                   else f"FINDING: Y moved {dev} counts while unpowered: review before T1 Run C.")
    if res.off_reads and res.off_after_ms is not None:
        srl = res.off_reads[-1][1]
        findings["y_srl_off"] = f"0x{srl:04X}" + ("" if srl == SRL_OFF_EXPECTED else f" (expected 0x{SRL_OFF_EXPECTED:04X})")
    if res.hold_each_s:
        findings["y_iq_while_off"] = [e["Y IQ"] for _t, e in res.hold_each_s]
    if f.get("Y"):
        findings["y_mer"] = {"before": f"0x{b['Y']['MER']:04X}", "after": f"0x{f['Y']['MER']:04X}"}
        findings["lcommerrpos"] = {ax: f[ax]["LCOMMERRPOS"] for ax in ("X", "Y")}
    checks = {
        "acknowledged": res.writes_sent == [W1_NAME] and not res.fault,
        "only_w1_written": res.writes_sent == [W1_NAME],
        "y_off_within_100ms": res.off_after_ms is not None and res.off_after_ms <= OFF_WITHIN_S * 1000,
        "y_stayed_off": bool(res.hold) and all(not s & SRL_AXIS_ON for _t, _a, s in res.hold),
        "hold_completed": not res.hold_stopped and bool(f),
    }
    if f:
        each = [e for _t, e in res.hold_each_s]
        checks["y_state_unchanged"] = (
            f["Y"]["ESTATEV"] == b["Y"]["ESTATEV"] and f["Y"]["CPOS"] == b["Y"]["CPOS"]
            and f["Y"]["ISTATUSFLAGS"] == b["Y"]["ISTATUSFLAGS"]
            and all(e["Y ESTATEV"] == b["Y"]["ESTATEV"] and e["Y CPOS"] == b["Y"]["CPOS"]
                    and e["Y ISTATUSFLAGS"] == b["Y"]["ISTATUSFLAGS"] for e in each))
        checks["x_unchanged"] = (
            bool(f["X"]["SRL"] & SRL_AXIS_ON) and abs(f["X"]["APOS"] - b["X"]["APOS"]) <= X_APOS_TOL
            and f["X"]["ESTATEV"] == 0 and f["X"]["ISTATUSFLAGS"] == b["X"]["ISTATUSFLAGS"])
        checks["all_axes_idle"] = all(v == 0 for v in f["ESTATEV_all"].values())
    return checks, findings


# ---------------------------------------------------------------------------------- Phase E
def _phase_e(link: _Link, res: MotorGate15Result) -> None:
    """After the operator's power cycle: X and Y on, homed, no trip flags. Read-only."""
    if not _connect(link, res):
        res.message = res.message.replace("; nothing written", "")
        return
    names = ("ESTATEV", "IINITONCEDONE", "SRL", "ISTATUSFLAGS", "APOS", "CPOS", "MER")
    res.final = {ax: {v: link.value(R[ax][v]) for v in names} for ax in ("X", "Y")}
    problems = []
    for ax, v in res.final.items():
        if not v["SRL"] & SRL_AXIS_ON:
            problems.append(f"{ax} power stage off (SRL 0x{v['SRL']:04X})")
        if v["IINITONCEDONE"] != INIT_DONE_MARKER:
            problems.append(f"{ax} not homed (IINITONCEDONE {v['IINITONCEDONE']})")
        if v["ISTATUSFLAGS"] & (ISF_OVERCURRENT | ISF_COMM_ABORT | ISF_OSCILLATION):
            problems.append(f"{ax} drive flags set (ISTATUSFLAGS 0x{v['ISTATUSFLAGS']:04X})")
    res.checks = {"x_and_y_on_homed_no_trip_flags": not problems}
    res.outcome = "not_recovered" if problems else "recovered"
    res.message = ("; ".join(problems) + ": power-cycle again" if problems
                   else "X and Y are on and homed again, no trip flags: Gate 15 closed")


# ---------------------------------------------------------------------------------- CLI
def write_evidence(result: MotorGate15Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate15-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate15_y_axisoff",
                                description="Motor Gate 15: one host AXISOFF to the idle Y axis. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run it (phrase: SWITCH OFF IDLE Y AXIS ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on for >= 2 minutes")
    p.add_argument("--power-cycled-no-move", action="store_true",
                   help="the microscope was power-cycled for this gate and nothing has moved since")
    p.add_argument("--stage-clear-watching-y", action="store_true",
                   help="stage clear, Z retracted, nothing in Y's path; the operator is watching the Y carriage")
    p.add_argument("--main-switch-second-person", action="store_true",
                   help="the main switch is within reach and a second person is present")
    p.add_argument("--confirm", default="", help="must be exactly: SWITCH OFF IDLE Y AXIS ON COMn")
    p.add_argument("--check-recovered", action="store_true",
                   help="Phase E, read-only: after the power cycle, check X and Y are on and homed again")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 15 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              power_cycled_no_move=a.power_cycled_no_move, stage_clear_watching_y=a.stage_clear_watching_y,
              main_switch_second_person=a.main_switch_second_person, check_recovered=a.check_recovered, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  {ex.what:16s} {ex.request_hex}")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "pass", "recovered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
