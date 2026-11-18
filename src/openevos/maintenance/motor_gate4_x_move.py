"""Motor Gate 4: first movement, X +1 mm (2,268 counts) and back to 0 (ADR-0019; L4 reviewed).

Spec: docs/evidence/fl1_v1.6_motor_gate4_x_move_spec.md. Transport as Motor Gate 1 rev 2. The ONLY writes
this module can send are constants to axis 1 (X):
  M1 CPOSCMD = 2268, M2 CPOSCMD = 0, M3 ESTATE = 8 (move), and Gate 3's verified stop W1..W3.
Speed/acceleration are never written: the drive's power-up CSPDCMD 15.0 / CACCCMD 1.0 are required instead.
Completion = SRH bit 9 set (vendor IsMoving) and ESTATEV == 13, then |APOS - target| <= LCONVERGEBAND.
On any fault after the first move write: Gate 3's stop once, read the state, send nothing else.
Dry run by default.
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
from openevos.maintenance import motor_gate3_stop as g3
from openevos.maintenance.motor_gate1 import (
    ACK,
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
    SYNC_ATTEMPTS,
    SYNC_BYTE,
    SYNC_REPLY,
    Exchange,
    Port,
    PortInfo,
    build_read,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
    parse_id_reply,
    parse_read_reply,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate4_x_move_spec.md; ADR-0019"
TARGET = 2268  # counts = 1.0001 mm on X
M1_TARGET_OUT = bytes.fromhex("08 00 10 25 D4 08 DC 00 00 F5")
M2_TARGET_HOME = bytes.fromhex("08 00 10 25 D4 00 00 00 00 11")
M3_ESTATE_MOVE = bytes.fromhex("06 00 10 21 B3 00 08 F2")
STOP = tuple(g3.WRITES)  # Gate 3's W1..W3, unchanged
ALLOWED_WRITES = frozenset({M1_TARGET_OUT, M2_TARGET_HOME, M3_ESTATE_MOVE, *(f for _n, f in STOP)})

RAW_1_0 = "00 00 00 01"  # 16.16 fixed 1.0 in vendor order
RAW_15_0 = "00 00 00 0f"  # 15.0
SRH_NOT_MOVING_BIT = 1 << 9
ESTATEV_AFTER_MOVE = 13
MAX_BAND = 50
LEG_TIMEOUT_S, POLL_S, PAUSE_S = 3.0, 0.050, 1.5
ABANDON_POLLS = 3  # consecutive polls with the drive not moving and CPOS unchanged short of the target (~5 s on the line)  # noqa: E501

def axis_reads(axis: str, axis_id: int) -> dict[str, g2.Read]:
    """Gate 2's reads for one X/Y-type axis, plus SRH, LCONVERGEBAND and CPOSCMD (same addresses on X and Y)."""
    base = {r.var: r for r in g2.READS if r.axis == axis}
    extra = (g2.Read(axis, axis_id, "SRH", "INT", 0x090F, build_read(axis_id, 0xB004, 0x090F)),
             g2.Read(axis, axis_id, "LCONVERGEBAND", "LONG", 0x03CF, build_read(axis_id, 0xB005, 0x03CF)),
             g2.Read(axis, axis_id, "CPOSCMD", "LONG", 0x03D4, build_read(axis_id, 0xB005, 0x03D4)))
    return {**base, **{r.var: r for r in extra}}


XR = axis_reads("X", 1)
_X = XR
SRH, LCONVERGEBAND, CPOSCMD = XR["SRH"], XR["LCONVERGEBAND"], XR["CPOSCMD"]
READINESS = tuple(r for r in g2.READS if r.var in g2.READY)
ESTATEV_ALL = tuple(r for r in g2.READS if r.var == "ESTATEV")
BASELINE = ("APOS", "CPOS", "CPOSCMD", "LPOSMIN", "LPOSMAX", "CACCSTOP", "CSPDCMD", "CACCCMD", "LCONVERGEBAND", "SRH")
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in READINESS), *(r.frame for r in XR.values())})


@dataclass(frozen=True)
class AxisPlan:
    """What the shared move/stop machinery needs for one axis: its reads, its move request and its stop."""

    axis: str
    reads: dict
    move: bytes
    stop: tuple


X_PLAN = AxisPlan("X", XR, M3_ESTATE_MOVE, STOP)


class MotorGate4Refused(RuntimeError):
    pass


def assert_allowed(request: bytes) -> None:
    if request == bytes([SYNC_BYTE]) or request in ALLOWED_READS or request in ALLOWED_WRITES:
        return
    raise MotorGate4Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 4 frames")


@dataclass
class MotorGate4Result:
    outcome: str  # dry_run | refused | driver_state | stopped | not_idle | baseline_refused | fault_stopped | fail | pass  # noqa: E501
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
    baseline: dict = field(default_factory=dict)
    legs: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    fault: str = ""
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def confirm_phrase(device: str) -> str:
    return f"MOVE X 1MM AND BACK ON {device.upper()}"


class _Fault(Exception):
    pass


class DriftRefused(_Fault):
    """The axis' encoder disagrees with its command by more than DRIFT_MAX_COUNTS before a move."""


#: max |APOS - CPOS| allowed before a move, and max drift of an idle axis during the other's move (ADR-0030)
DRIFT_MAX_COUNTS = 3


# ---------------------------------------------------------------------------------- exchange primitives
class _Link:
    def __init__(self, port: Port, res: MotorGate4Result, monotonic, sleep, allowed_writes=ALLOWED_WRITES,
                 allowed_reads=ALLOWED_READS):
        self.port, self.res, self.monotonic, self.sleep = port, res, monotonic, sleep
        self.allowed_writes = frozenset(allowed_writes)
        self.allowed_reads = frozenset(allowed_reads)

    def sync(self, label: str) -> bool:
        ex = Exchange(label, f"{SYNC_BYTE:02x}", attempts=0)
        self.res.exchanges.append(ex)
        self.port.purge_rx()
        for _ in range(SYNC_ATTEMPTS):
            assert_allowed(bytes([SYNC_BYTE]))
            ex.attempts += 1
            self.res.bytes_written += self.port.write(bytes([SYNC_BYTE]))
            got = self.port.read(1)
            if got == bytes([SYNC_REPLY]):
                ex.reply_hex = got.hex()
                if ex.attempts > 1:
                    # an earlier FF's 0D may still be on its way: let it arrive, then drop it, so it cannot
                    # prefix the next reply (seen on the instrument: Gate 8, 2026-11-13, reply "0D 4F ...")
                    self.sleep(0.030)
                    self.port.purge_rx()
                return True
            self.sleep(0.010)
        ex.problem = f"no 0D after {SYNC_ATTEMPTS} attempts"
        return False

    def read(self, r: g2.Read) -> dict:
        if r.frame not in self.allowed_reads:
            raise MotorGate4Refused(f"read {r.frame.hex(' ')} is not one of this gate's reads")
        if not self.sync(f"line sync before {r.axis} {r.var}"):
            raise _Fault(f"line sync lost before {r.axis} {r.var}")
        ex = Exchange(f"{r.axis} {r.var}", r.frame.hex(" "))
        self.res.exchanges.append(ex)
        t0 = self.monotonic()
        self.res.bytes_written += self.port.write(r.frame)
        self.sleep(0.050)
        raw = self.port.read(32)
        ex.ms = round((self.monotonic() - t0) * 1000, 1)
        ex.reply_hex = raw.hex(" ") if raw else None
        # A late sync reply (0D) can still precede the real reply even after a first-attempt sync (seen
        # 2026-11-13, Gate 8 X-only run). A real reply always starts with 'O', so leading 0D bytes are dropped.
        if raw and raw[0] == SYNC_REPLY:
            raw = raw.lstrip(bytes([SYNC_REPLY]))
            ex.problem = "leading late sync byte(s) dropped"
        value, why = parse_read_reply(raw, r.axis_id, r.address, r.bits) if raw else (None, "no reply")
        if value is None:
            ex.problem = why
            raise _Fault(f"{r.axis} {r.var}: {why}")
        ex.value = g2.decode_value(value, r.vtype)
        return ex.value

    def query_id(self) -> int | None:
        assert_allowed(ID_QUERY)
        ex = Exchange("axis-ID query", ID_QUERY.hex(" "))
        self.res.exchanges.append(ex)
        self.res.bytes_written += self.port.write(ID_QUERY)
        self.sleep(0.050)
        raw = self.port.read(32)
        ex.reply_hex = raw.hex(" ") if raw else None
        gid, ex.problem = parse_id_reply(raw) if raw else (None, "no reply")
        return gid

    def write(self, name: str, frame: bytes) -> None:
        if frame not in self.allowed_writes:
            raise MotorGate4Refused(f"write {frame.hex(' ')} is not one of this gate's constants")
        if not self.sync(f"line sync before {name}"):
            raise _Fault(f"line sync lost before {name}")
        ex = Exchange(name, frame.hex(" "))
        self.res.exchanges.append(ex)
        t0 = self.monotonic()
        self.res.bytes_written += self.port.write(frame)
        self.res.writes_sent.append(name)
        got = b""
        for _ in range(g3.ACK_READ_ATTEMPTS):
            got = self.port.read(1)
            if got:
                break
            self.sleep(0.001)
        ex.ms = round((self.monotonic() - t0) * 1000, 1)
        ex.reply_hex = got.hex() if got else None
        if got != bytes([ACK]):
            ex.problem = "no acknowledgement" if not got else f"acknowledgement 0x{got[0]:02X}"
            raise _Fault(f"{name}: {ex.problem}")


# ---------------------------------------------------------------------------------- run
def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_watching: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> MotorGate4Result:  # noqa: E501
    res = MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())  # noqa: E501
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would open {dev}, check readiness and the X baseline, move X to +{TARGET} counts "
                       "(1.000 mm), pause, return to 0, stop to idle, and verify; nothing opened or sent")
        res.exchanges = [Exchange(n, f.hex(" ")) for n, f in
                         (("M1 CPOSCMD=2268", M1_TARGET_OUT), ("M3 ESTATE=8", M3_ESTATE_MOVE),
                          ("M2 CPOSCMD=0", M2_TARGET_HOME), ("M3 ESTATE=8", M3_ESTATE_MOVE), *STOP)]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
        return res
    if not stage_empty_watching:
        res.message = ("operator has not confirmed an empty, unobstructed stage and watching X with a hand near "
                       "the power switch; nothing opened")
        return res
    if confirm != confirm_phrase(dev):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev)!r}); nothing opened"
        return res
    try:
        port = open_port(dev)
    except Exception as exc:  # noqa: BLE001
        res.message = f"could not open {dev}: {exc}; nothing sent"
        return res
    try:
        res.port_state_read_back = port.inspect()
        res.driver_state_problems = driver_state_problems(res.port_state_read_back)
        if res.driver_state_problems:
            res.outcome = "driver_state"
            res.message = "driver state is not normal (" + "; ".join(res.driver_state_problems) + "). Nothing sent"
            return res
        _procedure(_Link(port, res, monotonic, sleep), res)
    finally:
        port.close()
    return res


def _procedure(link: _Link, res: MotorGate4Result) -> None:
    # Phase A
    for baud in BAUD_PASSES:
        link.port.configure(baud)
        if link.sync(f"line sync @ {baud}"):
            res.baud = baud
            break
    if res.baud is None:
        res.outcome, res.message = "stopped", "no line sync at either rate; nothing written"
        return
    gid = link.query_id()
    res.gateway_axis_id = gid
    if gid != 1:
        res.outcome, res.message = "stopped", f"gateway axis ID {gid}, not 1; nothing written"
        return
    try:
        for r in READINESS:
            v = link.read(r)
            if (r.var == "ESTATEV" and v["u16"] != 0) or (r.var == "IINITONCEDONE" and v["u16"] != INIT_DONE_MARKER):
                res.outcome, res.message = "not_idle", f"{r.axis} {r.var} = {v['u16']}; nothing written"
                return
        # Phase B
        res.baseline = {name: link.read(XR[name]) for name in BASELINE}
    except _Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    problems = baseline_problems(res.baseline)
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    band = res.baseline["LCONVERGEBAND"]["i32"]
    try:
        _leg(link, res, "out", "M1 CPOSCMD=2268", M1_TARGET_OUT, TARGET, band)
        link.sleep(PAUSE_S)
        _leg(link, res, "home", "M2 CPOSCMD=0", M2_TARGET_HOME, 0, band)
        for name, frame in STOP:  # Phase F: back to ESTATEV 0 through the verified stop
            link.write(name, frame)
        res.final = _final(link, band)
    except _Fault as f:
        _fault_stop(link, res, str(f))
        return
    failed = [k for k, v in res.final["checks"].items() if not v]
    res.outcome = "fail" if failed else "pass"
    res.message = ("X moved +1 mm and back to 0; positions within band; stopped to idle; all axes idle; "
                   "parameters unchanged" if not failed else "FAILED final checks: " + ", ".join(failed))


def baseline_problems(b: dict, target: int = TARGET, axis: str = "X") -> list[str]:
    p = []
    if not (b["APOS"]["i32"] == b["CPOS"]["i32"] == 0):
        p.append(f"{axis} not exactly at home (APOS {b['APOS']['i32']}, CPOS {b['CPOS']['i32']})")
    if not (b["LPOSMIN"]["i32"] <= 0 and b["LPOSMAX"]["i32"] >= target):
        p.append(f"limits ({b['LPOSMIN']['i32']}, {b['LPOSMAX']['i32']}) do not cover 0..{target}")
    if b["CSPDCMD"]["raw"] != RAW_15_0:
        p.append(f"CSPDCMD raw {b['CSPDCMD']['raw']} is not the power-up 15.0")
    if b["CACCCMD"]["raw"] != RAW_1_0:
        p.append(f"CACCCMD raw {b['CACCCMD']['raw']} is not the power-up 1.0")
    if b["CACCSTOP"]["raw"] != RAW_1_0:
        p.append(f"CACCSTOP raw {b['CACCSTOP']['raw']} is not 1.0")
    if not 0 < b["LCONVERGEBAND"]["i32"] <= MAX_BAND:
        p.append(f"LCONVERGEBAND {b['LCONVERGEBAND']['i32']} outside 1..{MAX_BAND}")
    if not b["SRH"]["u16"] & SRH_NOT_MOVING_BIT:
        p.append("SRH bit 9 clear (moving)")
    return p


def _leg(link: _Link, res: MotorGate4Result, leg: str, name: str, target_frame: bytes, target: int, band: int,
         plan: AxisPlan = X_PLAN, timeout_s: float = LEG_TIMEOUT_S, watch: tuple = ()) -> None:
    """`watch`: (name, read) pairs sampled on every poll (read-only), e.g. the other axis' encoder."""
    rd = plan.reads
    trace = []
    res.legs[leg] = {"target": target, "trace": trace}
    # Commutation-drift interlock (ADR-0030): a closed-loop stepper uses the encoder to commutate; 1 count =
    # 6.25 electrical degrees here, and past ~14 counts of encoder/rotor mismatch the torque reverses (Y ran
    # backwards twice on 2026-11-13). Refuse to move unless encoder and command agree within DRIFT_MAX_COUNTS.
    apos0, cpos0 = link.read(rd["APOS"])["i32"], link.read(rd["CPOS"])["i32"]
    res.legs[leg]["pre_drift"] = apos0 - cpos0
    if abs(apos0 - cpos0) > DRIFT_MAX_COUNTS:
        raise DriftRefused(f"{plan.axis} encoder {apos0} vs command {cpos0}: drift {apos0 - cpos0} counts "
                           f"> {DRIFT_MAX_COUNTS}; nothing written; power-cycle to re-home before moving {plan.axis}")
    link.write(name, target_frame)
    link.write("M3 ESTATE=8", plan.move)
    polls = int(timeout_s / POLL_S)
    # Each poll costs ~0.4 s per read on the real line, so a poll count alone stretched a 15 s timeout to
    # ~6 min (2026-11-13). The wall-clock deadline bounds it; the poll cap still bounds a frozen clock.
    deadline = link.monotonic() + timeout_s
    done, still, prev = False, 0, None
    for _ in range(polls):
        if link.monotonic() > deadline:
            break
        link.sleep(POLL_S)
        srh = link.read(rd["SRH"])["u16"]
        st = link.read(rd["ESTATEV"])["u16"]
        cpos = link.read(rd["CPOS"])["i32"]
        trace.append({"SRH": srh, "ESTATEV": st, "CPOS": cpos})
        for wname, wread in watch:
            trace[-1][wname] = link.read(wread)["i32"]
        # The previous leg's "complete" state (bit 9 + ESTATEV 13) can still be latched at the first poll
        # of a new leg (seen 2026-11-13, Y home after an out leg that settled 10 counts off). Completion
        # therefore also requires the commanded trajectory to have reached this leg's target.
        if srh & SRH_NOT_MOVING_BIT and st == ESTATEV_AFTER_MOVE and cpos == target:
            done = True
            break
        # The drive itself can end a move short of the target (2026-11-13, Y near 73.5 mm: CPOS = APOS, idle).
        # Not moving, trajectory frozen short of the target for ABANDON_POLLS polls in a row: stop waiting.
        still = still + 1 if (srh & SRH_NOT_MOVING_BIT and cpos == prev and cpos != target) else 0
        prev = cpos
        if still >= ABANDON_POLLS:
            raise _Fault(f"{leg} leg: the drive ended the move at CPOS {cpos} (ESTATEV {st}), short of {target}")
    if not done:
        raise _Fault(f"{leg} leg did not complete within {timeout_s} s")
    apos, cpos = link.read(rd["APOS"])["i32"], link.read(rd["CPOS"])["i32"]
    res.legs[leg].update(APOS=apos, CPOS=cpos, error=apos - target)
    if abs(apos - target) > band or cpos != target:
        raise _Fault(f"{leg} leg ended at APOS {apos}, CPOS {cpos}; target {target}, band {band}")


def _final(link: _Link, band: int, plan: AxisPlan = X_PLAN) -> dict:
    x = {n: link.read(plan.reads[n]) for n in ("ESTATEV", "APOS", "CPOS", "CACCSTOP", "CSPDCMD", "CACCCMD")}
    idle = {r.axis: link.read(r)["u16"] for r in ESTATEV_ALL}
    checks = {
        "x_idle": x["ESTATEV"]["u16"] == 0,
        "all_axes_idle": all(v == 0 for v in idle.values()),
        "x_home_within_band": abs(x["APOS"]["i32"]) <= band and abs(x["CPOS"]["i32"]) <= band,
        "caccstop_unchanged": x["CACCSTOP"]["raw"] == RAW_1_0,
        "speed_unchanged": x["CSPDCMD"]["raw"] == RAW_15_0,
        "accel_unchanged": x["CACCCMD"]["raw"] == RAW_1_0,
    }
    return {"x": {k: v.get("i32", v.get("u16")) for k, v in x.items()}, "ESTATEV_all": idle, "checks": checks}


def _fault_stop(link: _Link, res: MotorGate4Result, why: str, plan: AxisPlan = X_PLAN) -> None:
    """One controlled stop (the axis' verified stop frames), then read-only state capture. Never anything else."""
    res.fault = why
    res.outcome = "fault_stopped"
    stop_note = "stop sent and acknowledged"
    try:
        for name, frame in plan.stop:
            link.write(f"FAULT {name}", frame)
    except _Fault as f:
        stop_note = f"STOP NOT COMPLETED ({f}); if anything still moves, switch the instrument off"
    try:
        res.final = {"after_fault": {n: link.read(plan.reads[n]) for n in ("ESTATEV", "APOS", "CPOS")},
                     "SRH": link.read(plan.reads["SRH"])}
    except _Fault as f:
        res.final = {"after_fault_read_error": str(f)}
    res.message = f"FAULT: {why}. {stop_note}. Nothing further sent; recovery = operator power cycle"


def write_evidence(result: MotorGate4Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate4-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate4_x_move",
                                description="Motor Gate 4: move X +1 mm and back to 0. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run it (phrase: MOVE X 1MM AND BACK ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on for >= 2 minutes")
    p.add_argument("--stage-empty-watching", action="store_true",
                   help="no vessel on the stage, path clear, operator watching X with a hand near the power switch")
    p.add_argument("--confirm", default="", help="must be exactly: MOVE X 1MM AND BACK ON COMn")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 4 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_empty_watching=a.stage_empty_watching, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  {ex.what:28s} {ex.request_hex}")
    for leg, d in res.legs.items():
        if "APOS" in d:
            print(f"  leg {leg}: target {d['target']}, APOS {d['APOS']}, polls {len(d['trace'])}")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
