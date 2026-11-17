"""Motor Gate 14: F past 128,000 by raising the drive's RAM LPOSMAX in fixed steps (ADR-0043; L4).

Spec: docs/evidence/fl1_v1.6_motor_gate14_f_beyond_mirror_spec.md. The first write to a drive limit: only LPOSMAX
(F RAM user variable 0x03EC, re-set to 128,000 by INIT_ONCE at power-up). Fixed 1,000-count steps 129,000..134,000
(ceiling just inside PearlScope's own limit, ~134,270 drive counts); --up-to picks the last (default 130,000). Each step
writes LPOSMAX = v, then LTARGETPOS = v and ESTATE = 2, so the drive's clamp still bounds the move to v. A free reference
move (128,000 -> 124,000 -> 128,000) sets the peak-current threshold. Stop on: timeout, ISTATUSFLAGS 0x0101 (the
drive's over-current / position-error ISRs, which CLEAR their MER bits), MER, SRH LSP, peak |IQ| > 2x reference,
wrong-way or drift. Always restore LPOSMAX = 128,000 (read back); after a pass F returns to 128,000 first.
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
from openevos.maintenance import motor_gate11_f_cube as g11
from openevos.maintenance import motor_gate12_f_mirror as g12
from openevos.maintenance import motor_gate13_f_centring as g13
from openevos.maintenance.motor_gate1 import (
    BAUD_PASSES,
    INIT_DONE_MARKER,
    Exchange,
    Port,
    PortInfo,
    build_read,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

SPEC = "docs/evidence/fl1_v1.6_motor_gate14_f_beyond_mirror_spec.md; ADR-0043"
_LPOSMAX = 0x03EC
OP_READ16 = 0xB004
#: F program variables (variables.cfg): peak IQ since the last STATE_2_GOTO_POS (UPDATECURRENTMAXMIN, 0x46BA)
EXTRA_READS = {v: g2.Read("F", g11.F_ID, v, "INT", a, build_read(g11.F_ID, OP_READ16, a))
               for v, a in (("IIQMAX", 0x03FA), ("IIQMIN", 0x03FB))}
FR = {**g11.FR, **EXTRA_READS}
STEPS = (129000, 130000, 131000, 132000, 133000, 134000)
DEFAULT_UP_TO = 130000
MIRROR = g12.EXPECTED_MIRROR_POS
REF_TARGET = 124000  # Gate 13's first target: a free move inside the reviewed range
ISR_FLAGS = 0x0101  # _INT2_ISR sets 0x0001, _INT3_ISR sets 0x0100 (both clear their MER bit)
LSP_BIT = 1 << 6  # SRH bit 6: positive limit switch event
CURRENT_FACTOR = 2.0
DWELL_S = 8.0


def _w32(addr: int, value: int) -> bytes:
    b = value.to_bytes(4, "big")
    return g11._frame(0x2400 | (addr & 0x1FF), b[2:4] + b[0:2])


def limit_frame(value: int) -> bytes:
    if value != MIRROR and value not in STEPS:
        raise g4.MotorGate4Refused(f"LPOSMAX {value} is not one of this gate's fixed values")
    return _w32(_LPOSMAX, value)


def target_frame(value: int) -> bytes:
    if value not in STEPS:
        raise g4.MotorGate4Refused(f"{value} is not one of this gate's fixed F steps")
    return _w32(g11._LTARGETPOS, value)


RESTORE_FRAME = limit_frame(MIRROR)
REF_FRAME = g13.target_frame(REF_TARGET)
GOTO_POS_FRAME, GOTO_MIRR_FRAME, STOP_FRAME = g13.GOTO_POS_FRAME, g12.GOTO_MIRR_FRAME, g12.STOP_FRAME
ALLOWED_WRITES = frozenset({*(limit_frame(v) for v in STEPS), RESTORE_FRAME, *(target_frame(v) for v in STEPS),
                            REF_FRAME, GOTO_POS_FRAME, GOTO_MIRR_FRAME, STOP_FRAME})


def confirm_phrase(device: str, up_to: int) -> str:
    return f"RAISE F LIMIT TO {up_to} ON {device.upper()}"


def _steps(up_to: int) -> tuple[int, ...]:
    return tuple(v for v in STEPS if v <= up_to)


def _plan(up_to: int) -> list[tuple[str, bytes]]:
    out = [("ref-out: F LTARGETPOS=124000", REF_FRAME), ("ref-out: F ESTATE=2 (GOTO_POS)", GOTO_POS_FRAME),
           ("ref-back: F ESTATE=17 (GOTO_MIRR)", GOTO_MIRR_FRAME)]
    for v in _steps(up_to):
        out += [(f"S{v}: F LPOSMAX={v}", limit_frame(v)), (f"S{v}: F LTARGETPOS={v}", target_frame(v)),
                (f"S{v}: F ESTATE=2 (GOTO_POS)", GOTO_POS_FRAME)]
    return out + [("back: F ESTATE=17 (GOTO_MIRR)", GOTO_MIRR_FRAME),
                  ("restore: F LPOSMAX=128000", RESTORE_FRAME)]


def run(*, up_to: int = DEFAULT_UP_TO, send: bool = False, confirm: str = "", on_for_2_min: bool = False,
        nobody_looking: bool = False, dwell_s: float = DWELL_S,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
    res = g4.MotorGate4Result("refused", "", utcnow().isoformat(timespec="milliseconds"), platform.node(), spec=SPEC)
    if up_to not in STEPS:
        res.message = f"--up-to must be one of {', '.join(map(str, STEPS))}; nothing opened"
        return res
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
        res.message = (f"dry run: reference move, then F LPOSMAX raised and F moved to "
                       f"{', '.join(map(str, _steps(up_to)))} ({dwell_s:g} s dwell each), back to the mirror, "
                       f"LPOSMAX restored to 128000; phrase {confirm_phrase(dev, up_to)!r}; nothing opened or sent")
        res.exchanges = [Exchange(name, frame.hex(" ")) for name, frame in _plan(up_to)]
        return res
    if not on_for_2_min or not nobody_looking:
        res.message = ("operator has not confirmed >= 2 min on, and that nobody looks into the light path; "
                       "nothing opened")
        return res
    if confirm != confirm_phrase(dev, up_to):
        res.message = (f"confirmation phrase missing or wrong (expected {confirm_phrase(dev, up_to)!r}); "
                       "nothing opened")
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
        _procedure(link, res, up_to, dwell_s, monotonic, utcnow)
    finally:
        port.close()
    return res


def _f(link, name):
    v = link.read(FR[name])
    return v.get("i32", v.get("i16"))


def _peak(link) -> int:
    return max(abs(_f(link, "IIQMAX")), abs(_f(link, "IIQMIN")))


def _move(link, step, target, writes, band, monotonic) -> dict:
    start = _f(link, "APOS")
    trace = []
    leg = {"target": target, "start": start, "trace": trace}
    for name, frame in writes:
        link.write(name, frame)
    t0 = monotonic()
    deadline = t0 + abs(target - start) / g11.SPEED_COUNTS_S_MIN + 5.0
    while True:
        link.sleep(g11.POLL_S)
        flags, apos = _f(link, "ISTATUSFLAGS") & 0xFFFF, _f(link, "APOS")
        trace.append({"t": round(monotonic() - t0, 2), "APOS": apos, "flags": f"0x{flags:04X}"})
        if flags & ISR_FLAGS:
            raise g4._Fault(f"{step}: drive protection fired (ISTATUSFLAGS 0x{flags:04X}) at APOS {apos}")
        if abs(target - apos) > abs(target - start) + 200:
            raise g4._Fault(f"{step}: F moved away from {target} ({start} -> {apos})")
        if flags & g11.CONVERGED and abs(apos - target) <= band:
            leg["seconds"] = round(monotonic() - t0, 2)
            leg["peak_iq"] = _peak(link)
            return leg
        if monotonic() > deadline:
            raise g4._Fault(f"{step}: F did not reach {target} in time (APOS {apos}); possible hard stop")


def _restore(link, res) -> str:
    try:
        link.write("restore: F LPOSMAX=128000", RESTORE_FRAME)
        back = _f(link, "LPOSMAX")
        res.legs["restore"] = {"LPOSMAX": back}
        return "LPOSMAX restored to 128000" if back == MIRROR else f"LPOSMAX READ BACK {back}, NOT 128000"
    except g4._Fault as g:
        return f"LPOSMAX RESTORE NOT CONFIRMED ({g}); a power cycle resets it to 128000"


def _procedure(link, res, up_to, dwell_s, monotonic, utcnow) -> None:
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
    if base["ISTATUSFLAGS"] & ISR_FLAGS:
        problems.append(f"ISTATUSFLAGS 0x{base['ISTATUSFLAGS'] & 0xFFFF:04X}: a drive protection flag is set")
    if problems:
        res.outcome, res.message = "baseline_refused", "; ".join(problems) + "; nothing written"
        return
    try:
        out = _move(link, "ref-out", REF_TARGET, (("ref-out: F LTARGETPOS=124000", REF_FRAME),
                                                   ("ref-out: F ESTATE=2 (GOTO_POS)", GOTO_POS_FRAME)), band, monotonic)
        back = _move(link, "ref-back", MIRROR, (("ref-back: F ESTATE=17 (GOTO_MIRR)", GOTO_MIRR_FRAME),),
                     band, monotonic)
        iref = max(out["peak_iq"], back["peak_iq"])
        res.legs["reference"] = {"out": out, "back": back, "iref": iref,
                                 "threshold": round(CURRENT_FACTOR * iref, 1)}
        if iref <= 0:
            raise g4._Fault("reference move recorded no motor current; cannot set the contact threshold")
        for v in _steps(up_to):
            step = f"S{v}"
            link.write(f"{step}: F LPOSMAX={v}", limit_frame(v))
            leg = _move(link, step, v, ((f"{step}: F LTARGETPOS={v}", target_frame(v)),
                                        (f"{step}: F ESTATE=2 (GOTO_POS)", GOTO_POS_FRAME)), band, monotonic)
            leg["converged_utc"] = utcnow().isoformat(timespec="milliseconds")
            res.legs[step] = leg
            if leg["peak_iq"] > CURRENT_FACTOR * iref:
                raise g4._Fault(f"{step}: peak |IQ| {leg['peak_iq']} > {CURRENT_FACTOR:g} x reference {iref}; "
                                "possible contact")
            link.sleep(dwell_s)
            after = {n: _f(link, n) for n in ("APOS", "MER", "SRH", "ISTATUSFLAGS")}
            leg["after_dwell"] = after
            leg["dwell_end_utc"] = utcnow().isoformat(timespec="milliseconds")
            if after["MER"] & 0xFFFF or after["ISTATUSFLAGS"] & ISR_FLAGS:
                raise g4._Fault(f"{step}: MER 0x{after['MER'] & 0xFFFF:04X} / ISTATUSFLAGS "
                                f"0x{after['ISTATUSFLAGS'] & 0xFFFF:04X} after the dwell")
            if after["SRH"] & LSP_BIT:
                raise g4._Fault(f"{step}: positive limit switch event (SRH 0x{after['SRH'] & 0xFFFF:04X})")
            if abs(after["APOS"] - v) > band:
                raise g4._Fault(f"{step}: F drifted to {after['APOS']} during the dwell (target {v})")
        res.legs["back"] = _move(link, "back", MIRROR, (("back: F ESTATE=17 (GOTO_MIRR)", GOTO_MIRR_FRAME),),
                                 band, monotonic)
    except g4._Fault as f:
        res.fault, res.outcome = str(f), "fault_stopped"
        try:
            link.write("FAULT F ESTATE=15 (STOP)", STOP_FRAME)
            note = "F stop sent and acknowledged; F left where it stopped (recover with Gate 12 --to mirror)"
        except g4._Fault as g:
            note = f"F STOP NOT COMPLETED ({g}); switch the instrument off if anything still moves"
        res.message = f"FAULT: {f}. {note}. {_restore(link, res)}"
        return
    restored = _restore(link, res)
    res.final = {n: _f(link, n) for n in (*g12.FINAL_READS, "LPOSMAX")}
    fin = res.final
    ok = (fin["ESTATEV"] == 0 and abs(fin["APOS"] - MIRROR) <= band and not fin["MER"] & 0xFFFF
          and fin["LPOSMAX"] == MIRROR)
    res.outcome = "pass" if ok else "fail"
    peaks = ", ".join(f"{v}: {res.legs[f'S{v}']['peak_iq']}" for v in _steps(up_to))
    res.message = (f"F reached {', '.join(map(str, _steps(up_to)))} and is back at the mirror (APOS {fin['APOS']}); "
                   f"{restored}; peak |IQ| ref {res.legs['reference']['iref']}, steps {peaks}"
                   if ok else f"FAILED final checks: {fin}; {restored}")


def write_evidence(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate14-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate14_f_beyond_mirror",
                                 description="Motor Gate 14: raise F's RAM LPOSMAX in fixed 1,000-count steps and "
                                             "move F past 128,000; always restores 128,000. Dry run by default.")
    ap.add_argument("--up-to", type=int, choices=STEPS, default=DEFAULT_UP_TO)
    ap.add_argument("--send", action="store_true", help="run it (phrase: RAISE F LIMIT TO <up-to> ON COMn)")
    ap.add_argument("--dwell", type=float, default=DWELL_S, help="seconds at each step (1..15)")
    ap.add_argument("--instrument-on-2min", action="store_true")
    ap.add_argument("--nobody-looking", action="store_true")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--evidence-dir", type=Path, default=Path("openevos-scans"))
    a = ap.parse_args(argv)
    res = run(up_to=a.up_to, send=a.send, confirm=a.confirm, on_for_2_min=a.instrument_on_2min,
              nobody_looking=a.nobody_looking, dwell_s=a.dwell)
    print(f"[{res.outcome}] {res.message}")
    for ex in res.exchanges if res.outcome == "dry_run" else ():
        print(f"  would write {ex.what}: {ex.request_hex}")
    if res.outcome not in ("refused", "dry_run"):
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir).resolve()}")
    return 0 if res.outcome in ("pass", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
