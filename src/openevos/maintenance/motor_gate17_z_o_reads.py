"""Motor Gate 17: read-only Z (focus) and O (objective turret) program variables (ADR-0052; L1, reads only).

Spec: docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md. Z (axis 4) and O (axis 6) have only ever been
read for their state and position; the variables Gates 18/19 depend on (Z's band, limits and homing result; O's
position sensor, target and tick constants) have never been read on the unit. Addresses: ZAxis.t / OAxis.t
variables.cfg. Expected values: offline decode of ZAxis.sw / OAxis.sw (VAR_INIT / INIT constants).

Each variable is read REPEATS times, REPEAT_GAP_S apart. This module builds only 0xB004 / 0xB005 reads (Gate 1
`build_read`); it has NO writes. Dry run by default.
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
from openevos.maintenance import motor_gate15_y_axisoff as g15
from openevos.maintenance.motor_gate1 import (
    ID_QUERY,
    INIT_DONE_MARKER,
    OP_READ16,
    OP_READ32,
    Exchange,
    Port,
    PortInfo,
    build_read,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md; ADR-0052"
AXIS_IDS = {"Z": 4, "O": 6}
#: common to every axis (Gate 1/2/9 addresses)
COMMON = (("APOS", "LONG", 0x0228), ("CPOS", "LONG", 0x029E), ("SRL", "INT", 0x090E), ("SRH", "INT", 0x090F),
          ("MER", "INT", 0x08FC))
#: ZAxis.t variables.cfg (Verified addresses)
Z_VARS = (("HOMEDONE", "INT", 0x03B0), ("ESTATEV", "INT", 0x03B2), ("CPOSCMD", "LONG", 0x03B3),
          ("CSPDCMD", "LONG", 0x03B5), ("CACCCMD", "LONG", 0x03B7), ("CACCSTOP", "LONG", 0x03BB),
          ("LPOSMIN", "LONG", 0x03BD), ("LPOSMAX", "LONG", 0x03BF), ("IIQMAX", "INT", 0x03C4),
          ("IIQMIN", "INT", 0x03C5), ("IPENDINGMOVES", "INT", 0x03C8), ("LCONVERGEBAND", "LONG", 0x03D3),
          ("BDOAXISOFF", "INT", 0x03D7), ("LTARGETPOSV", "LONG", 0x03DB), ("IVERSION", "INT", 0x03E2),
          ("LHOMEINDEXDELTA", "LONG", 0x03E8), ("ISTATUSFLAGS", "INT", 0x03F5), ("IINITONCEDONE", "INT", 0x03F6))
#: OAxis.t variables.cfg (Verified addresses)
O_VARS = (("ESTATEV", "INT", 0x03B2), ("INUMTURRETPOSITIONS", "INT", 0x03C9), ("IHF", "INT", 0x03CA),
          ("ICURRENTPOSHF", "INT", 0x03CB), ("ITARGETPOS", "INT", 0x03CC), ("IMOVEPOSITIVE", "INT", 0x03CD),
          ("LGOPASTAMOUNT", "LONG", 0x03CE), ("LDEBOUNCEJUMP", "LONG", 0x03D0), ("LTICKSPERGENEVAREV", "LONG", 0x03D2),
          ("LTICKSPERTURRETREV", "LONG", 0x03D4), ("IPENDINGMOVES", "INT", 0x03DE), ("LSTUCKDELTA", "LONG", 0x03E0),
          ("ISTATUSFLAGS", "INT", 0x03E4), ("IVERSION", "INT", 0x03E8))
SIGNED16 = frozenset({"IIQMAX", "IIQMIN"})

#: decoded constants (ZAxis.sw VAR_INIT @0x401C; OAxis.sw INIT @0x402A); a mismatch blocks Gates 18/19
EXPECTED = {
    "Z": {"IVERSION": 21, "LCONVERGEBAND": 5, "LPOSMAX": 67000, "CSPDCMD": 10 * 65536, "CACCCMD": 65536 // 4,
          "CACCSTOP": 65536},
    "O": {"IVERSION": 7, "INUMTURRETPOSITIONS": 5, "IMOVEPOSITIVE": 1, "LGOPASTAMOUNT": 9600, "LDEBOUNCEJUMP": 1000,
          "LTICKSPERGENEVAREV": 76800, "LTICKSPERTURRETREV": 384000, "LSTUCKDELTA": 76800},
}
#: O's UPDATEHF table: raw sensor code IHF -> position (anything else = 0, unknown)
O_HF_TABLE = {4: 1, 0: 2, 2: 3, 10: 4, 12: 5}
REPEATS = 3
REPEAT_GAP_S = 1.0


def _r(axis: str, var: str, vtype: str, address: int) -> g2.Read:
    aid = AXIS_IDS[axis]
    return g2.Read(axis, aid, var, vtype, address, build_read(aid, OP_READ16 if vtype == "INT" else OP_READ32, address))


READS = {ax: {v: _r(ax, v, t, a) for v, t, a in (*COMMON, *(Z_VARS if ax == "Z" else O_VARS))} for ax in AXIS_IDS}
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS),
                           *(r.frame for rs in READS.values() for r in rs.values())})


def confirm_phrase(device: str) -> str:
    return f"READ Z AND TURRET ON {device.upper()}"


def _value(r: g2.Read, d: dict) -> int:
    if "i32" in d:
        return d["i32"]
    return d["i16"] if r.var in SIGNED16 else d["u16"]


def evaluate(samples: dict[str, list[dict[str, int]]]) -> tuple[list[str], list[str]]:
    """(problems, notes). Problems block Gates 18/19; notes are recorded only."""
    problems, notes = [], []
    for ax, rows in samples.items():
        for var in rows[0]:
            # positions may dither a count on a powered axis; they are judged against the band below
            if len({row[var] for row in rows}) > 1 and var not in ("IIQMAX", "IIQMIN", "APOS", "CPOS"):
                problems.append(f"{ax} {var} changed between reads: {[row[var] for row in rows]}")
    z, o = samples["Z"][-1], samples["O"][-1]
    for ax, last in (("Z", z), ("O", o)):
        for var, want in EXPECTED[ax].items():
            if last[var] != want:
                problems.append(f"{ax} {var} = {last[var]}, decoded {want}")
        if last["MER"]:
            problems.append(f"{ax} MER 0x{last['MER']:04X} not clear")
        if last["ESTATEV"] != 0:
            problems.append(f"{ax} not idle (ESTATEV {last['ESTATEV']})")
    if z["IINITONCEDONE"] != INIT_DONE_MARKER:
        problems.append(f"Z not homed (IINITONCEDONE {z['IINITONCEDONE']})")
    if z["LPOSMIN"] != -z["LHOMEINDEXDELTA"]:
        problems.append(f"Z LPOSMIN {z['LPOSMIN']} is not -LHOMEINDEXDELTA ({z['LHOMEINDEXDELTA']})")
    if not -2500 <= z["LPOSMIN"] < 0:
        problems.append(f"Z LPOSMIN {z['LPOSMIN']} outside the power-up scatter seen by Gate 2 (about -1,780)")
    if abs(z["APOS"]) > z["LCONVERGEBAND"] or abs(z["APOS"] - z["CPOS"]) > z["LCONVERGEBAND"]:
        problems.append(f"Z not at rest at home (APOS {z['APOS']}, CPOS {z['CPOS']})")
    if not z["SRL"] & 0x8000:
        problems.append(f"Z power stage off (SRL 0x{z['SRL']:04X}); the decode says Z stays on at rest")
    if o["ICURRENTPOSHF"] not in range(1, 6):
        problems.append(f"O position sensor reads {o['ICURRENTPOSHF']} (0 = between positions / unknown)")
    elif O_HF_TABLE.get(o["IHF"]) != o["ICURRENTPOSHF"]:
        problems.append(f"O IHF {o['IHF']} does not decode to ICURRENTPOSHF {o['ICURRENTPOSHF']}")
    if o["IPENDINGMOVES"] != 0:
        problems.append(f"O IPENDINGMOVES {o['IPENDINGMOVES']} (a move is pending)")
    if o["SRL"] & 0x8000:
        notes.append(f"O power stage ON at rest (SRL 0x{o['SRL']:04X}); the decode says O is off between moves")
    notes.append(f"Z: LHOMEINDEXDELTA {z['LHOMEINDEXDELTA']}, HOMEDONE {z['HOMEDONE']}, ISTATUSFLAGS "
                 f"0x{z['ISTATUSFLAGS']:04X}, IPENDINGMOVES {z['IPENDINGMOVES']}, peak current {z['IIQMAX']}/{z['IIQMIN']}")  # noqa: E501
    notes.append(f"O: objective position {o['ICURRENTPOSHF']} (IHF {o['IHF']}), ITARGETPOS {o['ITARGETPOS']}, "
                 f"ISTATUSFLAGS 0x{o['ISTATUSFLAGS']:04X}, APOS {o['APOS']}")
    return problems, notes


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports, open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g15.MotorGate15Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                                platform.node(), spec=SPEC)
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        n = sum(len(rs) for rs in READS.values())
        res.message = f"dry run: {n} read-only variables on Z and O, {REPEATS} times each; nothing opened or sent"
        res.exchanges = [Exchange(f"{r.axis} {r.var}", r.frame.hex(" ")) for rs in READS.values() for r in rs.values()]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on (and homed) for >= 2 minutes; nothing opened"  # noqa: E501
        return res
    if confirm != confirm_phrase(dev):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev)!r}); nothing opened"
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
        link = g15._Link(port, res, monotonic, sleep, allowed_writes=frozenset(), allowed_reads=ALLOWED_READS)
        if not g15._connect(link, res):
            return res
        samples: dict[str, list[dict[str, int]]] = {ax: [] for ax in AXIS_IDS}
        try:
            res.baseline = {"readiness": {f"{r.axis} {r.var}": link.read(r)["u16"] for r in g4.READINESS}}
            for i in range(REPEATS):
                if i:
                    link.sleep(REPEAT_GAP_S)
                for ax, rs in READS.items():
                    samples[ax].append({v: _value(r, link.read(r)) for v, r in rs.items()})
        except g4._Fault as f:
            res.outcome, res.message = "stopped", f"read failed: {f}"
            res.final = {"samples": samples}
            return res
        problems, notes = evaluate(samples)
        res.final = {"samples": samples, "problems": problems, "notes": notes, "expected": EXPECTED}
        res.outcome = "pass" if not problems else "fail"
        res.message = ("all decoded constants confirmed; Z homed at rest; O at position "
                       f"{samples['O'][-1]['ICURRENTPOSHF']}" if not problems else "; ".join(problems))
    finally:
        port.close()
    return res


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate17-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate17_z_o_reads",
                                 description="Motor Gate 17: read-only Z (focus) and O (objective turret) variables. "
                                             "Dry run by default.")
    ap.add_argument("--send", action="store_true", help="run the reads (phrase: READ Z AND TURRET ON COMn)")
    ap.add_argument("--instrument-on-2min", action="store_true",
                    help="operator confirms the EVOS has been on and homed for at least 2 minutes")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = ap.parse_args(argv)
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min)
    print(f"[{res.outcome}] {res.message}")
    for n in (res.final or {}).get("notes", ()):
        print(f"  {n}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
