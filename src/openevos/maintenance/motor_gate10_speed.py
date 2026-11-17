"""Motor Gate 10: verify the three X/Y jog speeds on X (ADR-0032; L4 reviewed, RAM-only write).

Spec: docs/evidence/fl1_v1.6_motor_gate10_speed_spec.md. For each preset (slow, medium, fast) in turn: write
X CSPDCMD (one of three fixed frames), move X 0 -> +10 mm -> 0 with Gate 4's verified leg, and time each leg.
Finally write the power-up 15.0 back and read it back. Writes: the 3 X speed frames, X target 10 mm and 0,
X move request, X verified stop. Any fault -> one controlled stop, then the power-up speed is written back.
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

from openevos.backends.technosoft import protocol as tp
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate8_full_range as g8
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

SPEC = "docs/evidence/fl1_v1.6_motor_gate10_speed_spec.md; ADR-0032"
DIST_MM = 10.0
TARGET = round(DIST_MM * tp.COUNTS_PER_MM["X"])  # 22,677 counts
OUT, HOME = g8._w(1, TARGET), g8._w(1, 0)
SPEED = {p: tp.speed_write("X", p) for p in ("slow", "medium", "fast")}
ALLOWED_WRITES = frozenset({OUT, HOME, *SPEED.values(), g4.M3_ESTATE_MOVE, *(f for _n, f in g4.STOP)})
#: Each timed leg = DIST / true_speed + a fixed overhead (the gate's own reads ~0.4 s each, poll interval,
#: acceleration). A straight-line fit of leg time against 1/preset gives slope = DIST / scale and intercept =
#: overhead, where true_speed = scale x preset. Pass: scale within SCALE_BOUNDS (never > +5 % faster than set),
#: overhead plausible, and the fit tight (every leg within RESIDUAL_MAX_S of the line).
SCALE_BOUNDS = (0.85, 1.05)
OVERHEAD_BOUNDS_S = (0.0, 6.0)
RESIDUAL_MAX_S = 0.3


def analyse(results: dict) -> dict:
    pts = [(1.0 / r["preset_mm_s"], t) for r in results.values() for t in r["leg_s"].values()]
    problems = []
    if len({x for x, _ in pts}) < 2:
        return {"problems": ["fewer than two speeds measured"]}
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
    overhead = my - slope * mx
    scale = DIST_MM / slope
    worst = max(abs(y - (overhead + slope * x)) for x, y in pts)
    if not SCALE_BOUNDS[0] <= scale <= SCALE_BOUNDS[1]:
        problems.append(f"speed scale {scale:.3f} outside {SCALE_BOUNDS}")
    if not OVERHEAD_BOUNDS_S[0] <= overhead <= OVERHEAD_BOUNDS_S[1]:
        problems.append(f"overhead {overhead:.2f} s implausible")
    if worst > RESIDUAL_MAX_S:
        problems.append(f"legs off the fit by up to {worst:.2f} s")
    return {"speed_scale": round(scale, 4), "overhead_s": round(overhead, 3), "worst_residual_s": round(worst, 3),
            "true_mm_s": {p: round(scale * r["preset_mm_s"], 3) for p, r in results.items()},
            "problems": problems}


def confirm_phrase(device: str) -> str:
    return f"MEASURE X SPEEDS ON {device.upper()}"


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, stage_empty_watching: bool = False,
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
        res.message = ("dry run: X 0 -> +10 mm -> 0 at slow (1 mm/s), medium (4 mm/s), fast (8.27 mm/s, power-up), "
                       "then CSPDCMD back to 15.0; nothing opened or sent")
        res.exchanges = [Exchange(f"X CSPDCMD {p}", f.hex(" ")) for p, f in SPEED.items()]
        return res
    if not on_for_2_min or not stage_empty_watching:
        res.message = "operator has not confirmed >= 2 min on, X travel 0..10 mm clear, and watching"
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
        if driver_state_problems(res.port_state_read_back):
            res.outcome, res.message = "driver_state", "driver state not normal; nothing sent"
            return res
        reads = {g4.ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in g4.XR.values())}
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=reads)
        _procedure(link, res, monotonic)
    finally:
        port.close()
    return res


def _procedure(link, res, monotonic) -> None:
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
        base = {n: link.read(g4.XR[n]) for n in g4.BASELINE}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline = {n: v.get("i32", v.get("u16")) for n, v in base.items()}
    problems = g4.baseline_problems(base, target=TARGET, axis="X")
    # Start within the settle band is enough for a timing gate (X can rest 1-3 counts off 0 after a move):
    # at rest (encoder == command; the verified stop sets CPOS = APOS) and within LCONVERGEBAND of 0.
    apos0, cpos0, band0 = base["APOS"]["i32"], base["CPOS"]["i32"], base["LCONVERGEBAND"]["i32"]
    if apos0 == cpos0 and abs(apos0) <= band0:
        problems = [p for p in problems if "not exactly at home" not in p]
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    band = base["LCONVERGEBAND"]["i32"]
    results = {}
    try:
        for preset, frame in SPEED.items():
            link.write(f"X CSPDCMD {preset}", frame)
            mm_s = tp.SPEED_PRESETS_MM_S[preset]
            timeout = DIST_MM / mm_s * 1.5 + 6.0
            legs = {}
            for leg, f, tgt in (("out", OUT, TARGET), ("home", HOME, 0)):
                t0 = monotonic()
                g4._leg(link, res, f"{preset} {leg}", f"X CPOSCMD {tgt}", f, tgt, band, timeout_s=timeout)
                legs[leg] = round(monotonic() - t0, 3)
            measured = DIST_MM / min(legs.values())
            results[preset] = {"preset_mm_s": round(mm_s, 3), "leg_s": legs, "measured_mm_s": round(measured, 3),
                               "ratio": round(measured / mm_s, 3)}
        for name, frame in g4.STOP:
            link.write(name, frame)
    except g4._Fault as f:
        g4._fault_stop(link, res, str(f))
        _restore(link, res)
        res.baseline["speeds"] = results
        return
    _restore(link, res)
    res.baseline["speeds"] = results
    res.final = g4._final(link, band)
    cspd = link.read(g4.XR["CSPDCMD"])["raw"]
    res.final["CSPDCMD_raw_after"] = cspd
    fit = analyse(results)
    res.baseline["fit"] = fit
    bad = fit["problems"]
    failed = [k for k, v in res.final["checks"].items() if not v] + (
        ["CSPDCMD not restored"] if cspd != g4.RAW_15_0 else []) + [f"{p} speed off" for p in bad]
    res.outcome = "fail" if failed else "pass"
    res.message = _summary(fit) + ("; power-up speed restored" if not failed else "; FAILED: " + ", ".join(failed))


def _summary(fit: dict) -> str:
    if "speed_scale" not in fit:
        return "; ".join(fit["problems"])
    return (", ".join(f"{p} {v} mm/s" for p, v in fit["true_mm_s"].items())
            + f" (speed scale {fit['speed_scale']}, fixed overhead {fit['overhead_s']} s per leg, worst residual "
              f"{fit['worst_residual_s']} s)")


def _restore(link, res) -> None:
    try:
        link.write("X CSPDCMD fast (power-up 15.0)", SPEED["fast"])
    except g4._Fault as f:
        res.message += f"; COULD NOT RESTORE THE POWER-UP SPEED ({f}): power-cycle the instrument"


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate10-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate10_speed",
                                 description="Motor Gate 10: verify the three jog speeds on X. Dry run by default.")
    ap.add_argument("--send", action="store_true", help="run it (phrase: MEASURE X SPEEDS ON COMn)")
    ap.add_argument("--instrument-on-2min", action="store_true")
    ap.add_argument("--stage-empty-watching", action="store_true", help="X travel 0..10 mm clear, operator watching")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    ap.add_argument("--reanalyse", type=Path, default=None, metavar="EVIDENCE_JSON",
                    help="re-check a recorded Gate 10 run with the current analysis (opens nothing)")
    a = ap.parse_args(argv)
    if a.reanalyse:
        import json

        rec = json.loads(a.reanalyse.read_text(encoding="utf-8"))
        fit = analyse(rec["baseline"]["speeds"])
        fin = rec.get("final", {})
        failed = [k for k, v in fin.get("checks", {}).items() if not v] + (
            ["CSPDCMD not restored"] if fin.get("CSPDCMD_raw_after") != g4.RAW_15_0 else []) + fit["problems"]
        print(f"[{'fail' if failed else 'pass'} (re-analysed)] {_summary(fit)}"
              + ("" if not failed else "; FAILED: " + ", ".join(failed)))
        return 1 if failed else 0
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              stage_empty_watching=a.stage_empty_watching)
    print(f"[{res.outcome}] {res.message}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
