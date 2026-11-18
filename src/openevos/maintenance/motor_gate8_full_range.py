"""Motor Gate 8: full-range X/Y traverse inside the calibrated limits (ADR-0028; L4 reviewed).

Prerequisite: Gate 7 has loaded the calibrated limits (this session's power-up). One axis at a time:
X from home to (calibrated max - 2 mm) and back to 0, then Y likewise. Gate 4's verified machinery:
completion = SRH bit 9 + ESTATEV 13, encoder within LCONVERGEBAND, verified stop to idle; any fault ->
one controlled stop, nothing else. The only target writes are the two far targets and 0, computed from
the git-ignored calibration (never stored here) and rebuilt/validated before sending. Dry run by default.
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
from openevos.maintenance import motor_gate6_y_move as g6
from openevos.maintenance import motor_gate7_xy_limits as g7
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

SPEC = "docs/evidence/fl1_v1.6_motor_gate8_full_range_spec.md; ADR-0028"
MARGIN_MM = 2.0
SPEED_MM_S = 8.27
PLANS = {"X": (1, g4.X_PLAN), "Y": (2, g6.Y_PLAN)}


def _w(axis_id: int, counts: int) -> bytes:
    """CPOSCMD (0x03D4) 32-bit write, vendor order. Only X/Y, and only 0 <= counts <= 280,000."""
    if axis_id not in (1, 2) or not 0 <= counts <= 280_000:
        raise g4.MotorGate4Refused(f"no traverse target {counts} for axis {axis_id}")
    body = bytes([0x08, 0x00, axis_id << 4, 0x25, 0xD4]) + g7.vendor32(counts)
    return body + bytes([sum(body) & 0xFF])


def plan_targets(lim: dict[str, tuple[int, int]]) -> dict[str, int]:
    return {ax: lim[ax][1] - round(MARGIN_MM * g7.COUNTS_PER_MM) for ax in ("X", "Y")}


def confirm_phrase(device: str) -> str:
    return f"TRAVERSE XY FULL RANGE ON {device.upper()}"


MAX_CYCLES = 5
#: measurement runs only: largest landing error recorded instead of stopping (50 counts = 22 um on X/Y)
MAX_TOLERANCE_COUNTS = 50
#: stepped tests: the shortest Y target allowed, and the current fraction that ends the run after the cycle
MIN_Y_TARGET_MM = 5.0
CURRENT_STOP_FRACTION = 0.70


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_watching: bool = False,
        cycles: int = 1, tolerance_counts: int | None = None, return_home: bool = False,
        axes: str = "XY", y_target_mm: float | None = None,
        calibration: Path | None = None, scans: Path = Path("openevos-scans"),
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    src = calibration or g7.latest_parsed(scans)
    if src is None:
        res.message = "no parsed calibration found; nothing opened"
        return res
    lim, problems = g7.load_limits(src)
    if problems:
        res.outcome, res.message = "calibration_refused", "; ".join(problems)
        return res
    targets = plan_targets(lim)
    if axes not in ("XY", "X", "Y"):
        res.message = "axes must be XY, X or Y; nothing opened"
        return res
    if y_target_mm is not None:
        y = round(y_target_mm * g7.COUNTS_PER_MM)
        if not (y_target_mm >= MIN_Y_TARGET_MM and 0 < y <= targets["Y"]):
            res.message = (f"Y target must be {MIN_Y_TARGET_MM}..{targets['Y'] / g7.COUNTS_PER_MM:.3f} mm "
                           "(calibrated max - 2 mm); nothing opened")
            return res
        targets["Y"] = y
    targets = {ax: t for ax, t in targets.items() if ax in axes}
    frames = {ax: _w(PLANS[ax][0], t) for ax, t in targets.items()}
    home = {ax: _w(PLANS[ax][0], 0) for ax in ("X", "Y")}
    res.baseline["axes"] = axes
    if not 1 <= cycles <= MAX_CYCLES:
        res.message = f"cycles must be 1..{MAX_CYCLES}; nothing opened"
        return res
    if tolerance_counts is not None and not 1 <= tolerance_counts <= MAX_TOLERANCE_COUNTS:
        res.message = f"tolerance must be 1..{MAX_TOLERANCE_COUNTS} counts; nothing opened"
        return res
    res.baseline = {"targets_mm": {ax: round(t / g7.COUNTS_PER_MM, 3) for ax, t in targets.items()}, "cycles": cycles,
                    "tolerance_counts": tolerance_counts}
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if return_home:
        return _run_return_home(res, dev, send, confirm, on_for_2_min, stage_empty_watching, home, open_port,
                                monotonic, sleep)
    if not send:
        res.outcome = "dry_run"
        legs = ", then ".join(f"{ax} to {mm} mm and back" for ax, mm in res.baseline["targets_mm"].items())
        res.message = (f"dry run: {legs} x {cycles} (~{sum(targets.values()) * cycles / g7.COUNTS_PER_MM * 2 / SPEED_MM_S:.0f} s "  # noqa: E501
                       "of motion); nothing opened or sent")
        res.exchanges = [Exchange(f"{ax} far target", f.hex(" ")) for ax, f in frames.items()]
        return res
    if not on_for_2_min or not stage_empty_watching:
        res.message = "operator has not confirmed >= 2 min on, an empty stage with the full travel clear, and watching"
        return res
    if confirm != confirm_phrase(dev):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev)!r}); nothing opened"
        return res
    allowed = set(frames.values()) | set(home.values()) | {p.move for _a, p in PLANS.values()} \
        | {f for _a, p in PLANS.values() for _n, f in p.stop}
    reads = {g4.ID_QUERY, *(r.frame for r in g4.READINESS),
             *(r.frame for _a, p in PLANS.values() for r in p.reads.values()), *g9.STATUS_FRAMES}
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
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=allowed, allowed_reads=reads)
        _procedure(link, res, lim, targets, frames, home)
    finally:
        port.close()
    return res


def return_home_phrase(device: str) -> str:
    return f"RETURN XY TO ZERO ON {device.upper()}"


def _run_return_home(res, dev, send, confirm, on_for_2_min, stage_empty_watching, home, open_port, monotonic, sleep):
    """Recovery after a stopped run: move X/Y back to position 0 (the drives' power-up home), using only the
    verified return leg (CPOSCMD = 0, ESTATE = 8, then the verified stop). This is NOT re-homing: it requires
    the drives to be still referenced (homed marker set, idle, at rest, APOS == CPOS within band)."""
    if not send:
        res.outcome = "dry_run"
        res.message = "dry run: return X/Y to position 0 with the verified return leg; nothing opened or sent"
        res.exchanges = [Exchange(f"{ax} CPOSCMD 0", f.hex(" ")) for ax, f in home.items()]
        return res
    if not on_for_2_min or not stage_empty_watching:
        res.message = "operator has not confirmed >= 2 min on, the travel back to 0 clear, and watching"
        return res
    if confirm != return_home_phrase(dev):
        res.message = f"confirmation phrase missing or wrong (expected {return_home_phrase(dev)!r}); nothing opened"
        return res
    allowed = set(home.values()) | {p.move for _a, p in PLANS.values()} | {f for _a, p in PLANS.values() for _n, f in p.stop}  # noqa: E501
    reads = {g4.ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for _a, p in PLANS.values() for r in p.reads.values()),  # noqa: E501
             *g9.STATUS_FRAMES}
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
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=allowed, allowed_reads=reads)
        for baud in BAUD_PASSES:
            link.port.configure(baud)
            if link.sync(f"line sync @ {baud}"):
                res.baud = baud
                break
        if res.baud is None or link.query_id() != 1:
            res.outcome, res.message = "stopped", "no link or wrong gateway ID; nothing written"
            return res
        try:
            for r in g4.READINESS:
                v = link.read(r)
                if (r.var == "ESTATEV" and v["u16"] != 0) or (r.var == "IINITONCEDONE" and v["u16"] != INIT_DONE_MARKER):  # noqa: E501
                    res.outcome, res.message = "not_idle", f"{r.axis} {r.var} = {v['u16']}; nothing written"
                    return res
            base = {ax: {n: link.read(p.reads[n]) for n in g4.BASELINE} for ax, (_i, p) in PLANS.items()}
            res.baseline["status"] = {"before": g9.snapshot(link)}
        except g4._Fault as f:
            res.outcome, res.message = "stopped", f"{f}; nothing written"
            return res
        res.baseline["before"] = {ax: {n: v.get("i32", v.get("u16")) for n, v in b.items()} for ax, b in base.items()}
        problems = []
        for ax, b in base.items():
            band = b["LCONVERGEBAND"]["i32"]
            if not 0 < band <= g4.MAX_BAND:
                problems.append(f"{ax} LCONVERGEBAND {band} not sane")
            if not b["SRH"]["u16"] & g4.SRH_NOT_MOVING_BIT:
                problems.append(f"{ax} is moving")
            if abs(b["APOS"]["i32"] - b["CPOS"]["i32"]) > band:
                problems.append(f"{ax} not at rest (APOS {b['APOS']['i32']} vs CPOS {b['CPOS']['i32']})")
            if not b["LPOSMIN"]["i32"] <= 0 <= b["LPOSMAX"]["i32"] or \
                    not b["LPOSMIN"]["i32"] <= b["APOS"]["i32"] <= b["LPOSMAX"]["i32"]:
                problems.append(f"{ax} position or 0 outside the drive limits")
            for var, raw in (("CSPDCMD", g4.RAW_15_0), ("CACCCMD", g4.RAW_1_0), ("CACCSTOP", g4.RAW_1_0)):
                if b[var]["raw"] != raw:
                    problems.append(f"{ax} {var} is not the power-up value")
        if problems:
            res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
            return res
        moved = []
        for ax, (_aid, plan) in PLANS.items():
            band = base[ax]["LCONVERGEBAND"]["i32"]
            dist = abs(base[ax]["APOS"]["i32"])
            if dist <= band:
                continue
            try:
                g4._leg(link, res, f"{ax} to 0", f"{ax} CPOSCMD 0", home[ax], 0, band, plan=plan,
                        timeout_s=dist / g7.COUNTS_PER_MM / SPEED_MM_S + 6.0)
                _settled(link, res, f"{ax} to 0", plan, 0)
                for name, frame in plan.stop:
                    link.write(name, frame)
                moved.append(ax)
            except g4.DriftRefused as f:
                res.outcome, res.fault, res.message = "drift_refused", str(f), str(f)
                return res
            except g4._Fault as f:
                g4._fault_stop(link, res, str(f), plan=plan)
                _status(link, res, "after_fault")
                return res
        res.final = {ax: g4._final(link, base[ax]["LCONVERGEBAND"]["i32"], plan=p) for ax, (_i, p) in PLANS.items()}
        _status(link, res, "end")
        failed = [f"{ax}:{k}" for ax, fin in res.final.items() for k, v in fin["checks"].items() if not v]
        res.outcome = "fail" if failed else "pass"
        res.message = (f"returned to 0 ({', '.join(moved) or 'already at 0'}); idle" if not failed
                       else "FAILED final checks: " + ", ".join(failed))
    except g4._Fault as f:
        res.outcome, res.message = "fail", f"final reads failed: {f}"
    finally:
        port.close()
    return res


def _procedure(link, res, lim, targets, frames, home) -> None:
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
        base = {ax: {n: link.read(p.reads[n]) for n in g4.BASELINE} for ax, (_i, p) in PLANS.items()}
        res.baseline["status"] = {"before": g9.snapshot(link)}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline["before"] = {ax: {n: v.get("i32", v.get("u16")) for n, v in b.items()} for ax, b in base.items()}
    problems = []
    for ax, b in base.items():
        if ax in targets:  # the idle axis is only watched, so it need not be exactly at home
            problems += g4.baseline_problems(b, target=targets[ax], axis=ax)
        if (b["LPOSMIN"]["i32"], b["LPOSMAX"]["i32"]) != lim[ax]:
            problems.append(f"{ax} drive limits are not the calibrated ones (run Gate 7 first)")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    cycles = res.baseline.get("cycles", 1)
    tol = res.baseline.get("tolerance_counts")
    imaxprot = {ax: s.get("decoded", {}).get("IMAXPROT") for ax, s in res.baseline["status"]["before"].items()}
    low_margin = []
    for cycle in range(1, cycles + 1):
        if low_margin:
            break
        for ax, (_aid, plan) in PLANS.items():
            if ax not in targets:
                continue
            band = tol if tol is not None else base[ax]["LCONVERGEBAND"]["i32"]
            timeout = targets[ax] / g7.COUNTS_PER_MM / SPEED_MM_S + 6.0
            tag = f"c{cycle} {ax}" if cycles > 1 else ax
            try:
                other = "Y" if ax == "X" else "X"
                watch = ((f"{other}_APOS", PLANS[other][1].reads["APOS"]),)
                g4._leg(link, res, f"{tag} out", f"{ax} CPOSCMD far", frames[ax], targets[ax], band, plan=plan,
                        timeout_s=timeout, watch=watch)
                _watch_summary(res, f"{tag} out", other)
                _cross_drift(res, f"{tag} out", other, base[other]["CPOS"]["i32"])
                _settled(link, res, f"{tag} out", plan, targets[ax])
                low_margin += _peaks(link, res, f"{tag} out", ax, imaxprot.get(ax))
                link.sleep(1.5)
                g4._leg(link, res, f"{tag} home", f"{ax} CPOSCMD 0", home[ax], 0, band, plan=plan, timeout_s=timeout,
                        watch=watch)
                _watch_summary(res, f"{tag} home", other)
                _cross_drift(res, f"{tag} home", other, base[other]["CPOS"]["i32"])
                _settled(link, res, f"{tag} home", plan, 0)
                low_margin += _peaks(link, res, f"{tag} home", ax, imaxprot.get(ax))
                for name, frame in plan.stop:
                    link.write(name, frame)
            except g4.DriftRefused as f:
                res.outcome, res.fault = "drift_refused", str(f)
                res.message = f"cycle {cycle}: {f}"
                _status(link, res, "after_fault")
                return
            except _CrossDrift as f:
                res.outcome, res.fault = "cross_axis_encoder_drift", str(f)
                res.message = (f"cycle {cycle}: {f}. The moving axis finished its leg and is at rest; nothing further "
                               f"sent. {other} is locked: power-cycle to re-home before moving it")
                _status(link, res, "after_fault")
                return
            except g4._Fault as f:
                g4._fault_stop(link, res, f"cycle {cycle}: {f}", plan=plan)
                _status(link, res, "after_fault")
                return
    try:
        res.final = {ax: g4._final(link, base[ax]["LCONVERGEBAND"]["i32"], plan=p) for ax, (_i, p) in PLANS.items()}
        _status(link, res, "end")
    except g4._Fault as f:
        res.outcome, res.message = "fail", f"final reads failed: {f}"
        return
    failed = [f"{ax}:{k}" for ax, fin in res.final.items() for k, v in fin["checks"].items() if not v]
    if low_margin and not failed:
        res.outcome = "current_margin_low"
        res.message = ("stopped after the cycle: motor current reached " + ", ".join(low_margin)
                       + f" (>= {CURRENT_STOP_FRACTION:.0%} of the drive's IMAXPROT); returned home and idle")
        return
    res.outcome = "fail" if failed else "pass"
    res.message = ("full-range traverse complete: X and Y out and back within band; idle"
                   if not failed else "FAILED final checks: " + ", ".join(failed))


class _CrossDrift(Exception):
    pass


def _cross_drift(res, leg: str, other: str, cpos: int) -> None:
    """The idle axis must not drift more than DRIFT_MAX_COUNTS from its command while the other axis moves
    (2026-11-13: Y crept 0 -> -3 -> -8 -> -9 during X-only moves; >~14 counts reverses Y's commutation)."""
    seen = [p[f"{other}_APOS"] for p in res.legs[leg]["trace"] if f"{other}_APOS" in p]
    worst = max(seen, key=lambda a: abs(a - cpos), default=cpos)
    res.legs[leg][f"{other}_drift"] = worst - cpos
    if abs(worst - cpos) > g4.DRIFT_MAX_COUNTS:
        raise _CrossDrift(f"cross_axis_encoder_drift: {other} encoder reached {worst} (command {cpos}) during {leg}")


def _watch_summary(res, leg: str, other: str) -> None:
    """Range of the idle axis' encoder seen while this leg moved (it should not change)."""
    seen = [p[f"{other}_APOS"] for p in res.legs[leg]["trace"] if f"{other}_APOS" in p]
    if seen:
        res.legs[leg][f"{other}_APOS_range"] = [min(seen), max(seen)]


def _peaks(link, res, leg: str, ax: str, limit) -> list[str]:
    """Peak motor current of the leg just finished (IIQMAX/IIQMIN are reset by the drive at every move start).
    Returns a note when the peak reached CURRENT_STOP_FRACTION of the drive's over-current limit."""
    hi = link.read(g9.STATUS[ax]["IIQMAX"])["i16"]
    lo = link.read(g9.STATUS[ax]["IIQMIN"])["i16"]
    peak = max(hi, -lo)
    res.legs[leg].update(IIQMAX=hi, IIQMIN=lo, current_peak=peak,
                         current_fraction=round(peak / limit, 3) if limit else None)
    if limit and peak >= CURRENT_STOP_FRACTION * limit:
        return [f"{leg} {peak}/{limit}"]
    return []


def _status(link, res, moment: str) -> None:
    """Read-only Gate 9 status capture (why a drive stopped); a read failure here is recorded, never raised."""
    try:
        res.baseline.setdefault("status", {})[moment] = g9.snapshot(link)
    except g4._Fault as f:
        res.baseline.setdefault("status", {})[moment] = {"read_error": str(f)}


def _settled(link, res, leg: str, plan, target: int) -> None:
    """0.5 s after completion, re-read the encoder: the settled landing error (repeatability data)."""
    link.sleep(0.5)
    apos = link.read(plan.reads["APOS"])["i32"]
    res.legs[leg].update(APOS_settled=apos, error_settled=apos - target)


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate8-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate8_full_range",
                                description="Motor Gate 8: full-range X/Y traverse. Dry run by default.")
    p.add_argument("--send", action="store_true")
    p.add_argument("--instrument-on-2min", action="store_true")
    p.add_argument("--stage-empty-watching", action="store_true",
                   help="empty stage, the FULL X/Y travel clear, operator watching near the power switch")
    p.add_argument("--confirm", default="")
    p.add_argument("--calibration", type=Path, default=None)
    p.add_argument("--cycles", type=int, default=1, help=f"repeat the X then Y out-and-back cycle (1..{MAX_CYCLES})")
    p.add_argument("--return-home", action="store_true",
                   help="recovery: move X/Y back to position 0 with the verified return leg (phrase RETURN XY TO ZERO ON COMn)")  # noqa: E501
    p.add_argument("--tolerance-counts", type=int, default=None,
                   help=f"measurement runs: landing error tolerated per leg (1..{MAX_TOLERANCE_COUNTS}); default = the "
                        "drive's LCONVERGEBAND")
    p.add_argument("--axes", choices=("XY", "X", "Y"), default="XY", help="which axes to traverse (default XY)")
    p.add_argument("--y-target-mm", type=float, default=None,
                   help=f"stepped tests: Y out target in mm ({MIN_Y_TARGET_MM} .. calibrated max - 2 mm)")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 8 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min, cycles=a.cycles,
              tolerance_counts=a.tolerance_counts, return_home=a.return_home, axes=a.axes, y_target_mm=a.y_target_mm,
              stage_empty_watching=a.stage_empty_watching, calibration=a.calibration, scans=a.evidence_dir, **seams)
    print(f"[{res.outcome}] {res.message}")
    for leg, d in res.legs.items():
        if "APOS" in d:
            print(f"  leg {leg}: target {d['target']}, APOS {d['APOS']}, error {d.get('error')}, "
                  f"settled {d.get('error_settled')}, current peak {d.get('current_peak')} "
                  f"({d.get('current_fraction')} of limit)"
                  + "".join(f", {k} {v}" for k, v in d.items() if k.endswith("_APOS_range")))
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
