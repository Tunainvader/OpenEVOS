"""Motor Gate 12: move the F carriage (epi carriage, axis 5) to the mirror, or back to cube slot 1 (ADR-0041; L4).

Spec: docs/evidence/fl1_v1.6_motor_gate12_f_mirror_spec.md. Uses the drive's own STATE_17_GOTO_MIRR (offline decode
of FAxis.sw @0x41A8: LTARGETPOS = LMIRROR1POS, ESTATEV = 2, GOTO STATE_2_GOTO_POS). The PC writes only ESTATE = 17
and never a position; the drive moves to its stored LMIRROR1POS (128,000 counts, read back before moving), clamped
to its LPOSMIN/LPOSMAX. `--to slot1` returns via Gate 11's reviewed GOTO_CUBE frames (LTARGETPOS = 1, ESTATE = 16).
Completion = ISTATUSFLAGS bit 0x4000 (converged) with APOS within LCONVERGEBAND of the stored target.
Fault -> F's own STATE_15_STOP (ESTATE = 15). Writes: ESTATE 17; LTARGETPOS 1 + ESTATE 16; ESTATE 15. Dry run by default.
"""  # noqa: E501

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate11_f_cube as g11
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

SPEC = "docs/evidence/fl1_v1.6_motor_gate12_f_mirror_spec.md; ADR-0041"
FR = g11.FR  # F's reads (status registers + program variables, Gate 9 rev 3)
GOTO_MIRR = 17
#: the drive's stored mirror position and limits (FAxis.sw INIT_ONCE constants, read back on 2026-11-13); the gate
#: refuses if the drive holds anything else. The mirror sits exactly on LPOSMAX, so the clamp cannot move it.
EXPECTED_MIRROR_POS = 128000
EXPECTED_LIMITS = (0, 128000)

GOTO_MIRR_FRAME = g11._frame(0x2000 | (g11._ESTATE & 0x1FF), GOTO_MIRR.to_bytes(2, "big"))
SLOT1_FRAME = g11.slot_frame(1)
GOTO_CUBE_FRAME, STOP_FRAME = g11.GOTO_FRAME, g11.STOP_FRAME
ALLOWED_WRITES = frozenset({GOTO_MIRR_FRAME, SLOT1_FRAME, GOTO_CUBE_FRAME, STOP_FRAME})

#: destination -> (label, stored-position variable, writes in order)
TARGETS = {
    "mirror": ("the mirror", "LMIRROR1POS", (("F ESTATE=17 (GOTO_MIRR)", GOTO_MIRR_FRAME),)),
    "slot1": (f"cube slot 1 ({g11.CUBE_NAMES[1]})", "LCUBE1POS",
              (("F LTARGETPOS=1", SLOT1_FRAME), ("F ESTATE=16 (GOTO_CUBE)", GOTO_CUBE_FRAME))),
}
FINAL_READS = ("ESTATEV", "APOS", "CPOS", "LTARGETPOSV", "ISTATUSFLAGS", "MER", "SRL", "SRH")


def confirm_phrase(device: str, to: str) -> str:
    return f"MOVE F TO MIRROR ON {device.upper()}" if to == "mirror" else f"RETURN F TO CUBE 1 ON {device.upper()}"


def run(*, to: str = "mirror", send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        nobody_looking: bool = False, find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g4.MotorGate4Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              platform.node(), spec=SPEC)
    if to not in TARGETS:
        res.message = "destination must be 'mirror' or 'slot1'; nothing opened"
        return res
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    label, var, writes = TARGETS[to]
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: F to {label} via the drive's stored {var} "
                       f"({EXPECTED_MIRROR_POS if to == 'mirror' else g11.EXPECTED_CUBE_POS[1]} expected); "
                       f"phrase {confirm_phrase(dev, to)!r}; nothing opened or sent")
        res.exchanges = [Exchange(name, frame.hex(" ")) for name, frame in writes]
        return res
    if not on_for_2_min or not nobody_looking:
        res.message = ("operator has not confirmed >= 2 min on, and that nobody looks into the light path (the "
                       "carriage passes the DAPI cube, near-UV); nothing opened")
        return res
    if confirm != confirm_phrase(dev, to):
        res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev, to)!r}); nothing opened"
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
        reads = {g4.ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in FR.values())}
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=reads)
        _procedure(link, res, to, monotonic)
    finally:
        port.close()
    return res


def _f(link, name):
    v = link.read(FR[name])
    return v.get("i32", v.get("i16"))


def baseline_problems(base: dict, target: int) -> list[str]:
    band = base["LCONVERGEBAND"]
    problems = []
    if any(base[f"LCUBE{n}POS"] != p for n, p in g11.EXPECTED_CUBE_POS.items()):
        problems.append("the drive's stored cube positions are not the reviewed 0/34000/68000/102000")
    if base["LMIRROR1POS"] != EXPECTED_MIRROR_POS:
        problems.append(f"the drive's stored mirror position {base['LMIRROR1POS']} is not the reviewed "
                        f"{EXPECTED_MIRROR_POS}")
    if (base["LPOSMIN"], base["LPOSMAX"]) != EXPECTED_LIMITS:
        problems.append(f"F limits {base['LPOSMIN']}..{base['LPOSMAX']} are not the reviewed 0..128000")
    if not base["LPOSMIN"] <= target <= base["LPOSMAX"]:
        problems.append(f"target {target} outside the drive limits")
    if base["APOS"] != base["CPOS"] and abs(base["APOS"] - base["CPOS"]) > band:
        problems.append(f"F not at rest (APOS {base['APOS']}, CPOS {base['CPOS']})")
    if base["MER"] & 0xFFFF:
        problems.append(f"F MER 0x{base['MER'] & 0xFFFF:04X} not clear")
    if not 0 < band <= 50:
        problems.append(f"LCONVERGEBAND {band} not sane")
    return problems


def _procedure(link, res, to, monotonic) -> None:
    label, var, writes = TARGETS[to]
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
        names = ("ESTATEV", "IINITONCEDONE", "APOS", "CPOS", "LPOSMIN", "LPOSMAX", "LCUBE1POS", "LCUBE2POS",
                 "LCUBE3POS", "LCUBE4POS", "LMIRROR1POS", "LCONVERGEBAND", "MER", "ISTATUSFLAGS")
        base = {n: _f(link, n) for n in names}
    except g4._Fault as f:
        res.outcome, res.message = "stopped", f"{f}; nothing written"
        return
    res.baseline = dict(base)
    target, band = base[var], base["LCONVERGEBAND"]
    problems = baseline_problems(base, target)
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    start = base["APOS"]
    trace = []
    res.legs["goto"] = {"to": to, "target": target, "start": start, "trace": trace}
    try:
        for name, frame in writes:
            link.write(name, frame)
        t0 = monotonic()
        deadline = t0 + abs(target - start) / g11.SPEED_COUNTS_S_MIN + 5.0
        while True:
            link.sleep(g11.POLL_S)
            flags, apos = _f(link, "ISTATUSFLAGS") & 0xFFFF, _f(link, "APOS")
            trace.append({"t": round(monotonic() - t0, 2), "APOS": apos, "flags": f"0x{flags:04X}"})
            if abs(target - apos) > abs(target - start) + 200:
                raise g4._Fault(f"F moved away from {label} ({start} -> {apos}, target {target})")
            if flags & g11.CONVERGED and abs(apos - target) <= band:
                break
            if monotonic() > deadline:
                raise g4._Fault(f"F did not reach {label} in time (APOS {apos}, target {target})")
    except g4._Fault as f:
        res.fault, res.outcome = str(f), "fault_stopped"
        try:
            link.write("FAULT F ESTATE=15 (STOP)", STOP_FRAME)
            note = "F stop sent and acknowledged"
        except g4._Fault as g:
            note = f"F STOP NOT COMPLETED ({g}); switch the instrument off if anything still moves"
        res.message = f"FAULT: {f}. {note}"
        return
    res.final = {n: _f(link, n) for n in FINAL_READS}
    res.legs["goto"]["seconds"] = round(monotonic() - t0, 2)
    fin = res.final
    ok = fin["ESTATEV"] == 0 and abs(fin["APOS"] - target) <= band and not fin["MER"] & 0xFFFF
    res.outcome = "pass" if ok else "fail"
    res.message = (f"F at {label}: APOS {fin['APOS']} (target {target}) in {res.legs['goto']['seconds']} s; "
                   f"ESTATEV {fin['ESTATEV']}, ISTATUSFLAGS 0x{fin['ISTATUSFLAGS'] & 0xFFFF:04X}, "
                   f"MER 0x{fin['MER'] & 0xFFFF:04X}, SRH 0x{fin['SRH'] & 0xFFFF:04X}"
                   if ok else f"FAILED final checks: {fin}")


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate12-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate12_f_mirror",
                                 description="Motor Gate 12: move the F carriage to the mirror, or back to cube "
                                             "slot 1. Dry run by default.")
    ap.add_argument("--to", choices=tuple(TARGETS), default="mirror")
    ap.add_argument("--send", action="store_true",
                    help="run it (phrase: MOVE F TO MIRROR ON COMn, or RETURN F TO CUBE 1 ON COMn)")
    ap.add_argument("--instrument-on-2min", action="store_true")
    ap.add_argument("--nobody-looking", action="store_true",
                    help="nobody looks into the light path (the carriage passes the near-UV DAPI cube)")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = ap.parse_args(argv)
    res = run(to=a.to, send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              nobody_looking=a.nobody_looking)
    print(f"[{res.outcome}] {res.message}")
    for ex in res.exchanges if res.outcome == "dry_run" else ():
        print(f"  would write {ex.what}: {ex.request_hex}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
