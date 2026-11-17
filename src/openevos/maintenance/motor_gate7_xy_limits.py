"""Motor Gate 7: load the calibrated X/Y stage limits into the drives (ADR-0022; L4 reviewed; no motion).

Spec: docs/evidence/fl1_v1.6_motor_gate7_xy_limits_spec.md. PearlScope passes the stored mcba
X/YAxisPositionLimits unchanged to SetX/YAxisPositionLimits, which write LPOSMIN (25 B4) and LPOSMAX
(25 B6) and read each back. This module does the same for X (00 10) and Y (00 20) only.

The four values are read at run time from the git-ignored parsed calibration (Step D + pearl_persistent)
and validated against the independently known spans; no calibration value lives in this repository.
Only frames with opcode 25B4/25B6, destination 00 10/00 20 and exactly the validated values can be sent.
RAM only: VAR_INIT restores 0..50,000 at the next power-up. Dry run by default.
"""

from __future__ import annotations

import argparse
import json
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
    ID_QUERY,
    INIT_DONE_MARKER,
    Exchange,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate7_xy_limits_spec.md; ADR-0022"
COUNTS_PER_MM = 2880 / 1.27
POWER_UP = (0, 50000)
EXPECTED_SPAN_MM = {"X": 119.1, "Y": 78.1}
SPAN_TOL_MM = 0.5
MIN_BEHIND_HOME_MM = 2.0
AXES = (("X", 1), ("Y", 2))
OPS = {"LPOSMIN": 0x25B4, "LPOSMAX": 0x25B6}
READS = {ax: g4.axis_reads(ax, aid) for ax, aid in AXES}
ZF_LIMITS = tuple(r for r in g2.READS if r.axis in ("Z", "F") and r.var in ("LPOSMIN", "LPOSMAX"))
BASE_VARS = ("APOS", "CPOS", "LPOSMIN", "LPOSMAX", "SRH", "LCONVERGEBAND")
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in ZF_LIMITS),
                           *(READS[ax][v].frame for ax, _ in AXES for v in BASE_VARS)})


class MotorGate7Refused(RuntimeError):
    pass


def vendor32(v: int) -> bytes:
    b = v.to_bytes(4, "big", signed=True)
    return b[2:4] + b[0:2]


def build_limit_write(axis_id: int, var: str, value: int) -> bytes:
    """The only write builder: X/Y LPOSMIN/LPOSMAX, 32-bit, vendor order (as SetXAxisPositionLimits)."""
    if axis_id not in (1, 2) or var not in OPS:
        raise MotorGate7Refused(f"no limit write for axis {axis_id} {var}")
    op = OPS[var]
    body = bytes([0x08, 0x00, axis_id << 4, op >> 8, op & 0xFF]) + vendor32(int(value))
    return body + bytes([sum(body) & 0xFF])


def latest_parsed(scans: Path) -> Path | None:
    files = sorted(scans.glob("led-application-stepd-*-parsed.json"))
    return files[-1] if files else None


def load_limits(path: Path) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Read and validate the backed-up X/Y limits. Returns ({axis: (min, max)}, problems)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mb = data["motor_base"]
        lim = {ax: (int(mb[f"{ax}AxisPositionLimits.lMin"]), int(mb[f"{ax}AxisPositionLimits.lMax"])) for ax, _ in AXES}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, [f"cannot read X/Y limits from {path.name}: {exc}"]
    problems = []
    for ax, (lo, hi) in lim.items():
        if not -MIN_BEHIND_HOME_MM * COUNTS_PER_MM < lo <= 0:
            problems.append(f"{ax} lMin {lo / COUNTS_PER_MM:.3f} mm outside (-{MIN_BEHIND_HOME_MM}, 0]")
        span = (hi - lo) / COUNTS_PER_MM
        if abs(span - EXPECTED_SPAN_MM[ax]) > SPAN_TOL_MM:
            problems.append(f"{ax} span {span:.2f} mm != {EXPECTED_SPAN_MM[ax]} ± {SPAN_TOL_MM}")
        if hi <= POWER_UP[1]:
            problems.append(f"{ax} lMax {hi} does not widen the power-up {POWER_UP[1]}")
    return lim, problems


def plan_writes(lim: dict[str, tuple[int, int]]) -> list[tuple[str, int, str, int, bytes]]:
    out = []
    for ax, aid in AXES:
        lo, hi = lim[ax]
        out += [(ax, aid, "LPOSMIN", lo, build_limit_write(aid, "LPOSMIN", lo)),
                (ax, aid, "LPOSMAX", hi, build_limit_write(aid, "LPOSMAX", hi))]
    return out


def confirm_phrase(device: str) -> str:
    return f"LOAD XY CALIBRATED LIMITS ON {device.upper()}"


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_clear: bool = False,
        calibration: Path | None = None, scans: Path = Path("openevos-scans"),
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> g4.MotorGate4Result:
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    src = calibration or latest_parsed(scans)
    if src is None:
        res.message = "no parsed calibration (led-application-stepd-*-parsed.json) found; nothing opened"
        return res
    lim, problems = load_limits(src)
    if problems:
        res.outcome, res.message = "calibration_refused", "; ".join(problems) + "; nothing opened"
        return res
    writes = plan_writes(lim)
    res.baseline = {"calibration_file": src.name,
                    "planned_mm": {ax: [round(v / COUNTS_PER_MM, 3) for v in lim[ax]] for ax, _ in AXES}}
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would load X/Y limits from {src.name} (X {res.baseline['planned_mm']['X']} mm, "
                       f"Y {res.baseline['planned_mm']['Y']} mm), each read back; no motion; nothing opened or sent")
        res.exchanges = [Exchange(f"{ax} {var}", f.hex(" ")) for ax, _aid, var, _v, f in writes]
        return res
    if not on_for_2_min or not stage_clear:
        res.message = "operator has not confirmed >= 2 minutes on and a clear stage; nothing opened"
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
        link = g4._Link(port, res, monotonic, sleep, allowed_writes={w[4] for w in writes},
                        allowed_reads=ALLOWED_READS)
        _procedure(link, res, lim, writes)
    finally:
        port.close()
    return res


def _procedure(link: g4._Link, res: g4.MotorGate4Result, lim, writes) -> None:
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
        before = {ax: {v: link.read(READS[ax][v]) for v in BASE_VARS} for ax, _ in AXES}
        zf_before = {f"{r.axis} {r.var}": link.read(r)["i32"] for r in ZF_LIMITS}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline["before"] = {ax: {v: d.get("i32", d.get("u16")) for v, d in b.items()} for ax, b in before.items()}
    res.baseline["zf_limits_before"] = zf_before
    problems, already = [], True
    for ax, _ in AXES:
        b = before[ax]
        band = b["LCONVERGEBAND"]["i32"]
        if not b["SRH"]["u16"] & g4.SRH_NOT_MOVING_BIT:
            problems.append(f"{ax} moving")
        if abs(b["APOS"]["i32"]) > band or abs(b["CPOS"]["i32"]) > band:
            problems.append(f"{ax} not at home (APOS {b['APOS']['i32']}, CPOS {b['CPOS']['i32']})")
        cur = (b["LPOSMIN"]["i32"], b["LPOSMAX"]["i32"])
        if cur != lim[ax]:
            already = False
            if cur != POWER_UP:
                problems.append(f"{ax} limits {cur} are neither power-up {POWER_UP} nor the calibration")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    if already:
        res.outcome, res.message = "already_loaded", "X/Y limits already equal the calibration; nothing written"
        return
    readback = {}
    try:
        for ax, _aid, var, value, frame in writes:
            link.write(f"{ax} {var}", frame)
            got = link.read(READS[ax][var])["i32"]
            readback[f"{ax} {var}"] = got
            if got != value:
                raise g4._Fault(f"{ax} {var} read back {got}, wrote {value}")
    except g4._Fault as f:
        res.legs["readback"] = readback
        res.outcome = "write_failed"
        res.message = f"{f}; no further write. Limits are RAM only: a power cycle restores {POWER_UP}"
        return
    res.legs["readback"] = readback
    try:
        after = {ax: {v: link.read(READS[ax][v]) for v in ("APOS", "CPOS", "LPOSMIN", "LPOSMAX")} for ax, _ in AXES}
        zf_after = {f"{r.axis} {r.var}": link.read(r)["i32"] for r in ZF_LIMITS}
        idle = {r.axis: link.read(r)["u16"] for r in g4.ESTATEV_ALL}
    except g4._Fault as f:
        res.outcome, res.message = "fail", f"final reads failed: {f}"
        return
    checks = {"all_axes_idle": all(v == 0 for v in idle.values()), "zf_limits_untouched": zf_after == zf_before}
    for ax, _ in AXES:
        a, b = after[ax], before[ax]
        checks[f"{ax}_limits_loaded"] = (a["LPOSMIN"]["i32"], a["LPOSMAX"]["i32"]) == lim[ax]
        checks[f"{ax}_no_motion"] = (a["APOS"]["i32"], a["CPOS"]["i32"]) == (b["APOS"]["i32"], b["CPOS"]["i32"])
    res.final = {"checks": checks, "ESTATEV_all": idle}
    failed = [k for k, v in checks.items() if not v]
    res.outcome = "fail" if failed else "pass"
    res.message = ("calibrated X/Y limits loaded and read back; no motion; all axes idle. RAM only: lost at "
                   "power-off" if not failed else "FAILED final checks: " + ", ".join(failed))


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate7-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate7_xy_limits",
                                description="Motor Gate 7: load calibrated X/Y limits. Dry run by default.")
    p.add_argument("--send", action="store_true", help="run it (phrase: LOAD XY CALIBRATED LIMITS ON COMn)")
    p.add_argument("--instrument-on-2min", action="store_true")
    p.add_argument("--stage-clear", action="store_true")
    p.add_argument("--confirm", default="")
    p.add_argument("--calibration", type=Path, default=None, help="parsed calibration JSON (default: newest)")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 7 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min, stage_clear=a.stage_clear,
              calibration=a.calibration, scans=a.evidence_dir, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.outcome == "dry_run":
        for ex in res.exchanges:
            print(f"  {ex.what:16s} {ex.request_hex}")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "pass", "already_loaded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
