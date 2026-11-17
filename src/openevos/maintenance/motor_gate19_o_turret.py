"""Motor Gate 19 rev 2: objective-turret (O) change through the drive's own GOTO_POS (ADR-0052; L4).

Spec: docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md. The ONLY writes this module can send are
constants to axis 6 (O), from OAxis.t variables.cfg:
  ITARGETPOS = n (n = 1..5 only; any other value would never match the sensor and the drive would turn for ever),
  ESTATE = 3 (STATE_3_GOTO_POS: AXISON, turn in the IMOVEPOSITIVE direction until the 3-bit sensor reads n, debounce,
  go past LGOPASTAMOUNT, AXISOFF, ESTATEV 0; no sensor change within LSTUCKDELTA -> ISTATUSFLAGS |= 2, AXISOFF),
  and the abort AXISOFF (TML 0x0002; the vendor's HaltAllMotors frame for O). States 2 and 4 cannot be built.

Rev 2 (after run 1, 2026-11-17: the turret turned 1 -> 5, the opposite of the assumed direction, and an objective
struck the stage frame with the stage at its homed corner X 0, Y 0):
* positions DECREASE in the drive's direction (1 -> 5 -> 4 -> 3 -> 2);
* the stage must be centred over its opening (STAGE_BOX_MM) as well as Z retracted at 0;
* polls use the app's fast reads (~0.15 s per poll, was ~3.4 s);
* Ctrl+C sends AXISOFF at once;
* `from_between`: the turret may start between positions (sensor 0), given the last position it passed.
Completion = ICURRENTPOSHF n, ESTATEV 0, IPENDINGMOVES 0, stuck flag clear, O switched off again. Fault: AXISOFF once.
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
from openevos.maintenance import motor_gate15_y_axisoff as g15
from openevos.maintenance import motor_gate17_z_o_reads as g17
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
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
O_ID = 6
_ITARGETPOS, _ESTATE = 0x03CC, 0x03B1
GOTO_POS = 3
POSITIONS = (1, 2, 3, 4, 5)
STUCK_FLAG = 0x0002
#: this unit's objectives by turret position (calibration tpco; position 1 = 4X confirmed by the operator 2026-11-17)
OBJECTIVES = {1: "4X", 2: "10X", 3: "20X", 4: "40X", 5: "60X"}


def _frame(payload_head: bytes, payload: bytes) -> bytes:
    body = bytes([len(payload) + 4, 0x00, O_ID << 4]) + payload_head + payload
    return body + bytes([sum(body) & 0xFF])


def target_frame(n: int) -> bytes:
    if n not in POSITIONS:
        raise g4.MotorGate4Refused(f"turret position {n} is not 1..5 (the drive would turn for ever)")
    op = 0x2000 | (_ITARGETPOS & 0x1FF)
    return _frame(bytes([op >> 8, op & 0xFF]), n.to_bytes(2, "big"))


_op = 0x2000 | (_ESTATE & 0x1FF)
O_GOTO = _frame(bytes([_op >> 8, _op & 0xFF]), GOTO_POS.to_bytes(2, "big"))
O_AXISOFF = bytes.fromhex("04 00 60 00 02 66")  # vendor HaltAllMotors, O
TARGET_FRAMES = {n: target_frame(n) for n in POSITIONS}
ALLOWED_WRITES = frozenset({*TARGET_FRAMES.values(), O_GOTO, O_AXISOFF})

OR, ZR = g17.READS["O"], g17.READS["Z"]
#: X/Y encoders (Gate 1/2 address): the turret turns only with the stage over its opening
XYR = {ax: g2.Read(ax, aid, "APOS", "LONG", 0x0228, build_read(aid, OP_READ32, 0x0228))
       for ax, aid in (("X", 1), ("Y", 2))}
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in g4.ESTATEV_ALL),
                           *(r.frame for r in OR.values()), *(r.frame for r in ZR.values()),
                           *(r.frame for r in XYR.values())})
#: O APOS is not polled: it stays 0 while O turns (no encoder in APOS; run 1)
POLL_VARS = ("ESTATEV", "ICURRENTPOSHF", "IHF", "ISTATUSFLAGS", "IPENDINGMOVES", "SRL")
#: the stage must sit inside this box (mm) before the turret turns: the middle of X 0..119 and Y 0..78, well away from
#: the homed corner (0, 0), where an objective struck the stage frame on 2026-11-17
STAGE_BOX_MM = {"X": (40.0, 80.0), "Y": (20.0, 60.0)}
COUNTS_PER_MM_XY = 2880 / 1.27
#: slowest plausible turret speed, for the timeout only (power-up CSPD 34.0 ticks/0.8 ms is ~42,500 ticks/s)
SLOWEST_TICKS_S = 10000
POLL_S = 0.05  # each poll is 7 fast reads (~16 ms each on COM3): ~0.15 s per poll
OFF_POLLS = 2  # O must read switched off on this many consecutive polls at the target


def confirm_phrase(device: str, n: int) -> str:
    return f"TURN TURRET TO POSITION {n} ON {device.upper()}"


def steps_between(current: int, n: int) -> int:
    """Positions the turret passes going its one way. On this unit IMOVEPOSITIVE 1 turns so the numbers DECREASE
    (run 1: 1 -> 5): 2 -> 1 is 1 step, 1 -> 2 is 4 (through 5, 4, 3)."""
    return (current - n) % len(POSITIONS)


def path_from(current: int, n: int) -> list[int]:
    """The positions passed, in order, ending at n (decreasing, wrapping 1 -> 5)."""
    return [(current - i - 2) % 5 + 1 for i in range(steps_between(current, n))]


def _v(d: dict) -> int:
    return d.get("i32", d.get("u16"))


def baseline_problems(z: dict, o: dict, n: int, from_between: int | None = None,
                      xy_mm: dict | None = None) -> list[str]:
    p = []
    band = z["LCONVERGEBAND"]
    if z["IINITONCEDONE"] != INIT_DONE_MARKER or z["ESTATEV"] != 0:
        p.append(f"Z not homed and idle (IINITONCEDONE {z['IINITONCEDONE']}, ESTATEV {z['ESTATEV']})")
    if abs(z["APOS"]) > band or abs(z["CPOS"]) > band:
        p.append(f"Z not retracted at 0 (APOS {z['APOS']}, CPOS {z['CPOS']}); the turret turns only with Z at 0")
    if z["MER"]:
        p.append(f"Z MER 0x{z['MER']:04X}")
    for ax, (lo, hi) in STAGE_BOX_MM.items():
        if xy_mm is None or not lo <= xy_mm[ax] <= hi:
            got = None if xy_mm is None else round(xy_mm[ax], 2)
            p.append(f"stage {ax} at {got} mm, outside {lo:g}..{hi:g} mm: centre the stage over its opening first "
                     "(the app's Go to), then close the app")
    for var, want in (("INUMTURRETPOSITIONS", 5), ("IMOVEPOSITIVE", 1), ("LSTUCKDELTA", 76800),
                      ("LTICKSPERGENEVAREV", 76800), ("LGOPASTAMOUNT", 9600), ("IVERSION", 7)):
        if o[var] != want:
            p.append(f"O {var} {o[var]}, Gate 17 read {want}")
    cur = o["ICURRENTPOSHF"]
    if from_between is not None:
        if cur != 0:
            p.append(f"--from-between given, but the turret reads position {cur}, not between positions")
        elif from_between not in POSITIONS or n == from_between:
            p.append(f"--from-between {from_between}: the last position passed must be 1..5 and not the target")
    elif cur not in POSITIONS or g17.O_HF_TABLE.get(o["IHF"]) != cur:
        p.append(f"O position unknown (ICURRENTPOSHF {cur}, IHF {o['IHF']}); if it is between positions, give "
                 "--from-between with the last position it passed")
    elif n == cur:
        p.append(f"the turret is already at position {n}")
    if o["ESTATEV"] != 0 or o["IPENDINGMOVES"] != 0:
        p.append(f"O not idle (ESTATEV {o['ESTATEV']}, IPENDINGMOVES {o['IPENDINGMOVES']})")
    if o["ISTATUSFLAGS"] & STUCK_FLAG:
        p.append(f"O stuck flag already set (ISTATUSFLAGS 0x{o['ISTATUSFLAGS']:04X})")
    if o["MER"]:
        p.append(f"O MER 0x{o['MER']:04X}")
    if o["SRL"] & 0x8000:
        p.append(f"O power stage on at rest (SRL 0x{o['SRL']:04X}); Gate 17 read it off")
    return p


def run(*, n: int, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_z_down: bool = False,
        from_between: int | None = None,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports, open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    if n not in POSITIONS:
        res.message = f"--to must be 1..5, not {n}; nothing opened"
        return res
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would check that the stage is centred, Z is retracted at 0 and every axis is idle, "
                       f"then turn the turret to position {n} ({OBJECTIVES[n]}) via the drive's GOTO_POS (positions "
                       "decrease as it turns); Ctrl+C cuts O's power; nothing opened or sent")
        res.exchanges = [Exchange(f"O ITARGETPOS={n}", TARGET_FRAMES[n].hex(" ")),
                         Exchange("O ESTATE=3 (GOTO_POS)", O_GOTO.hex(" ")),
                         Exchange("O AXISOFF (fault / Ctrl+C only)", O_AXISOFF.hex(" "))]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on (and homed) for >= 2 minutes; nothing opened"
        return res
    if not stage_empty_z_down:
        res.message = ("operator has not confirmed an empty stage, hands clear of the turret, no immersion oil on any "
                       "objective, and watching with a hand on Ctrl+C and near the power switch; nothing opened")
        return res
    if confirm != confirm_phrase(dev, n):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev, n)!r}); nothing opened"
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
        link = g15._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=ALLOWED_READS)
        res.fast_resyncs = []  # type: ignore[attr-defined]  # g15's fast reads log re-syncs here
        _procedure(link, res, n, from_between)
    finally:
        port.close()
    return res


def _procedure(link, res, n: int, from_between: int | None = None) -> None:
    for baud in BAUD_PASSES:
        link.port.configure(baud)
        if link.sync(f"line sync @ {baud}"):
            res.baud = baud
            break
    if res.baud is None:
        res.outcome, res.message = "stopped", "no line sync at either rate; nothing written"
        return
    res.gateway_axis_id = link.query_id()
    if res.gateway_axis_id != 1:
        res.outcome, res.message = "stopped", f"gateway axis ID {res.gateway_axis_id}, not 1; nothing written"
        return
    try:
        for r in g4.READINESS:
            v = link.read(r)
            if (r.var == "ESTATEV" and v["u16"] != 0) or (r.var == "IINITONCEDONE" and v["u16"] != INIT_DONE_MARKER):
                res.outcome, res.message = "not_idle", f"{r.axis} {r.var} = {v['u16']}; nothing written"
                return
        z = {k: _v(link.read(ZR[k])) for k in ("IINITONCEDONE", "ESTATEV", "APOS", "CPOS", "LCONVERGEBAND", "MER")}
        o = {k: _v(link.read(r)) for k, r in OR.items()}
        xy_mm = {ax: _v(link.read(r)) / COUNTS_PER_MM_XY for ax, r in XYR.items()}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline = {"Z": z, "O": o, "XY_mm": xy_mm}
    problems = baseline_problems(z, o, n, from_between, xy_mm)
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    start = o["ICURRENTPOSHF"] or from_between  # between positions: the last one passed
    path = path_from(start, n)
    steps = len(path)
    timeout_s = steps * (o["LTICKSPERGENEVAREV"] + o["LGOPASTAMOUNT"] + o["LDEBOUNCEJUMP"]) / SLOWEST_TICKS_S + 5.0
    trace: list[dict] = []
    res.legs["turn"] = {"from": start, "from_between": from_between is not None, "to": n, "steps": steps,
                        "path": path, "timeout_s": round(timeout_s, 1), "trace": trace}
    seen = [start]
    try:
        link.write(f"O ITARGETPOS={n}", TARGET_FRAMES[n])
        link.write("O ESTATE=3 (GOTO_POS)", O_GOTO)
        t0 = link.monotonic()
        off_at_target = 0
        while True:
            link.sleep(POLL_S)
            s = {k: link.fast(OR[k]) for k in POLL_VARS}
            s["Z_APOS"] = link.fast(ZR["APOS"])
            s["t"] = round(link.monotonic() - t0, 2)
            trace.append(s)
            pos = s["ICURRENTPOSHF"]
            if pos in POSITIONS and pos != seen[-1]:
                if pos not in path or (seen[-1] in path and path.index(pos) < path.index(seen[-1])):
                    raise g4._Fault(f"turret reached position {pos}, not next on its path {start} -> {path}")
                seen.append(pos)
            if s["ISTATUSFLAGS"] & STUCK_FLAG:
                raise g4._Fault(f"the drive flagged the turret stuck (ISTATUSFLAGS 0x{s['ISTATUSFLAGS']:04X}) at "
                                f"position {pos}")
            if abs(s["Z_APOS"] - z["APOS"]) > z["LCONVERGEBAND"]:
                raise g4._Fault(f"Z moved during the turret change ({z['APOS']} -> {s['Z_APOS']})")
            done = (pos == n and seen[-1] == n and s["ESTATEV"] == 0 and s["IPENDINGMOVES"] == 0
                    and not s["SRL"] & 0x8000)
            off_at_target = off_at_target + 1 if done else 0
            if off_at_target >= OFF_POLLS:
                break
            if link.monotonic() - t0 > timeout_s:
                raise g4._Fault(f"turret did not settle at position {n} within {timeout_s:.1f} s (now {pos}, "
                                f"ESTATEV {s['ESTATEV']}, SRL 0x{s['SRL']:04X})")
        res.legs["turn"]["seconds"] = round(link.monotonic() - t0, 2)
        res.legs["turn"]["positions_seen"] = seen
        res.legs["turn"]["fast_resyncs"] = list(res.fast_resyncs)
        fo = {k: _v(link.read(r)) for k, r in OR.items()}
        fz = {k: _v(link.read(ZR[k])) for k in ("ESTATEV", "APOS", "CPOS", "MER")}
        idle = {r.axis: link.read(r)["u16"] for r in g4.ESTATEV_ALL}
    except g4._Fault as f:
        _abort(link, res, str(f))
        return
    except KeyboardInterrupt:  # the operator saw contact: cut O's power at once
        _abort(link, res, "operator pressed Ctrl+C")
        return
    checks = {
        "at_target": fo["ICURRENTPOSHF"] == n and g17.O_HF_TABLE.get(fo["IHF"]) == n,
        "o_idle": fo["ESTATEV"] == 0 and fo["IPENDINGMOVES"] == 0,
        "o_off": not fo["SRL"] & 0x8000,
        "not_stuck": not fo["ISTATUSFLAGS"] & STUCK_FLAG,
        "o_mer_clear": fo["MER"] == 0,
        "z_unchanged": abs(fz["APOS"] - z["APOS"]) <= z["LCONVERGEBAND"] and fz["ESTATEV"] == 0 and fz["MER"] == 0,
        "all_axes_idle": all(v == 0 for v in idle.values()),
    }
    # a position the turret passes quickly can fall between polls: recorded, not judged
    res.legs["turn"]["saw_every_position"] = seen == [start, *path]
    res.final = {"O": fo, "Z": fz, "ESTATEV_all": idle, "checks": checks}
    failed = [k for k, v in checks.items() if not v]
    res.outcome = "fail" if failed else "pass"
    res.message = (f"turret {start}{' (between)' if from_between else ''} -> {n} ({OBJECTIVES[n]}) in "
                   f"{res.legs['turn']['seconds']} s, passing {seen}; O off and idle, not stuck, MER clear; Z unchanged "
                   f"at {fz['APOS']}; all axes idle" if not failed else "FAILED final checks: " + ", ".join(failed))


def _abort(link, res, why: str) -> None:
    """The vendor's stop for O: AXISOFF once (the drive coasts; the Geneva detent holds), then read the state."""
    res.fault, res.outcome = why, "fault_stopped"
    note = "O AXISOFF sent and acknowledged"
    try:
        link.write("FAULT O AXISOFF", O_AXISOFF)
    except g4._Fault as f:
        note = f"O AXISOFF NOT CONFIRMED ({f}); if the turret still turns, switch the instrument off"
    try:
        res.final = {"after_fault": {k: link.fast(OR[k]) for k in POLL_VARS}}
    except g4._Fault as f:
        res.final = {"after_fault_read_error": str(f)}
    res.message = f"FAULT: {why}. {note}. Nothing further sent"


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate19-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate19_o_turret",
                                description="Motor Gate 19 rev 2: turn the objective turret to one position via the "
                                            "drive's GOTO_POS, stage centred, Z retracted at 0. Dry run by default. "
                                            "Ctrl+C during the turn cuts O's power.")
    p.add_argument("--to", type=int, required=True, choices=POSITIONS,
                   help="turret position (1 4X, 2 10X, 3 20X, 4 40X, 5 60X); numbers decrease as it turns")
    p.add_argument("--from-between", type=int, choices=POSITIONS, default=None,
                   help="the turret is between positions (sensor 0): the last position it passed")
    p.add_argument("--send", action="store_true", help="run it (phrase: TURN TURRET TO POSITION n ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on and homed >= 2 minutes")
    p.add_argument("--stage-empty-z-down", action="store_true",
                   help="no vessel/slide/insert on the stage, hands clear of the turret, no immersion oil on any "
                        "objective, operator watching with a hand on Ctrl+C and near the power switch")
    p.add_argument("--confirm", default="")
    p.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = p.parse_args(argv)
    res = run(n=a.to, send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_empty_z_down=a.stage_empty_z_down, from_between=a.from_between)
    print(f"[{res.outcome}] {res.message}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  would write {ex.what:32s} {ex.request_hex}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
