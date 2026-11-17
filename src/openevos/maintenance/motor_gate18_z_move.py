"""Motor Gate 18: first Z (focus) motion, an idle stop, then +1 mm (8,200 counts) and back to 0 (ADR-0052; L4).

Spec: docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md. Transport as Motor Gate 1 rev 2 (Gate 4's
link). The ONLY writes this module can send are constants to axis 4 (Z), computed from ZAxis.t variables.cfg exactly as
Gates 3/4 did for X:
  Z1 CPOSCMD = 8200, Z2 CPOSCMD = 0, Z3 ESTATE = 8 (STATE_8_MOVE_POS, clamped by the drive to LPOSMIN..LPOSMAX),
  Z4 ESTATE = 6 (STATE_6_STOP: CACC = CACCSTOP, CPOS = APOS, ESTATEV 0; Z stays powered).
Speed/acceleration are never written: the drive's power-up CSPDCMD 10.0 / CACCCMD 0.25 are required instead.
Completion = SRH bit 9 set, ESTATEV 13 and CPOS = target (Gate 4's `_leg`), then |APOS - target| <= LCONVERGEBAND.
On any fault after the first move write: Z's stop once, read the state, send nothing else. Dry run by default.
The operator watches the objective and reports which way it went (Gate 18 sets the Z sign convention).
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate17_z_o_reads as g17
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    ID_QUERY,
    INIT_DONE_MARKER,
    Exchange,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md; ADR-0052"
Z_ID = 4
TARGET = 8200  # counts = 1.0004 mm at 5,000 counts per 0.61 mm
#: the reviewed out targets by --mm (2026-11-17: 1 mm was confirmed by the encoder but too small to see by eye, so the
#: operator asked for 5 mm = 41,000 counts, 61 % of the drive's own LPOSMAX 67,000)
TARGETS = {1: 8200, 5: 41000}
_CPOSCMD, _ESTATE = 0x03B3, 0x03B1
MOVE_STATE, STOP_STATE = 8, 6


def _frame(op: int, payload: bytes) -> bytes:
    body = bytes([0x06 if len(payload) == 2 else 0x08, 0x00, Z_ID << 4, op >> 8, op & 0xFF]) + payload
    return body + bytes([sum(body) & 0xFF])


def target_frame(counts: int) -> bytes:
    if counts not in (0, *TARGETS.values()):
        raise g4.MotorGate4Refused(f"Gate 18 builds only Z targets 0, {TARGETS[1]} and {TARGETS[5]}, not {counts}")
    b = counts.to_bytes(4, "big", signed=True)
    return _frame(0x2400 | (_CPOSCMD & 0x1FF), b[2:4] + b[0:2])


Z1_TARGET_OUT = target_frame(TARGET)
Z5_TARGET_OUT = target_frame(TARGETS[5])
OUT_FRAMES = {1: Z1_TARGET_OUT, 5: Z5_TARGET_OUT}
Z2_TARGET_HOME = target_frame(0)
Z3_ESTATE_MOVE = _frame(0x2000 | (_ESTATE & 0x1FF), MOVE_STATE.to_bytes(2, "big"))
Z4_ESTATE_STOP = _frame(0x2000 | (_ESTATE & 0x1FF), STOP_STATE.to_bytes(2, "big"))
STOP = (("Z4 ESTATE=6 (STOP)", Z4_ESTATE_STOP),)
ALLOWED_WRITES = frozenset({Z1_TARGET_OUT, Z5_TARGET_OUT, Z2_TARGET_HOME, Z3_ESTATE_MOVE, Z4_ESTATE_STOP})

ZR = g17.READS["Z"]
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in ZR.values()),
                           *(r.frame for r in g4.ESTATEV_ALL)})
Z_PLAN = g4.AxisPlan("Z", ZR, Z3_ESTATE_MOVE, STOP)
RAW_10_0 = 10 * 65536
RAW_0_25 = 65536 // 4
RAW_1_0 = 65536
LEG_TIMEOUT_S = 10.0  # 41,000 counts at ~12,500 counts/s (CSPDCMD 10.0; 1 mm took 2 polls on the unit) is ~3.3 s
PAUSE_S = 3.0  # at +1 mm, so the operator can see where the objective went
IDLE_STOP_SETTLE_S = 0.5


def confirm_phrase(device: str, mm: int = 1) -> str:
    return f"MOVE Z {mm}MM AND BACK ON {device.upper()}"


def _i(v: dict) -> int:
    return v.get("i32", v.get("u16"))


def baseline_problems(b: dict, target: int = TARGET) -> list[str]:
    p = []
    if b["IINITONCEDONE"]["u16"] != INIT_DONE_MARKER:
        p.append("Z not homed")
    if b["ESTATEV"]["u16"] != 0:
        p.append(f"Z not idle (ESTATEV {b['ESTATEV']['u16']})")
    band = b["LCONVERGEBAND"]["i32"]
    if not 0 < band <= g4.MAX_BAND:
        p.append(f"LCONVERGEBAND {band} outside 1..{g4.MAX_BAND}")
    if abs(b["APOS"]["i32"]) > band or abs(b["CPOS"]["i32"]) > band:
        p.append(f"Z not at home (APOS {b['APOS']['i32']}, CPOS {b['CPOS']['i32']})")
    if not b["LPOSMIN"]["i32"] < 0 < target < b["LPOSMAX"]["i32"]:
        p.append(f"Z limits {b['LPOSMIN']['i32']}..{b['LPOSMAX']['i32']} do not contain 0..{target}")
    if b["CSPDCMD"]["i32"] != RAW_10_0 or b["CACCCMD"]["i32"] != RAW_0_25 or b["CACCSTOP"]["i32"] != RAW_1_0:
        p.append("Z speed/acceleration/stop deceleration are not the power-up 10.0 / 0.25 / 1.0")
    if b["MER"]["u16"]:
        p.append(f"Z MER 0x{b['MER']['u16']:04X} not clear")
    if not b["SRL"]["u16"] & 0x8000:
        p.append(f"Z power stage off (SRL 0x{b['SRL']['u16']:04X})")
    if not b["SRH"]["u16"] & g4.SRH_NOT_MOVING_BIT:
        p.append("Z SRH bit 9 clear (moving)")
    return p


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_watching: bool = False,
        mm: int = 1,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports, open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if mm not in TARGETS:
        res.message = f"--mm must be one of {sorted(TARGETS)}; nothing opened"
        return res
    target, out_frame = TARGETS[mm], OUT_FRAMES[mm]
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would check readiness and the Z baseline, send Z's stop at rest, move Z to "
                       f"+{target} counts ({mm} mm), pause {PAUSE_S:g} s, return to 0, stop to idle, and verify; "
                       "nothing opened or sent")
        res.exchanges = [Exchange(n, f.hex(" ")) for n, f in
                         (*STOP, (f"Z1 CPOSCMD={target}", out_frame), ("Z3 ESTATE=8", Z3_ESTATE_MOVE),
                          ("Z2 CPOSCMD=0", Z2_TARGET_HOME), ("Z3 ESTATE=8", Z3_ESTATE_MOVE), *STOP)]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on (and homed) for >= 2 minutes; nothing opened"
        return res
    if not stage_empty_watching:
        res.message = ("operator has not confirmed an empty stage (no vessel, slide or holder insert), hands clear of "
                       "the objectives, and watching the objective with a hand near the power switch; nothing opened")
        return res
    if confirm != confirm_phrase(dev, mm):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev, mm)!r}); nothing opened"
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
        _procedure(link, res, target, out_frame)
    finally:
        port.close()
    return res


def _procedure(link: g4._Link, res, target: int = TARGET, out_frame: bytes = Z1_TARGET_OUT) -> None:
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
        res.baseline = {n: link.read(ZR[n]) for n in ("IINITONCEDONE", "ESTATEV", "APOS", "CPOS", "LPOSMIN", "LPOSMAX",
                                                     "CSPDCMD", "CACCCMD", "CACCSTOP", "LCONVERGEBAND", "MER", "SRL",
                                                     "SRH", "LHOMEINDEXDELTA")}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    problems = baseline_problems(res.baseline, target)
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    band = res.baseline["LCONVERGEBAND"]["i32"]
    apos0 = res.baseline["APOS"]["i32"]
    try:
        # Phase A: Z's stop at rest (as Gate 3 did for X): nothing may move, Z stays on
        link.write(*STOP[0])
        link.sleep(IDLE_STOP_SETTLE_S)
        a = {n: link.read(ZR[n]) for n in ("ESTATEV", "APOS", "CPOS", "SRL", "MER")}
        res.legs["idle_stop"] = {k: _i(v) for k, v in a.items()}
        if a["ESTATEV"]["u16"] != 0 or abs(a["APOS"]["i32"] - apos0) > band or not a["SRL"]["u16"] & 0x8000 \
                or a["MER"]["u16"]:
            raise g4._Fault(f"idle stop changed Z: {res.legs['idle_stop']}")
        # Phase B: out, pause, back to 0, stop at rest
        g4._leg(link, res, "out", f"Z1 CPOSCMD={target}", out_frame, target, band, plan=Z_PLAN, timeout_s=LEG_TIMEOUT_S)
        link.sleep(PAUSE_S)
        g4._leg(link, res, "home", "Z2 CPOSCMD=0", Z2_TARGET_HOME, 0, band, plan=Z_PLAN, timeout_s=LEG_TIMEOUT_S)
        link.write(*STOP[0])
        fin = {n: link.read(ZR[n]) for n in ("ESTATEV", "APOS", "CPOS", "SRL", "MER", "CSPDCMD", "CACCCMD",
                                             "CACCSTOP", "IIQMAX", "IIQMIN", "LPOSMIN", "LPOSMAX")}
        idle = {r.axis: link.read(r)["u16"] for r in g4.ESTATEV_ALL}
    except g4._Fault as f:
        g4._fault_stop(link, res, str(f), plan=Z_PLAN)
        return
    checks = {
        "z_idle": fin["ESTATEV"]["u16"] == 0,
        "all_axes_idle": all(v == 0 for v in idle.values()),
        "z_home_within_band": abs(fin["APOS"]["i32"]) <= band and abs(fin["CPOS"]["i32"]) <= band,
        "z_still_on": bool(fin["SRL"]["u16"] & 0x8000),
        "mer_clear": fin["MER"]["u16"] == 0,
        "speed_accel_unchanged": (fin["CSPDCMD"]["i32"], fin["CACCCMD"]["i32"], fin["CACCSTOP"]["i32"])
                                 == (RAW_10_0, RAW_0_25, RAW_1_0),
        "limits_unchanged": (fin["LPOSMIN"]["i32"], fin["LPOSMAX"]["i32"])
                            == (res.baseline["LPOSMIN"]["i32"], res.baseline["LPOSMAX"]["i32"]),
    }
    res.final = {"z": {k: _i(v) if k not in ("IIQMAX", "IIQMIN") else v.get("i16") for k, v in fin.items()},
                 "ESTATEV_all": idle, "checks": checks}
    failed = [k for k, v in checks.items() if not v]
    res.outcome = "fail" if failed else "pass"
    res.message = (f"Z: idle stop moved nothing; +{target} counts (APOS {res.legs['out']['APOS']}) and back to "
                   f"{fin['APOS']['i32']}; stopped to idle, still powered, MER clear; all axes idle"
                   if not failed else "FAILED final checks: " + ", ".join(failed))


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate18-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate18_z_move",
                                description="Motor Gate 18: Z idle stop, then +1 or +5 mm and back to 0. Dry run by default.")
    p.add_argument("--mm", type=int, choices=sorted(TARGETS), default=1, help="how far out: 1 or 5 mm")
    p.add_argument("--send", action="store_true", help="run it (phrase: MOVE Z <mm>MM AND BACK ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on and homed >= 2 minutes")
    p.add_argument("--stage-empty-watching", action="store_true",
                   help="no vessel/slide/insert on the stage, hands clear of the objectives, operator watching the "
                        "objective with a hand near the power switch")
    p.add_argument("--confirm", default="")
    p.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = p.parse_args(argv)
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_empty_watching=a.stage_empty_watching, mm=a.mm)
    print(f"[{res.outcome}] {res.message}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  would write {ex.what:24s} {ex.request_hex}")
    for leg, d in res.legs.items():
        if "APOS" in d and "target" in d:
            print(f"  leg {leg}: target {d['target']}, APOS {d['APOS']}, polls {len(d['trace'])}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
        print("Tell the session which way the objective moved during the out leg (towards or away from the stage).")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
