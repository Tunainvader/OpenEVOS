"""Motor Gate 3: controlled stop on the idle X axis, the first write to a drive (ADR-0018; L4 reviewed).

Spec: docs/evidence/fl1_v1.6_motor_gate3_stop_spec.md. Transport and reads are Motor Gate 1 rev 2 / Gate 2.
The ONLY writes this module can send are the three constants W1..W3 on axis 1 (X):
  W1 IPENDINGMOVES = 1, W2 CACCSTOP = 1.0 (the value just read), W3 ESTATE = 6 (STATE_6_STOP).
Each write is acknowledged by a single 'O' (0x4F) byte (vendor FUN_1800333B0). Expected motion: none.

Phases: 0 refuse before opening; A port + readiness; B X baseline (APOS == CPOS, inside limits, CACCSTOP
raw 00 00 00 01); C W1, W2, W3 with ack checks, fail fast; D poll X ESTATEV <= 10 x 50 ms, re-read and verify.
No halt, unhalt, move, second stop or retry, whatever happens. Dry run by default.
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
from openevos.maintenance.motor_gate1 import (
    ACK,
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
    SYNC_BYTE,
    Exchange,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
    parse_id_reply,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate3_stop_spec.md; ADR-0018"
W1_IPENDINGMOVES = bytes.fromhex("06 00 10 21 CD 00 01 05")
W2_CACCSTOP_1_0 = bytes.fromhex("08 00 10 25 E8 00 00 00 01 26")
W3_ESTATE_STOP = bytes.fromhex("06 00 10 21 B3 00 06 F0")
WRITES = (("W1 IPENDINGMOVES=1", W1_IPENDINGMOVES), ("W2 CACCSTOP=1.0", W2_CACCSTOP_1_0),
          ("W3 ESTATE=6 (STATE_6_STOP)", W3_ESTATE_STOP))
CACCSTOP_EXPECTED_RAW = "00 00 00 01"
ACK_READ_ATTEMPTS = 10  # as the vendor's read loop
POLLS, POLL_INTERVAL_S = 10, 0.050

X_BASELINE = ("APOS", "CPOS", "LPOSMIN", "LPOSMAX", "CACCSTOP")
_X = {r.var: r for r in g2.READS if r.axis == "X"}
READINESS = tuple(r for r in g2.READS if r.var in g2.READY)
ESTATEV_ALL = tuple(r for r in g2.READS if r.var == "ESTATEV")
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in READINESS), *(_X[v].frame for v in (*X_BASELINE, "ESTATEV"))})
ALLOWED_WRITES = frozenset(f for _n, f in WRITES)


class MotorGate3Refused(RuntimeError):
    pass


def assert_allowed(request: bytes) -> None:
    if request == bytes([SYNC_BYTE]) or request in ALLOWED_READS or request in ALLOWED_WRITES:
        return
    raise MotorGate3Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 3 frames")


@dataclass
class MotorGate3Result:
    outcome: str  # dry_run | refused | driver_state | stopped | not_idle | baseline_refused | write_failed | fail | pass  # noqa: E501
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
    axes: dict[str, dict] = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    polls: list[int] = field(default_factory=list)
    after: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def confirm_phrase(device: str) -> str:
    return f"STOP IDLE X AXIS ON {device.upper()}"


# ---------------------------------------------------------------------------------- exchanges
def _guarded_read(port, res, r, monotonic, sleep):
    assert_allowed(r.frame)
    return g2._read(port, res, r, monotonic, sleep)  # sync + one write + one read, reply checks


def _write(port: Port, res: MotorGate3Result, name: str, frame: bytes, monotonic, sleep) -> bool:
    """Line sync, one write of a constant frame, then exactly 1 acknowledgement byte, which must be 'O'."""
    if frame not in ALLOWED_WRITES:
        raise MotorGate3Refused(f"write {frame.hex(' ')} is not one of W1..W3")
    if not g2._sync(port, res, f"line sync before {name}", monotonic, sleep):
        res.message = f"line sync lost before {name}; no further write"
        return False
    assert_allowed(frame)
    ex = Exchange(name, frame.hex(" "))
    res.exchanges.append(ex)
    t0 = monotonic()
    res.bytes_written += port.write(frame)
    res.writes_sent.append(name)
    got = b""
    for _ in range(ACK_READ_ATTEMPTS):
        got = port.read(1)
        if got:
            break
        sleep(0.001)
    ex.ms = round((monotonic() - t0) * 1000, 1)
    ex.reply_hex = got.hex() if got else None
    if got != bytes([ACK]):
        ex.problem = "no acknowledgement" if not got else f"acknowledgement byte 0x{got[0]:02X}, expected 0x4F"
        res.message = f"{name}: {ex.problem}; no further write, not retried"
        return False
    return True


def _snapshot(port, res, names, monotonic, sleep) -> dict | None:
    out = {}
    for v in names:
        val = _guarded_read(port, res, _X[v], monotonic, sleep)
        if val is None:
            return None
        out[v] = val
    return out


def _all_idle(port, res, monotonic, sleep) -> dict | None:
    out = {}
    for r in ESTATEV_ALL:
        v = _guarded_read(port, res, r, monotonic, sleep)
        if v is None:
            return None
        out[r.axis] = v["u16"]
    return out


def _phase_d(port, res: MotorGate3Result, monotonic, sleep) -> None:
    """Completion poll and integrity reads (read-only). Never sends a write."""
    for i in range(POLLS):
        v = _guarded_read(port, res, _X["ESTATEV"], monotonic, sleep)
        if v is None:
            return
        res.polls.append(v["u16"])
        if v["u16"] == 0:
            break
        if i < POLLS - 1:
            sleep(POLL_INTERVAL_S)
    after = _snapshot(port, res, ("APOS", "CPOS", "CACCSTOP"), monotonic, sleep)
    if after is None:
        return
    idle = _all_idle(port, res, monotonic, sleep)
    if idle is None:
        return
    res.after = {**after, "ESTATEV_all": idle}


# ---------------------------------------------------------------------------------- run
def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_clear_watching: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> MotorGate3Result:  # noqa: E501
    res = MotorGate3Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())  # noqa: E501
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would open {dev}, check readiness on all axes and the X baseline, then send "
                       f"W1, W2, W3 to X once each (ack 'O'), poll ESTATEV and verify; nothing opened or sent")
        res.exchanges = [Exchange(n, f.hex(" ")) for n, f in WRITES]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
        return res
    if not stage_clear_watching:
        res.message = "operator has not confirmed the stage is clear and they are watching X; nothing opened"
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
        _procedure(port, res, monotonic, sleep)
    finally:
        port.close()
    return res


def _procedure(port: Port, res: MotorGate3Result, monotonic, sleep) -> None:
    # Phase A
    for baud in BAUD_PASSES:
        port.configure(baud)
        if g2._sync(port, res, f"line sync @ {baud}", monotonic, sleep):
            res.baud = baud
            break
    if res.baud is None:
        res.outcome, res.message = "stopped", "no line sync at either rate; nothing written"
        return
    assert_allowed(ID_QUERY)
    ex = g2._request(port, res, ID_QUERY, "axis-ID query", monotonic, sleep)
    gid, why = parse_id_reply(bytes.fromhex(ex.reply_hex)) if ex.reply_hex else (None, ex.problem)
    res.gateway_axis_id = gid
    if gid != 1:
        res.outcome, res.message = "stopped", f"axis-ID query: {why or f'ID {gid}, not 1'}; nothing written"
        return
    for r in READINESS:
        v = _guarded_read(port, res, r, monotonic, sleep)
        if v is None:
            res.message += "; nothing written"
            return
        if (r.var == "ESTATEV" and v["u16"] != 0) or (r.var == "IINITONCEDONE" and v["u16"] != INIT_DONE_MARKER):
            res.outcome = "not_idle"
            res.message = f"{r.axis} {r.var} = {v['u16']}: not idle/homed; nothing written"
            return
    # Phase B
    base = _snapshot(port, res, X_BASELINE, monotonic, sleep)
    if base is None:
        res.message += "; nothing written"
        return
    res.baseline = base
    apos, cpos = base["APOS"]["i32"], base["CPOS"]["i32"]
    lo, hi = base["LPOSMIN"]["i32"], base["LPOSMAX"]["i32"]
    problems = []
    if apos != cpos:
        problems.append(f"APOS {apos} != CPOS {cpos}")
    if not lo < hi:
        problems.append(f"limits not ordered ({lo}, {hi})")
    elif not lo <= apos <= hi:
        problems.append(f"APOS {apos} outside limits ({lo}, {hi})")
    if base["CACCSTOP"]["raw"] != CACCSTOP_EXPECTED_RAW:
        problems.append(f"CACCSTOP raw {base['CACCSTOP']['raw']} != {CACCSTOP_EXPECTED_RAW} (1.0)")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    # Phase C
    for name, frame in WRITES:
        if not _write(port, res, name, frame, monotonic, sleep):
            why = res.message
            _phase_d(port, res, monotonic, sleep)  # record the state; read-only
            res.outcome = "write_failed"
            res.message = why + (" | post-read: " + res.message if res.message != why else "")
            return
    # Phase D
    _phase_d(port, res, monotonic, sleep)
    res.checks = evaluate(res)
    ok = all(res.checks.values()) and bool(res.checks)
    res.outcome = "pass" if ok else "fail"
    failed = [k for k, v in res.checks.items() if not v]
    res.message = ("controlled stop on idle X: returned to idle, position and CACCSTOP unchanged, all axes idle"
                   if ok else "FAILED checks: " + ", ".join(failed) + ". Nothing further sent; recovery = power cycle")


def evaluate(res: MotorGate3Result) -> dict[str, bool]:
    a, b = res.after, res.baseline
    if not a:
        return {"post_reads_complete": False}
    return {
        "post_reads_complete": True,
        "x_returned_to_idle": bool(res.polls) and res.polls[-1] == 0,
        "apos_unchanged": a["APOS"]["i32"] == b["APOS"]["i32"],
        "cpos_equals_baseline_apos": a["CPOS"]["i32"] == b["APOS"]["i32"],
        "caccstop_unchanged": a["CACCSTOP"]["raw"] == CACCSTOP_EXPECTED_RAW,
        "all_axes_idle": all(v == 0 for v in a["ESTATEV_all"].values()),
    }


def write_evidence(result: MotorGate3Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate3-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate3_stop",
                                description="Motor Gate 3: controlled stop on the idle X axis. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run it (phrase: STOP IDLE X AXIS ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on for >= 2 minutes")
    p.add_argument("--stage-clear-watching", action="store_true",
                   help="the stage is clear and the operator is watching the X carriage")
    p.add_argument("--confirm", default="", help="must be exactly: STOP IDLE X AXIS ON COMn")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 3 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_clear_watching=a.stage_clear_watching, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  {ex.what:28s} {ex.request_hex}")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
