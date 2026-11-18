"""Motor Gate 20: re-home X and Y from software with the vendor's own home request, one axis at a time (ADR-0058; L4).

Spec: docs/evidence/fl1_v1.6_xy_software_rehome_research.md §8 (option A, approved by the operator 2026-11-19).
Transport as Motor Gate 1 rev 2 (Gate 4's link). The ONLY writes this module can send are two constants:
  H1 X ESTATE = 19  06 00 10 21 B3 00 13 FD
  H2 Y ESTATE = 19  06 00 20 21 B3 00 13 0D
ESTATE 19 is the drive program's STATE_19_HOME_DOUBLE, which calls the same _HOME_DOUBLE / _HOME_LSN_DOUBLE routine
INIT_ONCE runs at power-up (XAxis.sw / YAxis.sw, identical code), and is what PearlScope's XYStageHome{X,Y}Axis sends
(MotorController64.dll, byte-exact). RAM only: no variable, limit, speed, EEPROM, setup or firmware write.

While an axis homes, the drive's main loop is inside the homing call, so the controlled stop (ESTATE 6) would not be
read: this gate sends NO stop and no other frame. On any fault or timeout it only reads the state and reports;
recovery is the operator's power switch / a power cycle. X first; Y only after X passed. Dry run by default.
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
from openevos.maintenance import motor_gate9_drive_status as g9
from openevos.maintenance import motor_gate17_z_o_reads as g17
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
    Exchange,
    Port,
    PortInfo,
    build_read,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_xy_software_rehome_research.md §8; ADR-0058"
AXIS_IDS = {"X": 1, "Y": 2}
_ESTATE = 0x03B3  # same on X and Y (variables.cfg identical)
HOME_STATE = 19  # STATE_19_HOME_DOUBLE


def home_frame(axis: str) -> bytes:
    if axis not in AXIS_IDS:
        raise g4.MotorGate4Refused(f"Gate 20 homes only X or Y, not {axis!r}")
    body = bytes([0x06, 0x00, AXIS_IDS[axis] << 4, 0x20 | ((_ESTATE >> 8) & 0x01), _ESTATE & 0xFF, 0x00, HOME_STATE])
    return body + bytes([sum(body) & 0xFF])


H1_X_HOME = home_frame("X")
H2_Y_HOME = home_frame("Y")
assert H1_X_HOME == bytes.fromhex("06 00 10 21 B3 00 13 FD")  # the vendor's own bytes (VA 0x18003995F)
assert H2_Y_HOME == bytes.fromhex("06 00 20 21 B3 00 13 0D")  # (VA 0x180039B1F)
HOME_FRAMES = {"X": H1_X_HOME, "Y": H2_Y_HOME}
ALLOWED_WRITES = frozenset(HOME_FRAMES.values())


def _rd(axis: str, var: str, vtype: str, address: int) -> g2.Read:
    aid = AXIS_IDS[axis]
    return g2.Read(axis, aid, var, vtype, address, build_read(aid, 0xB004 if vtype == "INT" else 0xB005, address))


def axis_reads(axis: str) -> dict[str, g2.Read]:
    """Gate 4's X/Y reads, Gate 9's status and program reads, and the two homing results (HOMEDONE, LHOMEINDEXDELTA:
    read on the unit by the power-up record, ADR-0040 §9)."""
    return {**g4.axis_reads(axis, AXIS_IDS[axis]), **g9.status_reads(axis),
            "HOMEDONE": _rd(axis, "HOMEDONE", "INT", 0x03B0),
            "LHOMEINDEXDELTA": _rd(axis, "LHOMEINDEXDELTA", "LONG", 0x03F4)}


READS = {ax: axis_reads(ax) for ax in AXIS_IDS}
ZR = g17.READS["Z"]
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in g4.ESTATEV_ALL),
                           *(r.frame for rs in READS.values() for r in rs.values()),
                           ZR["APOS"].frame, ZR["CPOS"].frame, ZR["ESTATEV"].frame})

BASELINE = ("IINITONCEDONE", "ESTATEV", "HOMEDONE", "ISTATUSFLAGS", "SRL", "SRH", "MER", "APOS", "CPOS", "LPOSMIN",
            "LPOSMAX", "CSPDCMD", "IIQMAX", "IIQMIN", "LHOMEINDEXDELTA", "BDOAXISOFF")
POLL = ("ESTATEV", "HOMEDONE", "APOS", "SRH")
FINAL = ("IINITONCEDONE", "ESTATEV", "HOMEDONE", "ISTATUSFLAGS", "SRL", "SRH", "MER", "APOS", "CPOS", "LPOSMIN",
         "LPOSMAX", "CSPDCMD", "IIQMAX", "IIQMIN", "LHOMEINDEXDELTA")
#: ISTATUSFLAGS bit 0 = over-current, bit 3 = homing interrupted (XAxis.sw _HOME_OVERCURRENT / _H30_INTERRUPTED)
ISF_OVERCURRENT, ISF_HOME_INTERRUPTED = 0x0001, 0x0008
SRH_FAULT = 1 << 15
HOME_BAND = 5  # |APOS|, |CPOS| at the end of homing
Z_HOME_BAND = 5
START_TIMEOUT_S = 3.0  # the drive must show it took the request (ESTATEV 19 or HOMEDONE bit 0 cleared)
HOME_TIMEOUT_S = 120.0  # first run sets the real figure (research §9 Q1)
POLL_S = 0.2
SETTLE_S = 0.5  # after HOMEDONE is set, before the final reads


def confirm_phrase(device: str, axes: tuple[str, ...] = ("X", "Y")) -> str:
    return f"REHOME {' AND '.join(axes)} ON {device.upper()}"


def _i(v: dict) -> int | None:
    return v.get("i32", v.get("i16", v.get("u16")))


def baseline_problems(b: dict[str, dict], z: dict) -> list[str]:
    """Reasons not to send a home request. `b` = {axis: {var: decoded}} for X and Y; `z` = Z APOS/CPOS/ESTATEV."""
    p = []
    for ax, v in b.items():
        if v["ESTATEV"]["u16"] != 0:
            p.append(f"{ax} not idle (ESTATEV {v['ESTATEV']['u16']})")
        if v["MER"]["u16"]:
            p.append(f"{ax} MER 0x{v['MER']['u16']:04X}: a latched drive fault a software home cannot clear; "
                     "power-cycle instead")
        if v["SRH"]["u16"] & SRH_FAULT:
            p.append(f"{ax} SRH fault bit set (0x{v['SRH']['u16']:04X}); power-cycle instead")
        if not v["SRH"]["u16"] & g4.SRH_NOT_MOVING_BIT:
            p.append(f"{ax} is moving (SRH 0x{v['SRH']['u16']:04X})")
    if z["ESTATEV"]["u16"] != 0:
        p.append(f"Z not idle (ESTATEV {z['ESTATEV']['u16']})")
    if abs(z["APOS"]["i32"]) > Z_HOME_BAND or abs(z["CPOS"]["i32"]) > Z_HOME_BAND:
        p.append(f"Z not at home (APOS {z['APOS']['i32']}, CPOS {z['CPOS']['i32']}): lower the objective to 0 first")
    return p


def final_checks(fin: dict, base: dict) -> dict[str, bool]:
    isf = fin["ISTATUSFLAGS"]["u16"]
    return {
        "idle": fin["ESTATEV"]["u16"] == 0,
        "homedone": bool(fin["HOMEDONE"]["u16"] & 1),
        "no_overcurrent": not isf & ISF_OVERCURRENT,
        "not_interrupted": not isf & ISF_HOME_INTERRUPTED,
        "mer_clear": fin["MER"]["u16"] == 0,
        "no_srh_fault": not fin["SRH"]["u16"] & SRH_FAULT,
        "at_home": abs(fin["APOS"]["i32"]) <= HOME_BAND and abs(fin["CPOS"]["i32"]) <= HOME_BAND,
        "still_initialised": fin["IINITONCEDONE"]["u16"] == INIT_DONE_MARKER,
        "limits_unchanged": (fin["LPOSMIN"]["i32"], fin["LPOSMAX"]["i32"])
                            == (base["LPOSMIN"]["i32"], base["LPOSMAX"]["i32"]),
        "speed_unchanged": fin["CSPDCMD"]["i32"] == base["CSPDCMD"]["i32"],
    }


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_clear_watching: bool = False,
        axes: tuple[str, ...] = ("X", "Y"),
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports, open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    if not axes or len(set(axes)) != len(axes) or any(a not in AXIS_IDS for a in axes) or list(axes) != sorted(axes):
        res.message = f"axes must be X, Y or X then Y, not {axes!r}; nothing opened"
        return res
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = ("dry run: would check readiness, X/Y faults and that Z is at home, then for "
                       + " then ".join(axes) + f": send ESTATE {HOME_STATE} (the drive's double home, as at power-up), "
                       f"poll until it is homed (<= {HOME_TIMEOUT_S:g} s) and verify; nothing opened or sent")
        res.exchanges = [Exchange(f"H {ax} ESTATE={HOME_STATE}", HOME_FRAMES[ax].hex(" ")) for ax in axes]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on (and homed) for >= 2 minutes; nothing opened"  # noqa: E501
        return res
    if not stage_clear_watching:
        res.message = ("operator has not confirmed a clear stage travel to the home corner, hands clear, the OpenEVOS "
                       "app closed, and watching the stage with a hand near the power switch; nothing opened")
        return res
    if confirm != confirm_phrase(dev, axes):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev, axes)!r}); nothing opened"
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
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=ALLOWED_READS)
        _procedure(link, res, axes)
    finally:
        port.close()
    return res


def _read_all(link: g4._Link, axis: str, names) -> dict:
    return {n: link.read(READS[axis][n]) for n in names}


def _home_axis(link: g4._Link, res, axis: str) -> None:
    """Send one home request and follow it: started (ESTATEV 19 or HOMEDONE bit 0 cleared), then done (ESTATEV 0,
    HOMEDONE bit 0 set). Raises g4._Fault on anything else; nothing more is sent either way."""
    leg: dict = {"trace": []}
    res.legs[axis] = leg
    link.write(f"H {axis} ESTATE={HOME_STATE}", HOME_FRAMES[axis])
    t0 = link.monotonic()
    started = False
    while True:
        p = _read_all(link, axis, POLL)
        t = round(link.monotonic() - t0, 3)
        est, hd = p["ESTATEV"]["u16"], p["HOMEDONE"]["u16"]
        leg["trace"].append([t, est, hd, p["APOS"]["i32"], p["SRH"]["u16"]])
        if not started and (est == HOME_STATE or not hd & 1):
            started, leg["started_s"] = True, t
        if started and est == 0:
            if not hd & 1:
                raise g4._Fault(f"{axis} homing ended without HOMEDONE (interrupted or over-current) after {t:g} s")
            leg["done_s"] = t
            break
        if not started and t > START_TIMEOUT_S:
            raise g4._Fault(f"{axis} did not start homing within {START_TIMEOUT_S:g} s (ESTATEV {est}, HOMEDONE {hd})")
        if t > HOME_TIMEOUT_S:
            raise g4._Fault(f"{axis} still homing after {HOME_TIMEOUT_S:g} s (ESTATEV {est}, HOMEDONE {hd})")
        link.sleep(POLL_S)
    link.sleep(SETTLE_S)


def _procedure(link: g4._Link, res, axes: tuple[str, ...] = ("X", "Y")) -> None:
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
        base = {ax: _read_all(link, ax, BASELINE) for ax in AXIS_IDS}
        z = {n: link.read(ZR[n]) for n in ("ESTATEV", "APOS", "CPOS")}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline = {"X": base["X"], "Y": base["Y"], "Z": z}
    problems = baseline_problems(base, z)
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    summary: list[str] = []
    failed: list[str] = []
    finals: dict[str, object] = {}
    for ax in axes:
        try:
            _home_axis(link, res, ax)
            fin = _read_all(link, ax, FINAL)
        except g4._Fault as f:
            res.fault = str(f)
            try:  # read-only: what the drives say now; nothing is sent
                after: dict[str, object] = {a: {k: _i(v) for k, v in _read_all(link, a, FINAL).items()}
                                            for a in AXIS_IDS}
                res.final = after
                res.final["ESTATEV_all"] = {r.axis: link.read(r)["u16"] for r in g4.ESTATEV_ALL}
            except g4._Fault as f2:
                res.final["read_error"] = str(f2)
            res.outcome = "fault_stopped"
            res.message = (f"{f}. Nothing else was sent (a stop is not read while the drive homes). Leave the stage "
                           "and power-cycle the microscope; do not turn the turret until the stage is centred")
            return
        checks = final_checks(fin, base[ax])
        finals[ax] = {"values": {k: _i(v) for k, v in fin.items()}, "checks": checks}
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            failed += [f"{ax} {k}" for k in bad]
            break  # never home Y after a failed X
        summary.append(f"{ax} homed in {res.legs[ax]['done_s']:g} s (index delta {_i(fin['LHOMEINDEXDELTA'])}, "
                       f"peak current {fin['IIQMAX']['i16']}/{fin['IIQMIN']['i16']})")
    try:
        finals["ESTATEV_all"] = {r.axis: link.read(r)["u16"] for r in g4.ESTATEV_ALL}
    except g4._Fault as f:
        finals["read_error"] = str(f)
    res.final = finals
    res.outcome = "fail" if failed else "pass"
    if failed:
        res.message = ("FAILED final checks: " + ", ".join(failed)
                       + ". Nothing else was sent; power-cycle the microscope")
    else:
        res.message = ("; ".join(summary) + "; limits, speed and IINITONCEDONE unchanged, MER clear. The stage is at "
                       "home (0, 0): centre it before any turret change")


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate20-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate20_xy_rehome",
                                description="Motor Gate 20: re-home X then Y with the vendor's ESTATE 19. Dry run by default.")  # noqa: E501
    p.add_argument("--axes", choices=("XY", "X", "Y"), default="XY", help="X then Y (default), or one axis")
    p.add_argument("--send", action="store_true", help="run it (phrase: REHOME X AND Y ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on and homed >= 2 minutes")
    p.add_argument("--stage-clear-watching", action="store_true",
                   help="stage travel to the home corner is clear, hands clear, the OpenEVOS app closed, operator "
                        "watching the stage with a hand near the power switch")
    p.add_argument("--confirm", default="")
    p.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = p.parse_args(argv)
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_clear_watching=a.stage_clear_watching, axes=tuple(a.axes), **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  would write {ex.what:20s} {ex.request_hex}")
    # every run writes evidence, dry runs and refusals included
    print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
