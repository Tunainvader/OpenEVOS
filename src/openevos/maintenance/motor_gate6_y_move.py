"""Motor Gate 6: first Y movement, +12 mm (27,213 counts) and back to 0 (ADR-0021; L4 reviewed).

Spec: docs/evidence/fl1_v1.6_motor_gate6_y_move_spec.md. Gate 4's machinery (now axis-generic) on axis 2.
The only writes are constants to Y (destination 00 20): CPOSCMD = 27213 / 0, ESTATE = 8, and the Y copy of
Gate 3's verified stop. Y's .sw has the same 128 labels at the same addresses and identical state-machine
code as X (only a start-up constant and setup-table tuning differ), and the same variable addresses.
Speed/acceleration are never written (power-up 15.0 / 1.0 required). Dry run by default.
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
    ID_QUERY,
    INIT_DONE_MARKER,
    SYNC_BYTE,
    Exchange,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate6_y_move_spec.md; ADR-0021"
TARGET = 27213  # 12.000 mm on Y (2,880 counts per 1.27 mm)
Y1_TARGET_OUT = bytes.fromhex("08 00 20 25 D4 6A 4D 00 00 D8")
Y2_TARGET_HOME = bytes.fromhex("08 00 20 25 D4 00 00 00 00 21")
Y3_ESTATE_MOVE = bytes.fromhex("06 00 20 21 B3 00 08 02")
Y_STOP = (("YW1 IPENDINGMOVES=1", bytes.fromhex("06 00 20 21 CD 00 01 15")),
          ("YW2 CACCSTOP=1.0", bytes.fromhex("08 00 20 25 E8 00 00 00 01 36")),
          ("YW3 ESTATE=6 (STATE_6_STOP)", bytes.fromhex("06 00 20 21 B3 00 06 00")))
ALLOWED_WRITES = frozenset({Y1_TARGET_OUT, Y2_TARGET_HOME, Y3_ESTATE_MOVE, *(f for _n, f in Y_STOP)})
YR = g4.axis_reads("Y", 2)
Y_PLAN = g4.AxisPlan("Y", YR, Y3_ESTATE_MOVE, Y_STOP)
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in YR.values())})
LEG_TIMEOUT_S = 5.0  # 12 mm at ~8.3 mm/s is ~1.5 s


class MotorGate6Refused(RuntimeError):
    pass


def assert_allowed(request: bytes) -> None:
    if request == bytes([SYNC_BYTE]) or request in ALLOWED_READS or request in ALLOWED_WRITES:
        return
    raise MotorGate6Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 6 frames")


def confirm_phrase(device: str) -> str:
    return f"MOVE Y 12MM AND BACK ON {device.upper()}"


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_watching: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> g4.MotorGate4Result:  # noqa: E501
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
        res.message = (f"dry run: would open {dev}, check readiness and the Y baseline, move Y to +{TARGET} counts "
                       "(12.000 mm), pause, return to 0, stop to idle, and verify; nothing opened or sent")
        res.exchanges = [Exchange(n, f.hex(" ")) for n, f in
                         (("Y1 CPOSCMD=27213", Y1_TARGET_OUT), ("Y3 ESTATE=8", Y3_ESTATE_MOVE),
                          ("Y2 CPOSCMD=0", Y2_TARGET_HOME), ("Y3 ESTATE=8", Y3_ESTATE_MOVE), *Y_STOP)]
        return res
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
        return res
    if not stage_empty_watching:
        res.message = ("operator has not confirmed an empty stage, ~13 mm clear in both Y directions, and watching "
                       "Y with a hand near the power switch; nothing opened")
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
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=ALLOWED_READS)
        _procedure(link, res)
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
        res.baseline = {name: link.read(YR[name]) for name in g4.BASELINE}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    problems = g4.baseline_problems(res.baseline, target=TARGET, axis="Y")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    band = res.baseline["LCONVERGEBAND"]["i32"]
    try:
        g4._leg(link, res, "out", "Y1 CPOSCMD=27213", Y1_TARGET_OUT, TARGET, band, plan=Y_PLAN,
                timeout_s=LEG_TIMEOUT_S)
        link.sleep(g4.PAUSE_S)
        g4._leg(link, res, "home", "Y2 CPOSCMD=0", Y2_TARGET_HOME, 0, band, plan=Y_PLAN, timeout_s=LEG_TIMEOUT_S)
        for name, frame in Y_STOP:
            link.write(name, frame)
        res.final = g4._final(link, band, plan=Y_PLAN)
    except g4._Fault as f:
        g4._fault_stop(link, res, str(f), plan=Y_PLAN)
        return
    failed = [k for k, v in res.final["checks"].items() if not v]
    res.outcome = "fail" if failed else "pass"
    res.message = ("Y moved +12 mm and back to 0; positions within band; stopped to idle; all axes idle; "
                   "parameters unchanged" if not failed else "FAILED final checks: " + ", ".join(failed))


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate6-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate6_y_move",
                                description="Motor Gate 6: move Y +12 mm and back to 0. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run it (phrase: MOVE Y 12MM AND BACK ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true")
    p.add_argument("--stage-empty-watching", action="store_true",
                   help="empty stage, ~13 mm clear in both Y directions, operator watching near the power switch")
    p.add_argument("--confirm", default="")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 6 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_empty_watching=a.stage_empty_watching, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  {ex.what:30s} {ex.request_hex}")
    for leg, d in res.legs.items():
        if "APOS" in d:
            print(f"  leg {leg}: target {d['target']}, APOS {d['APOS']}, polls {len(d['trace'])}")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
