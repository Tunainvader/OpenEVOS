"""Motor Gate 13: F centring test, stepping short of the mirror under brightfield (ADR-0042; L4).

Spec: docs/evidence/fl1_v1.6_motor_gate13_f_centring_spec.md. The first gate that sends an F position: four FIXED
targets (124,000 / 120,000 / 116,000 / 112,000), each a precomputed allow-listed 32-bit LTARGETPOS frame, followed by
ESTATE = 2 (STATE_2_GOTO_POS, the path PearlScope's FAxisMoveToPos uses; the drive clamps to LPOSMIN/LPOSMAX). There
is no position argument. F must start at the mirror (128,000, Gate 12); it dwells DWELL_S at each target for the camera
recorder, then returns with Gate 12's ESTATE = 17. Fault -> ESTATE = 15 and stop where it is. Dry run by default.
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
from openevos.maintenance import motor_gate11_f_cube as g11
from openevos.maintenance import motor_gate12_f_mirror as g12
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

SPEC = "docs/evidence/fl1_v1.6_motor_gate13_f_centring_spec.md; ADR-0042"
FR = g11.FR
GOTO_POS = 2
#: the only F positions this gate can send (counts); >= 10,000 clear of cube 4 (102,000), below the mirror (128,000)
TARGETS = (124000, 120000, 116000, 112000)
DWELL_S = 8.0
MIRROR = g12.EXPECTED_MIRROR_POS


def target_frame(pos: int) -> bytes:
    if pos not in TARGETS:
        raise g4.MotorGate4Refused(f"{pos} is not one of this gate's fixed F targets")
    b = pos.to_bytes(4, "big")
    return g11._frame(0x2400 | (g11._LTARGETPOS & 0x1FF), b[2:4] + b[0:2])


GOTO_POS_FRAME = g11._frame(0x2000 | (g11._ESTATE & 0x1FF), GOTO_POS.to_bytes(2, "big"))
GOTO_MIRR_FRAME, STOP_FRAME = g12.GOTO_MIRR_FRAME, g12.STOP_FRAME
ALLOWED_WRITES = frozenset({*(target_frame(p) for p in TARGETS), GOTO_POS_FRAME, GOTO_MIRR_FRAME, STOP_FRAME})


def confirm_phrase(device: str) -> str:
    return f"STEP F SHORT OF MIRROR ON {device.upper()}"


def _plan() -> list[tuple[str, int, tuple[tuple[str, bytes], ...]]]:
    steps = [(f"P{i}", p, ((f"F LTARGETPOS={p}", target_frame(p)), ("F ESTATE=2 (GOTO_POS)", GOTO_POS_FRAME)))
             for i, p in enumerate(TARGETS, 1)]
    return [*steps, ("back", MIRROR, (("F ESTATE=17 (GOTO_MIRR)", GOTO_MIRR_FRAME),))]


def run(*, send: bool = False, confirm: str = "", on_for_2_min: bool = False, nobody_looking: bool = False,
        dwell_s: float = DWELL_S, find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
    res = g4.MotorGate4Result("refused", "", utcnow().isoformat(timespec="milliseconds"), platform.node(), spec=SPEC)
    if not 1.0 <= dwell_s <= 15.0:
        res.message = "dwell must be 1..15 s (the run has to fit in one 120 s brightfield hold); nothing opened"
        return res
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: F from the mirror ({MIRROR}) to {', '.join(map(str, TARGETS))}, {dwell_s:g} s "
                       f"dwell each, then back to the mirror; phrase {confirm_phrase(dev)!r}; nothing opened or sent")
        res.exchanges = [Exchange(f"{step}: {name}", frame.hex(" ")) for step, _, writes in _plan()
                         for name, frame in writes]
        return res
    if not on_for_2_min or not nobody_looking:
        res.message = ("operator has not confirmed >= 2 min on, and that nobody looks into the light path; "
                       "nothing opened")
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
        reads = {g4.ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for r in FR.values())}
        link = g4._Link(port, res, monotonic, sleep, allowed_writes=ALLOWED_WRITES, allowed_reads=reads)
        _procedure(link, res, dwell_s, monotonic, utcnow)
    finally:
        port.close()
    return res


def _f(link, name):
    return g12._f(link, name)


def _move(link, step, target, writes, band, monotonic) -> dict:
    start = _f(link, "APOS")
    trace = []
    leg = {"target": target, "start": start, "trace": trace}
    for name, frame in writes:
        link.write(f"{step}: {name}", frame)
    t0 = monotonic()
    deadline = t0 + abs(target - start) / g11.SPEED_COUNTS_S_MIN + 5.0
    while True:
        link.sleep(g11.POLL_S)
        flags, apos = _f(link, "ISTATUSFLAGS") & 0xFFFF, _f(link, "APOS")
        trace.append({"t": round(monotonic() - t0, 2), "APOS": apos, "flags": f"0x{flags:04X}"})
        if abs(target - apos) > abs(target - start) + 200:
            raise g4._Fault(f"{step}: F moved away from {target} ({start} -> {apos})")
        if flags & g11.CONVERGED and abs(apos - target) <= band:
            leg["seconds"] = round(monotonic() - t0, 2)
            return leg
        if monotonic() > deadline:
            raise g4._Fault(f"{step}: F did not reach {target} in time (APOS {apos})")


def _procedure(link, res, dwell_s, monotonic, utcnow) -> None:
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
    band = base["LCONVERGEBAND"]
    problems = g12.baseline_problems(base, MIRROR)
    if 0 < band <= 50 and abs(base["APOS"] - MIRROR) > band:
        problems.append(f"F not at the mirror (APOS {base['APOS']}); run Gate 12 --to mirror first")
    if any(not base["LPOSMIN"] <= p <= base["LPOSMAX"] for p in TARGETS):
        problems.append("a fixed target is outside the drive limits")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    try:
        for step, target, writes in _plan():
            leg = _move(link, step, target, writes, band, monotonic)
            leg["converged_utc"] = utcnow().isoformat(timespec="milliseconds")
            res.legs[step] = leg
            link.sleep(dwell_s)
            after = {n: _f(link, n) for n in ("APOS", "MER", "SRH")}
            leg["after_dwell"] = after
            leg["dwell_end_utc"] = utcnow().isoformat(timespec="milliseconds")
            if after["MER"] & 0xFFFF:
                raise g4._Fault(f"{step}: F MER 0x{after['MER'] & 0xFFFF:04X} after the dwell")
            if abs(after["APOS"] - target) > band:
                raise g4._Fault(f"{step}: F drifted to {after['APOS']} during the dwell (target {target})")
    except g4._Fault as f:
        res.fault, res.outcome = str(f), "fault_stopped"
        try:
            link.write("FAULT F ESTATE=15 (STOP)", STOP_FRAME)
            note = "F stop sent and acknowledged; F is NOT at the mirror (recover with Gate 12 --to mirror)"
        except g4._Fault as g:
            note = f"F STOP NOT COMPLETED ({g}); switch the instrument off if anything still moves"
        res.message = f"FAULT: {f}. {note}"
        return
    res.final = {n: _f(link, n) for n in g12.FINAL_READS}
    fin = res.final
    ok = fin["ESTATEV"] == 0 and abs(fin["APOS"] - MIRROR) <= band and not fin["MER"] & 0xFFFF
    res.outcome = "pass" if ok else "fail"
    res.message = (f"F stepped {', '.join(map(str, TARGETS))} ({dwell_s:g} s each) and is back at the mirror: APOS "
                   f"{fin['APOS']}, MER 0x{fin['MER'] & 0xFFFF:04X}; dwell times are in legs.*.converged_utc"
                   if ok else f"FAILED final checks: {fin}")


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate13-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate13_f_centring",
                                 description="Motor Gate 13: step F to four fixed positions short of the mirror, "
                                             "dwell at each, return to the mirror. Dry run by default.")
    ap.add_argument("--send", action="store_true", help="run it (phrase: STEP F SHORT OF MIRROR ON COMn)")
    ap.add_argument("--dwell", type=float, default=DWELL_S, help="seconds at each position (1..15)")
    ap.add_argument("--instrument-on-2min", action="store_true")
    ap.add_argument("--nobody-looking", action="store_true")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = ap.parse_args(argv)
    res = run(send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min, nobody_looking=a.nobody_looking,
              dwell_s=a.dwell)
    print(f"[{res.outcome}] {res.message}")
    for ex in res.exchanges if res.outcome == "dry_run" else ():
        print(f"  would write {ex.what}: {ex.request_hex}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
