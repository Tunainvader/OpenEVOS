"""Motor Gate 5: stop during motion. X is commanded towards +10 mm, Gate 3's stop is sent ~0.5 s in, the
drive must come to rest short of the target; then X returns to 0 and is stopped to idle (ADR-0020; L4).

Spec: docs/evidence/fl1_v1.6_motor_gate5_x_stop_in_motion_spec.md. Built on Motor Gate 4's link layer and
checks. The only writes: T1 CPOSCMD = 22680, Gate 4's M2/M3 and Gate 3's W1..W3 (constants, X only).
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

from openevos.maintenance import motor_gate4_x_move as g4
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

SPEC = "docs/evidence/fl1_v1.6_motor_gate5_x_stop_in_motion_spec.md; ADR-0020"
FAR_TARGET = 22680  # 10.00 mm
T1_TARGET_FAR = bytes.fromhex("08 00 10 25 D4 58 98 00 00 01")
STOP_DELAY_S = 0.5
STOP_SETTLE_TIMEOUT_S = 2.0
SHORT_MARGIN = 500  # counts: "stopped short" means 500 < APOS < target - 500
ALLOWED_WRITES = frozenset({T1_TARGET_FAR, g4.M2_TARGET_HOME, g4.M3_ESTATE_MOVE, *(f for _n, f in g4.STOP)})


class MotorGate5Refused(RuntimeError):
    pass


def assert_allowed(request: bytes) -> None:
    if request == bytes([g4.SYNC_BYTE]) or request in g4.ALLOWED_READS or request in ALLOWED_WRITES:
        return
    raise MotorGate5Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 5 frames")


def _Link5(port, res, monotonic, sleep) -> g4._Link:
    """Gate 4's link restricted to Gate 5's write constants."""
    return g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES)


def confirm_phrase(device: str) -> str:
    return f"STOP X DURING 10MM MOVE ON {device.upper()}"


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_watching: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> g4.MotorGate4Result:
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
        res.message = (f"dry run: would open {dev}, check readiness and the X baseline, command X towards "
                       f"+{FAR_TARGET} counts (10 mm), send the controlled stop {STOP_DELAY_S} s in, verify it came to "
                       "rest short of the target, return to 0, stop to idle; nothing opened or sent")
        res.exchanges = [Exchange(n, f.hex(" ")) for n, f in
                         (("T1 CPOSCMD=22680", T1_TARGET_FAR), ("M3 ESTATE=8", g4.M3_ESTATE_MOVE), *g4.STOP,
                          ("M2 CPOSCMD=0", g4.M2_TARGET_HOME), ("M3 ESTATE=8", g4.M3_ESTATE_MOVE), *g4.STOP)]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
        return res
    if not stage_empty_watching:
        res.message = ("operator has not confirmed an empty stage, a clear path ~12 mm to the right, and watching X "
                       "with a hand near the power switch; nothing opened")
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
        _procedure(_Link5(port, res, monotonic, sleep), res)
    finally:
        port.close()
    return res


def _procedure(link: g4._Link, res: g4.MotorGate4Result) -> None:
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
        res.baseline = {name: link.read(g4.XR[name]) for name in g4.BASELINE}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    problems = g4.baseline_problems(res.baseline)
    if res.baseline["LPOSMAX"]["i32"] < FAR_TARGET:
        problems.append(f"LPOSMAX {res.baseline['LPOSMAX']['i32']} < {FAR_TARGET}")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    band = res.baseline["LCONVERGEBAND"]["i32"]

    # Phase C: move towards +10 mm, stop after a fixed delay (no reads in between)
    stop_leg = {"target": FAR_TARGET, "stop_delay_s": STOP_DELAY_S, "trace": []}
    res.legs["stop_in_motion"] = stop_leg
    try:
        link.write("T1 CPOSCMD=22680", T1_TARGET_FAR)
        link.write("M3 ESTATE=8", g4.M3_ESTATE_MOVE)
        link.sleep(STOP_DELAY_S)
        for name, frame in g4.STOP:
            link.write(name, frame)
    except g4._Fault as f:
        res.fault, res.outcome = str(f), "fault_stopped"
        res.message = f"FAULT during move/stop writes: {f}. Nothing further sent; if anything moves, switch off"
        _capture(link, res)
        return
    # Phase D: wait for rest, judge
    try:
        settled = False
        for _ in range(max(1, int(STOP_SETTLE_TIMEOUT_S / g4.POLL_S))):
            srh = link.read(g4.SRH)["u16"]
            st = link.read(g4._X["ESTATEV"])["u16"]
            stop_leg["trace"].append({"SRH": srh, "ESTATEV": st})
            if srh & g4.SRH_NOT_MOVING_BIT:
                settled = True
                break
            link.sleep(g4.POLL_S)
        apos, cpos = link.read(g4._X["APOS"])["i32"], link.read(g4._X["CPOS"])["i32"]
        st = link.read(g4._X["ESTATEV"])["u16"]
    except g4._Fault as f:
        res.fault, res.outcome = str(f), "fault_stopped"
        res.message = f"FAULT reading after the stop: {f}. Nothing further sent; recovery = operator power cycle"
        return
    stop_leg.update(APOS=apos, CPOS=cpos, ESTATEV=st, APOS_mm=round(apos / 2880 * 1.27, 3))
    if not settled:
        res.fault, res.outcome = "X not at rest 2 s after the stop", "fault_stopped"
        res.message = "FAULT: X not at rest 2 s after the stop. Nothing further sent; if it moves, switch off"
        return
    if SHORT_MARGIN < apos < FAR_TARGET - SHORT_MARGIN and st == 0 and abs(apos - cpos) <= band:
        stop_leg["result"] = "interrupted"
    elif abs(apos - FAR_TARGET) <= band:
        stop_leg["result"] = "not_interrupted"
    else:
        res.fault, res.outcome = f"unexpected rest state APOS {apos}, CPOS {cpos}, ESTATEV {st}", "fault_stopped"
        res.message = f"FAULT: {res.fault}. Nothing further sent; recovery = operator power cycle"
        return
    # Phase E/F/G: return to 0 as Gate 4's home leg, stop to idle, final checks
    try:
        g4._leg(link, res, "home", "M2 CPOSCMD=0", g4.M2_TARGET_HOME, 0, band)
        for name, frame in g4.STOP:
            link.write(name, frame)
        res.final = g4._final(link, band)
    except g4._Fault as f:
        g4._fault_stop(link, res, str(f))
        return
    failed = [k for k, v in res.final["checks"].items() if not v]
    if failed:
        res.outcome, res.message = "fail", "FAILED final checks: " + ", ".join(failed)
    elif stop_leg["result"] == "interrupted":
        res.outcome = "pass"
        res.message = (f"stop during motion verified: X came to rest at {apos} counts "
                       f"({stop_leg['APOS_mm']} mm) short of {FAR_TARGET}; returned to 0; idle")
    else:
        res.outcome = "not_interrupted"
        res.message = ("the 10 mm move finished before the stop arrived (stop under motion NOT tested); "
                       "returned to 0 and idle")


def _capture(link, res) -> None:
    try:
        res.final = {"after_fault": {n: link.read(g4._X[n]) for n in ("ESTATEV", "APOS", "CPOS")}}
    except g4._Fault as f:
        res.final = {"after_fault_read_error": str(f)}


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate5-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate5_x_stop_in_motion",
                                description="Motor Gate 5: stop X during a 10 mm move, then return. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run it (phrase: STOP X DURING 10MM MOVE ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true")
    p.add_argument("--stage-empty-watching", action="store_true",
                   help="empty stage, clear path ~12 mm to the right, operator watching X near the power switch")
    p.add_argument("--confirm", default="")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 5 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_empty_watching=a.stage_empty_watching, **seams)
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
