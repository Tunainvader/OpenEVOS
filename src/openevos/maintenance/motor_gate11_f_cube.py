"""Motor Gate 11: move the F carriage (cube changer, axis 5) to one cube slot (ADR-0035; L4 reviewed).

Spec: docs/evidence/fl1_v1.6_motor_gate11_f_cube_spec.md. Uses the drive's own STATE_16_GOTO_CUBE: the PC
writes only the slot number (LTARGETPOS = 1..4) and ESTATE = 16; the drive moves to its stored LCUBEnPOS
(0 / 34,000 / 68,000 / 102,000 counts, read back before moving), clamped to its LPOSMIN/LPOSMAX.
Completion = ISTATUSFLAGS bit 0x4000 (converged) with APOS within LCONVERGEBAND of LCUBEnPOS.
Fault -> F's own STATE_15_STOP (ESTATE = 15). Writes: 4 slot frames, ESTATE 16, ESTATE 15. Dry run by default.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openevos.backends.technosoft import protocol as tp
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate9_drive_status as g9
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    INIT_DONE_MARKER,
    Exchange,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate11_f_cube_spec.md; ADR-0035"
F_ID = 5
FR = g9.STATUS["F"]  # F's reads (status registers + program variables)
_LTARGETPOS, _ESTATE = 0x03B7, 0x03B5
GOTO_CUBE, STOP = 16, 15
CONVERGED = 0x4000
#: the drive's stored slot positions, as read on 2026-11-13; the gate refuses if the drive holds anything else
EXPECTED_CUBE_POS = {1: 0, 2: 34000, 3: 68000, 4: 102000}
SPEED_COUNTS_S_MIN = 5000  # generous lower bound for the timeout (CSPDCMD 20.0 is ~25,000 counts/s)
POLL_S = 0.1


def _frame(op: int, payload: bytes) -> bytes:
    body = bytes([0x06 if len(payload) == 2 else 0x08, 0x00, F_ID << 4, op >> 8, op & 0xFF]) + payload
    return body + bytes([tp.checksum(body)])


def slot_frame(n: int) -> bytes:
    if n not in EXPECTED_CUBE_POS:
        raise g4.MotorGate4Refused(f"no F slot {n}")
    b = n.to_bytes(4, "big")
    return _frame(0x2400 | (_LTARGETPOS & 0x1FF), b[2:4] + b[0:2])


GOTO_FRAME = _frame(0x2000 | (_ESTATE & 0x1FF), GOTO_CUBE.to_bytes(2, "big"))
STOP_FRAME = _frame(0x2000 | (_ESTATE & 0x1FF), STOP.to_bytes(2, "big"))
ALLOWED_WRITES = frozenset({*(slot_frame(n) for n in EXPECTED_CUBE_POS), GOTO_FRAME, STOP_FRAME})
CUBE_NAMES = {1: "RFP", 2: "empty", 3: "DAPI (near-UV)", 4: "GFP"}  # LED Gate 3 (0x47) + user's photo/visual


def confirm_phrase(device: str, slot: int) -> str:
    return f"MOVE F TO CUBE {slot} ON {device.upper()}"


def run(*, slot: int, send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        nobody_looking: bool = False, find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    if slot not in EXPECTED_CUBE_POS:
        res.message = "slot must be 1..4; nothing opened"
        return res
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: F to cube slot {slot} ({CUBE_NAMES[slot]}, drive position "
                       f"{EXPECTED_CUBE_POS[slot]}) via the drive's GOTO_CUBE; nothing opened or sent")
        res.exchanges = [Exchange(f"F LTARGETPOS={slot}", slot_frame(slot).hex(" ")),
                         Exchange("F ESTATE=16 (GOTO_CUBE)", GOTO_FRAME.hex(" "))]
        return res
    if not on_for_2_min or not nobody_looking:
        res.message = ("operator has not confirmed >= 2 min on, and that nobody looks into the light path (the "
                       "DAPI cube is near-UV); nothing opened")
        return res
    if confirm != confirm_phrase(dev, slot):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev, slot)!r}); nothing opened"
        return res
    try:
        port = open_port(dev)
    except Exception as exc:  # noqa: BLE001
        res.message = f"could not open {dev}: {exc}; nothing sent"
        return res
    try:
        res.port_state_read_back = port.inspect()
        if driver_state_problems(res.port_state_read_back):
            res.outcome, res.message = "driver_state", "driver state not normal; nothing sent"
            return res
        reads = {g4.ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in FR.values())}
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=reads)
        _procedure(link, res, slot, monotonic)
    finally:
        port.close()
    return res


def _f(link, name):
    v = link.read(FR[name])
    return v.get("i32", v.get("i16"))


def _procedure(link, res, slot, monotonic) -> None:
    for baud in BAUD_PASSES:
        link.port.configure(baud)
        if link.sync(f"line sync @ {baud}"):
            res.baud = baud
            break
    if res.baud is None or link.query_id() != 1:
        res.outcome, res.message = "stopped", "no link or wrong gateway ID; nothing written"
        return
    try:
        for r in g4.READINESS:
            v = link.read(r)
            if (r.var == "ESTATEV" and v["u16"] != 0) or (r.var == "IINITONCEDONE" and v["u16"] != INIT_DONE_MARKER):
                res.outcome, res.message = "not_idle", f"{r.axis} {r.var} = {v['u16']}; nothing written"
                return
        names = ("ESTATEV", "IINITONCEDONE", "APOS", "CPOS", "LPOSMIN", "LPOSMAX", "LCUBE1POS", "LCUBE2POS",
                 "LCUBE3POS", "LCUBE4POS", "LCONVERGEBAND", "MER")
        base = {n: _f(link, n) for n in names}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline = dict(base)
    target, band = base[f"LCUBE{slot}POS"], base["LCONVERGEBAND"]
    problems = []
    if any(base[f"LCUBE{n}POS"] != p for n, p in EXPECTED_CUBE_POS.items()):
        problems.append("the drive's stored cube positions are not the reviewed 0/34000/68000/102000")
    if not base["LPOSMIN"] <= target <= base["LPOSMAX"]:
        problems.append(f"slot position {target} outside the drive limits")
    if base["APOS"] != base["CPOS"] and abs(base["APOS"] - base["CPOS"]) > band:
        problems.append(f"F not at rest (APOS {base['APOS']}, CPOS {base['CPOS']})")
    if base["MER"] & 0xFFFF:
        problems.append(f"F MER 0x{base['MER'] & 0xFFFF:04X} not clear")
    if not 0 < band <= 50:
        problems.append(f"LCONVERGEBAND {band} not sane")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    start = base["APOS"]
    trace = []
    res.legs["goto"] = {"slot": slot, "target": target, "start": start, "trace": trace}
    try:
        link.write(f"F LTARGETPOS={slot}", slot_frame(slot))
        link.write("F ESTATE=16 (GOTO_CUBE)", GOTO_FRAME)
        t0 = monotonic()
        deadline = t0 + abs(target - start) / SPEED_COUNTS_S_MIN + 5.0
        while True:
            link.sleep(POLL_S)
            flags, apos = _f(link, "ISTATUSFLAGS") & 0xFFFF, _f(link, "APOS")
            trace.append({"t": round(monotonic() - t0, 2), "APOS": apos, "flags": f"0x{flags:04X}"})
            if abs(target - apos) > abs(target - start) + 200:
                raise g4._Fault(f"F moved away from slot {slot} ({start} -> {apos}, target {target})")
            if flags & CONVERGED and abs(apos - target) <= band:
                break
            if monotonic() > deadline:
                raise g4._Fault(f"F did not reach slot {slot} in time (APOS {apos}, target {target})")
    except g4._Fault as f:
        res.fault, res.outcome = str(f), "fault_stopped"
        try:
            link.write("FAULT F ESTATE=15 (STOP)", STOP_FRAME)
            note = "F stop sent and acknowledged"
        except g4._Fault as g:
            note = f"F STOP NOT COMPLETED ({g}); switch the instrument off if anything still moves"
        res.message = f"FAULT: {f}. {note}"
        return
    res.final = {n: _f(link, n) for n in ("ESTATEV", "APOS", "CPOS", "ISTATUSFLAGS", "MER")}
    res.legs["goto"]["seconds"] = round(monotonic() - t0, 2)
    ok = res.final["ESTATEV"] == 0 and abs(res.final["APOS"] - target) <= band and not res.final["MER"] & 0xFFFF
    res.outcome = "pass" if ok else "fail"
    res.message = (f"F at cube slot {slot} ({CUBE_NAMES[slot]}): APOS {res.final['APOS']} (target {target}) in "
                   f"{res.legs['goto']['seconds']} s; idle" if ok else f"FAILED final checks: {res.final}")


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate11-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate11_f_cube",
                                 description="Motor Gate 11: move the F carriage to one cube slot. Dry run by default.")
    ap.add_argument("--slot", type=int, required=True, choices=(1, 2, 3, 4))
    ap.add_argument("--send", action="store_true", help="run it (phrase: MOVE F TO CUBE n ON COMn)")
    ap.add_argument("--instrument-on-2min", action="store_true")
    ap.add_argument("--nobody-looking", action="store_true",
                    help="nobody looks into the light path (DAPI is near-UV)")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = ap.parse_args(argv)
    res = run(slot=a.slot, send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              nobody_looking=a.nobody_looking)
    print(f"[{res.outcome}] {res.message}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
