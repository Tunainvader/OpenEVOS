"""Motor Gate 9: read-only drive status / error registers on X, Y and Z (ADR-0030; L3 reads only).

Spec: docs/evidence/fl1_v1.6_motor_gate9_drive_status_spec.md. Why: on 2026-11-13 Y twice ended a move to 0
on its own near 73.5 mm (CPOS = APOS short of the target, then idle) and no reviewed read could say why.
Addresses are identical in the X, Y and Z `variables.cfg` (Verified): MER 0x08FC, SRL 0x090E, SRH 0x090F,
MSR 0x0308, ISR 0x0306, INSTATUS 0x0908, POSERR 0x022A, ERRMAX 0x02C5, TERRMAX 0x02C6 (all 16-bit).
On X/Y it also reads the drive program's own variables (ISTATUSFLAGS, IOPTIONFLAGS, IIQMAX, IIQMIN, IMAXPROT,
IQ, IOSCCOUNT, BDOAXISOFF, LTARGETPOSV; rev 2). Only opcodes 0xB004/0xB005 (reads) are built here; this
module has NO writes. Gate 8 reuses `snapshot`.
Dry run by default.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openevos.maintenance import motor_gate2 as g2
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    Exchange,
    Port,
    PortInfo,
    build_read,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate9_drive_status_spec.md; ADR-0030"
OP_READ16 = 0xB004
#: (variable, address); identical on X, Y and Z (each axis' variables.cfg, Verified)
STATUS_VARS = (("MER", 0x08FC), ("SRL", 0x090E), ("SRH", 0x090F), ("MSR", 0x0308), ("ISR", 0x0306),
               ("INSTATUS", 0x0908), ("POSERR", 0x022A), ("ERRMAX", 0x02C5), ("TERRMAX", 0x02C6))
AXIS_IDS = {"X": 1, "Y": 2, "Z": 4, "F": 5}  # Gate 2's verified axis IDs
#: F (cube carriage) drive-program variables, from FAxis variables.cfg (Verified addresses; rev 3, 2026-11-13).
#: STATE_16_GOTO_CUBE moves to LCUBEnPOS (n = LTARGETPOS); completion = ISTATUSFLAGS 0x4000 (converged).
F_PROGRAM_VARS = (("ESTATEV", "INT", 0x03B6), ("IINITONCEDONE", "INT", 0x03F8), ("ISTATUSFLAGS", "INT", 0x03D2),
                  ("APOS", "LONG", 0x0228), ("CPOS", "LONG", 0x029E), ("LTARGETPOS", "LONG", 0x03B7),
                  ("LTARGETPOSV", "LONG", 0x03DF), ("LPOSMIN", "LONG", 0x03EA), ("LPOSMAX", "LONG", 0x03EC),
                  ("LCUBE1POS", "LONG", 0x03EE), ("LCUBE2POS", "LONG", 0x03F0), ("LCUBE3POS", "LONG", 0x03F2),
                  ("LCUBE4POS", "LONG", 0x03F4), ("LMIRROR1POS", "LONG", 0x03F6), ("CSPDCMD", "LONG", 0x03BE),
                  ("CACCCMD", "LONG", 0x03C0), ("LCONVERGEBAND", "LONG", 0x03E1), ("IAXOFFTIMEOUT", "INT", 0x03C2))

#: Bit names from the Technosoft register descriptions (Probable: vendor manual, not yet verified on this drive).
#: Raw values are always recorded; these names are only a reading aid.
MER_BITS = {0: "CAN bus error", 1: "short-circuit", 2: "invalid setup data", 3: "control error (position/speed)",
            4: "serial communication error", 5: "position wrap-around", 6: "LSP limit switch active",
            7: "LSN limit switch active", 8: "over current", 9: "I2t protection", 10: "motor over temperature",
            11: "drive over temperature", 12: "over voltage", 13: "under voltage", 14: "command error",
            15: "ENABLE input inactive"}
SRH_BITS = {6: "LSP event (positive limit switch)", 7: "LSN event (negative limit switch)",
            9: "target reached / not moving", 15: "fault"}
SRL_BITS = {10: "motion complete", 15: "axis on (power stage enabled)"}


OP_READ32 = 0xB005
#: X/Y drive-program (TML user) variables, identical in the X and Y variables.cfg (Verified); NOT valid on Z.
#: The Y program's over-current handler (_INT2_ISR, entered when |IQ| > IMAXPROT) freezes the target, turns
#: the power stage off, clears MER bit 8 and sets ESTATEV 0; IIQMAX/IIQMIN hold the peak current since the
#: last move started and survive a stop (offline decode, 2026-11-13).
PROGRAM_VARS = (("ISTATUSFLAGS", "INT", 0x03B2), ("IOPTIONFLAGS", "INT", 0x03EE), ("IIQMAX", "INT", 0x03BA),
                ("IIQMIN", "INT", 0x03BB), ("IMAXPROT", "INT", 0x0297), ("IQ", "INT", 0x0230),
                ("IOSCCOUNT", "INT", 0x03EB), ("BDOAXISOFF", "INT", 0x03D1), ("LTARGETPOSV", "LONG", 0x03B8))
PROGRAM_AXES = ("X", "Y")


def status_reads(axis: str) -> dict[str, g2.Read]:
    aid = AXIS_IDS[axis]
    reads = {v: g2.Read(axis, aid, v, "INT", a, build_read(aid, OP_READ16, a)) for v, a in STATUS_VARS}
    extra = PROGRAM_VARS if axis in PROGRAM_AXES else F_PROGRAM_VARS if axis == "F" else ()
    reads |= {v: g2.Read(axis, aid, v, t, a, build_read(aid, OP_READ16 if t == "INT" else OP_READ32, a))
              for v, t, a in extra}
    return reads


STATUS = {ax: status_reads(ax) for ax in AXIS_IDS}
STATUS_FRAMES = frozenset(r.frame for reads in STATUS.values() for r in reads.values())


def confirm_phrase(device: str) -> str:
    return f"READ DRIVE STATUS ON {device.upper()}"


def bits(value: int, names: dict[int, str]) -> list[str]:
    return [names.get(b, f"bit {b}") for b in range(16) if value >> b & 1]


def decode(values: dict[str, dict]) -> dict:
    """Readable summary of one axis' status reads (raw values stay in the evidence)."""
    u = {k: v["u16"] for k, v in values.items() if "u16" in v}
    program = {}
    if "IMAXPROT" in values:
        peak = max(values["IIQMAX"]["i16"], -values["IIQMIN"]["i16"])
        program = {
            "ISTATUSFLAGS": f"0x{u['ISTATUSFLAGS']:04X}", "IOPTIONFLAGS": f"0x{u['IOPTIONFLAGS']:04X}",
            "IIQMAX": values["IIQMAX"]["i16"], "IIQMIN": values["IIQMIN"]["i16"], "IMAXPROT": values["IMAXPROT"]["i16"],
            "IQ": values["IQ"]["i16"], "IOSCCOUNT": values["IOSCCOUNT"]["i16"], "BDOAXISOFF": u["BDOAXISOFF"],
            "LTARGETPOSV": values["LTARGETPOSV"]["i32"],
            "peak_current_reached_limit": peak > values["IMAXPROT"]["i16"],
        }
    if "LCUBE1POS" in values:  # F: report its program variables as plain numbers
        program = {k: v.get("i32", v.get("i16")) for k, v in values.items() if k not in dict(STATUS_VARS)}
        program["CSPDCMD"] = round(values["CSPDCMD"]["i32"] / 65536, 4) if "i32" in values["CSPDCMD"] else None
        program["ISTATUSFLAGS"] = f"0x{u['ISTATUSFLAGS']:04X}"
    return program | {
        "MER": f"0x{u['MER']:04X}", "MER_set": bits(u["MER"], MER_BITS),
        "SRH": f"0x{u['SRH']:04X}", "SRH_set": [SRH_BITS[b] for b in SRH_BITS if u["SRH"] >> b & 1],
        "SRL": f"0x{u['SRL']:04X}", "SRL_set": [SRL_BITS[b] for b in SRL_BITS if u["SRL"] >> b & 1],
        "MSR": f"0x{u['MSR']:04X}", "ISR": f"0x{u['ISR']:04X}", "INSTATUS": f"0x{u['INSTATUS']:04X}",
        "POSERR": values["POSERR"]["i16"], "ERRMAX": u["ERRMAX"], "TERRMAX": u["TERRMAX"],
    }


def snapshot(link: g4._Link, axes=("X", "Y")) -> dict:
    """Read-only status capture through an open, allowlisted Gate 4 link (STATUS_FRAMES must be allowed)."""
    out = {}
    for ax in axes:
        vals = {v: link.read(r) for v, r in STATUS[ax].items()}
        out[ax] = {"raw": {k: v["raw"] for k, v in vals.items()}, "decoded": decode(vals)}
    return out


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: {len(STATUS_FRAMES)} read-only status reads on X, Y, Z (plus X/Y drive-program "
                       "variables); nothing opened or sent")
        res.exchanges = [Exchange(f"{r.axis} {r.var}", r.frame.hex(" ")) for reads in STATUS.values()
                         for r in reads.values()]
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
            res.outcome, res.message = "driver_state", "driver state not normal; nothing sent"
            return res
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=frozenset(),
                        allowed_reads={g4.ID_QUERY, *STATUS_FRAMES})
        for baud in BAUD_PASSES:
            link.port.configure(baud)
            if link.sync(f"line sync @ {baud}"):
                res.baud = baud
                break
        if res.baud is None or link.query_id() != 1:
            res.outcome, res.message = "stopped", "no link or wrong gateway ID"
            return res
        try:
            res.baseline = {"status": snapshot(link, tuple(AXIS_IDS))}
        except g4._Fault as f:
            res.outcome, res.message = "fail", f"status reads failed: {f}"
            return res
        res.outcome = "complete"
        res.message = "; ".join(_summary(ax, d["decoded"]) for ax, d in res.baseline["status"].items())
    finally:
        port.close()
    return res


def _summary(ax: str, d: dict) -> str:
    s = (f"{ax}: MER {d['MER']}{' (' + ', '.join(d['MER_set']) + ')' if d['MER_set'] else ''}"
         f", SRH {d['SRH']}, SRL {d['SRL']}, POSERR {d['POSERR']}")
    if "IMAXPROT" in d:
        s += (f", ISTATUSFLAGS {d['ISTATUSFLAGS']}, current peak {d['IIQMAX']}/{d['IIQMIN']} vs limit "
              f"{d['IMAXPROT']}{' (LIMIT REACHED)' if d['peak_current_reached_limit'] else ''}")
    return s


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate9-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate9_drive_status",
                                 description="Motor Gate 9: read-only drive status/error registers on X, Y, Z. "
                                             "Dry run by default.")
    ap.add_argument("--send", action="store_true", help="run the reads (phrase: READ DRIVE STATUS ON COMn)")
    ap.add_argument("--instrument-on-2min", action="store_true",
                    help="operator confirms the EVOS has been powered on for at least 2 minutes")
    ap.add_argument("--confirm", default="", help="must be exactly: READ DRIVE STATUS ON COMn")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = ap.parse_args(argv)
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("complete", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
