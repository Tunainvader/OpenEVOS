"""Motor Gate 2: read-only check of the drives' safety-relevant variables (ADR-0017; L3 reviewed read).

Spec: docs/evidence/fl1_v1.6_motor_gate2_spec.md. Reuses Motor Gate 1 rev 2 unchanged for the port
(identification, driver-state check, vendor DCB/timeouts, 115,200 then 9,600, per-frame line sync) and
for reply checking. What this module can send: the sync byte, the axis-ID query and the 41 fixed reads in
READS (per-axis addresses from each axis' variables.cfg). Only opcodes 0xB000/0xB004/0xB005 exist here.

Order: axis-ID query; all readiness reads (stop unless every axis idle and X/Y/Z/F homed); then the
parameter reads. Values are decoded in the vendor's 32-bit order (low word first, each word big-endian)
and checked offline (limits ordered and non-zero, spans vs the mcba spans, CACCSTOP > 0, |APOS-CPOS|).
Values are CANDIDATE. Dry run by default.
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

from openevos.maintenance.motor_gate1 import (
    ACK,
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
    OP_READ16,
    OP_READ32,
    SYNC_ATTEMPTS,
    SYNC_BYTE,
    SYNC_REPLY,
    UNITS,
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

SPEC = "docs/evidence/fl1_v1.6_motor_gate2_spec.md; ADR-0017"
MCBA_SPAN_MM = {"X": 119.1, "Y": 78.1, "Z": 8.4}  # from the stored mcba limits (motor L0 §3a)
READY = ("ESTATEV", "IINITONCEDONE")

# (axis, ID, [(variable, type, address)]) in spec §3 order; INT = 16-bit, LONG/FIXED = 32-bit
_TABLE = (
    ("X", 1, [("ESTATEV", "INT", 0x03EA), ("IINITONCEDONE", "INT", 0x03FE), ("APOS", "LONG", 0x0228),
              ("CPOS", "LONG", 0x029E), ("LPOSMIN", "LONG", 0x03B4), ("LPOSMAX", "LONG", 0x03B6),
              ("CACCSTOP", "FIXED", 0x03E8), ("CSPDCMD", "FIXED", 0x03D6), ("CACCCMD", "FIXED", 0x03D8)]),
    ("Y", 2, [("ESTATEV", "INT", 0x03EA), ("IINITONCEDONE", "INT", 0x03FE), ("APOS", "LONG", 0x0228),
              ("CPOS", "LONG", 0x029E), ("LPOSMIN", "LONG", 0x03B4), ("LPOSMAX", "LONG", 0x03B6),
              ("CACCSTOP", "FIXED", 0x03E8), ("CSPDCMD", "FIXED", 0x03D6), ("CACCCMD", "FIXED", 0x03D8)]),
    ("P", 3, [("ESTATEV", "INT", 0x03B2), ("APOS", "LONG", 0x0228), ("CPOS", "LONG", 0x029E)]),
    ("Z", 4, [("ESTATEV", "INT", 0x03B2), ("IINITONCEDONE", "INT", 0x03F6), ("APOS", "LONG", 0x0228),
              ("CPOS", "LONG", 0x029E), ("LPOSMIN", "LONG", 0x03BD), ("LPOSMAX", "LONG", 0x03BF),
              ("CACCSTOP", "FIXED", 0x03BB), ("CSPDCMD", "FIXED", 0x03B5), ("CACCCMD", "FIXED", 0x03B7)]),
    ("F", 5, [("ESTATEV", "INT", 0x03B6), ("IINITONCEDONE", "INT", 0x03F8), ("APOS", "LONG", 0x0228),
              ("CPOS", "LONG", 0x029E), ("LPOSMIN", "LONG", 0x03EA), ("LPOSMAX", "LONG", 0x03EC),
              ("CSPDCMD", "FIXED", 0x03BE), ("CACCCMD", "FIXED", 0x03C0)]),
    ("O", 6, [("ESTATEV", "INT", 0x03B2), ("APOS", "LONG", 0x0228), ("CPOS", "LONG", 0x029E)]),
)


@dataclass(frozen=True)
class Read:
    axis: str
    axis_id: int
    var: str
    vtype: str
    address: int
    frame: bytes

    @property
    def bits(self) -> int:
        return 16 if self.vtype == "INT" else 32


READS = tuple(Read(ax, aid, v, t, a, build_read(aid, OP_READ16 if t == "INT" else OP_READ32, a))
              for ax, aid, vs in _TABLE for v, t, a in vs)
ALLOWED_REQUESTS = frozenset({ID_QUERY, *(r.frame for r in READS)})


class MotorGate2Refused(RuntimeError):
    pass


def assert_allowed(request: bytes) -> None:
    if request == bytes([SYNC_BYTE]) or request in ALLOWED_REQUESTS:
        return
    raise MotorGate2Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 2 frames")


def decode_value(raw: bytes, vtype: str) -> dict:
    """Vendor order (Verified): 32-bit = bytes[2:4] (high word) then bytes[0:2] (low word), each big-endian."""
    if vtype == "INT":
        return {"raw": raw.hex(" "), "u16": int.from_bytes(raw, "big"), "i16": int.from_bytes(raw, "big", signed=True)}
    v = int.from_bytes(raw[2:4] + raw[0:2], "big", signed=True)
    out = {"raw": raw.hex(" "), "i32": v}
    if vtype == "FIXED":
        out["fixed"] = round(v / 65536, 6)
    return out


# ---------------------------------------------------------------------------------- result
@dataclass
class MotorGate2Result:
    outcome: str  # "dry_run" | "refused" | "driver_state" | "stopped" | "not_idle" | "complete"
    message: str
    started_utc: str
    host: str
    port: PortInfo | None = None
    port_state_read_back: dict | None = None
    driver_state_problems: list[str] = field(default_factory=list)
    baud: int | None = None
    bytes_written: int = 0
    gateway_axis_id: int | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    axes: dict[str, dict] = field(default_factory=dict)
    checks: dict[str, dict] = field(default_factory=dict)
    capability_source: str = "CANDIDATE"
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def confirm_phrase(device: str) -> str:
    return f"READ MOTOR LIMITS ON {device.upper()}"


# ---------------------------------------------------------------------------------- exchange
def _sync(port: Port, res: MotorGate2Result, label: str, monotonic, sleep) -> bool:
    ex = Exchange(label, f"{SYNC_BYTE:02x}", attempts=0)
    res.exchanges.append(ex)
    port.purge_rx()
    t0 = monotonic()
    for _ in range(SYNC_ATTEMPTS):
        assert_allowed(bytes([SYNC_BYTE]))
        ex.attempts += 1
        res.bytes_written += port.write(bytes([SYNC_BYTE]))
        got = port.read(1)
        if got == bytes([SYNC_REPLY]):
            ex.reply_hex, ex.ms = got.hex(), round((monotonic() - t0) * 1000, 1)
            return True
        if got:
            ex.reply_hex = got.hex()
        sleep(0.010)
    ex.ms, ex.problem = round((monotonic() - t0) * 1000, 1), f"no 0D after {SYNC_ATTEMPTS} attempts"
    return False


def _request(port: Port, res: MotorGate2Result, request: bytes, what: str, monotonic, sleep) -> Exchange:
    assert_allowed(request)
    ex = Exchange(what, request.hex(" "))
    res.exchanges.append(ex)
    t0 = monotonic()
    res.bytes_written += port.write(request)
    sleep(0.050)
    raw = port.read(32)
    ex.ms = round((monotonic() - t0) * 1000, 1)
    ex.reply_hex = raw.hex(" ") if raw else None
    if not raw:
        ex.problem = "no reply"
    elif raw[0] != ACK:
        ex.problem = f"no acknowledgement (0x{raw[0]:02X})"
    return ex


def _read(port: Port, res: MotorGate2Result, r: Read, monotonic, sleep) -> dict | None:
    if not _sync(port, res, f"line sync before {r.axis} {r.var}", monotonic, sleep):
        res.outcome, res.message = "stopped", f"line sync lost before {r.axis} {r.var}; stopped"
        return None
    ex = _request(port, res, r.frame, f"{r.axis} {r.var}", monotonic, sleep)
    value, why = parse_read_reply(bytes.fromhex(ex.reply_hex), r.axis_id, r.address, r.bits) if ex.reply_hex \
        else (None, ex.problem)
    if value is None:
        ex.problem = why
        res.outcome, res.message = "stopped", f"{r.axis} {r.var}: {why}. Not retried"
        return None
    ex.value = decode_value(value, r.vtype)
    res.axes.setdefault(r.axis, {"id": r.axis_id})[r.var] = ex.value
    return ex.value


def _exchange_all(port: Port, res: MotorGate2Result, monotonic, sleep) -> None:
    for baud in BAUD_PASSES:
        port.configure(baud)
        if _sync(port, res, f"line sync @ {baud}", monotonic, sleep):
            res.baud = baud
            break
    if res.baud is None:
        res.outcome = "stopped"
        res.message = f"no line sync at {' or '.join(map(str, BAUD_PASSES))} baud; no frame sent"
        return
    ex = _request(port, res, ID_QUERY, "axis-ID query", monotonic, sleep)
    gid, why = parse_id_reply(bytes.fromhex(ex.reply_hex)) if ex.reply_hex else (None, ex.problem)
    if gid is None:
        ex.problem = why
        res.outcome, res.message = "stopped", f"axis-ID query: {why}. Not retried"
        return
    res.gateway_axis_id = gid
    if gid != 1:
        res.outcome, res.message = "stopped", f"gateway reports axis ID {gid}, not 1; stopped"
        return
    readiness = [r for r in READS if r.var in READY]
    for r in readiness:
        v = _read(port, res, r, monotonic, sleep)
        if v is None:
            return
        if r.var == "ESTATEV" and v["u16"] != 0:
            res.outcome, res.message = "not_idle", f"{r.axis} state is {v['u16']}, not idle; no parameter read"
            return
        if r.var == "IINITONCEDONE" and v["u16"] != INIT_DONE_MARKER:
            res.outcome, res.message = "not_idle", f"{r.axis} not initialised (marker {v['u16']}); no parameter read"
            return
    for r in READS:
        if r.var not in READY and _read(port, res, r, monotonic, sleep) is None:
            return
    res.checks = evaluate(res.axes)
    res.outcome = "complete"
    res.message = f"all {len(READS)} reads answered at {res.baud} baud. " + summary(res.checks)


# ---------------------------------------------------------------------------------- offline checks
def evaluate(axes: dict[str, dict], mcba: dict[str, tuple[int, int]] | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ax, info in axes.items():
        c: dict = {}
        if "APOS" in info and "CPOS" in info:
            c["holding_error_counts"] = abs(info["APOS"]["i32"] - info["CPOS"]["i32"])
        if "LPOSMIN" in info and "LPOSMAX" in info:
            lo, hi = info["LPOSMIN"]["i32"], info["LPOSMAX"]["i32"]
            c["limits_counts"] = [lo, hi]
            c["limits_zero"] = lo == 0 and hi == 0
            c["limits_ordered"] = lo < hi
            if ax in UNITS:
                cpr, mmrev = UNITS[ax]
                span = round((hi - lo) / cpr * mmrev, 3)
                c["span_mm_candidate"] = span
                c["mcba_span_mm"] = MCBA_SPAN_MM[ax]
                c["span_matches_mcba"] = abs(span - MCBA_SPAN_MM[ax]) <= 0.2
            if mcba and ax in mcba:
                c["vs_mcba"] = "equal" if (lo, hi) == tuple(mcba[ax]) else ("zero" if lo == hi == 0 else "different")
        if "CACCSTOP" in info:
            c["stop_decel_positive"] = info["CACCSTOP"]["i32"] > 0
        out[ax] = c
    return out


def summary(checks: dict[str, dict]) -> str:
    parts = []
    for ax, c in checks.items():
        if "limits_ordered" not in c:
            continue
        state = "limits 0/0" if c["limits_zero"] else ("limits OK" if c["limits_ordered"] else "limits NOT ordered")
        if "span_matches_mcba" in c:
            state += f", span {c['span_mm_candidate']} mm ({'matches' if c['span_matches_mcba'] else 'differs from'} mcba)"
        if "stop_decel_positive" in c:
            state += ", CACCSTOP " + ("> 0" if c["stop_decel_positive"] else "NOT > 0")
        parts.append(f"{ax}: {state}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------------- run
def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> MotorGate2Result:
    res = MotorGate2Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.exchanges = [Exchange("axis-ID query", ID_QUERY.hex(" "))] + [
            Exchange(f"{r.axis} {r.var}", r.frame.hex(" ")) for r in READS]
        res.message = (f"dry run: would open {dev} as Gate 1 rev 2, check readiness on all axes, then read "
                       f"{len(READS)} fixed variables, each once; nothing opened or sent")
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
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
            res.message = ("driver state is not normal (" + "; ".join(res.driver_state_problems)
                           + "). Nothing sent. Power-cycle the instrument, wait 2 minutes, try again")
            return res
        _exchange_all(port, res, monotonic, sleep)
    finally:
        port.close()
    return res


def write_evidence(result: MotorGate2Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate2-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate2",
                                description="Motor Gate 2: read-only check of limits, stop deceleration, speeds and "
                                            "positions. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run the reads (phrase: READ MOTOR LIMITS ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true",
                   help="operator confirms the EVOS has been powered on for at least 2 minutes")
    p.add_argument("--confirm", default="", help="must be exactly: READ MOTOR LIMITS ON COMn")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 2 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.outcome == "dry_run":
        for ex in res.exchanges[:3]:
            print(f"  {ex.what:22s} {ex.request_hex}")
        print(f"  ... {len(res.exchanges) - 3} more fixed reads")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
