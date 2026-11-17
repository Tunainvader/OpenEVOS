"""Power-up record, read-only CLI (ADR-0040 §9) for sessions that run maintenance gates without the app.

Reads X and Y once after a power-up (index-to-hard-stop distance, homing result, holding current at rest, status)
and appends a row to openevos-scans/stage-monitor/power-up-history.csv, the same file the app writes. Prints the
comparison with the previous row: an index jump > 10 counts flags that an encoder moved relative to the mechanics.
No writes exist in this module. Needs only the "on >= 2 minutes" confirmation (reads, like Gate 15 Phase E).
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openevos.backends.technosoft import power_up_record as pur
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate8_t1_runs as t1
from openevos.maintenance import motor_gate15_y_axisoff as g15
from openevos.maintenance.motor_gate1 import (
    ID_QUERY,
    INIT_DONE_MARKER,
    Port,
    PortInfo,
    driver_state_problems,
    find_ftdi_ports,
    open_vendor_port,
)

READS = {ax: {**g15.R[ax], **t1.HOME_READS[ax]} for ax in ("X", "Y")}
ALLOWED_READS = frozenset({ID_QUERY, *(r.frame for r in g4.READINESS), *(r.frame for d in READS.values()
                                                                          for r in d.values())})


def run(*, on_for_2_min: bool = False, folder: Path = Path("openevos-scans") / "stage-monitor",
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports, open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    res = g15.MotorGate15Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                                platform.node(), spec="ADR-0040 §9 power-up record")
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res, []
    res.port = ports[0]
    if not on_for_2_min:
        res.message = "operator has not confirmed the instrument has been on (and homed) for >= 2 minutes"
        return res, []
    try:
        port = open_port(ports[0].device)
    except Exception as exc:  # noqa: BLE001
        res.message = f"could not open {ports[0].device}: {exc} (close the OpenEVOS app first: it records this itself)"
        return res, []
    notes: list[str] = []
    try:
        res.port_state_read_back = port.inspect()
        if driver_state_problems(res.port_state_read_back):
            res.outcome, res.message = "driver_state", "driver state not normal; nothing sent"
            return res, []
        link = g15._Link(port, res, monotonic, sleep, allowed_writes=frozenset(), allowed_reads=ALLOWED_READS)
        try:
            if not g15._connect(link, res):
                return res, []
            for ax in ("X", "Y"):
                if link.value(READS[ax]["IINITONCEDONE"]) != INIT_DONE_MARKER:
                    res.outcome, res.message = "not_homed", f"{ax} is not homed yet; wait for power-on homing"
                    return res, []
            row = pur.take(lambda ax, var: link.fast(READS[ax][var]))
        except g4._Fault as f:
            res.outcome, res.message = "stopped", f"link fault: {f}"
            return res, []
        res.final = row
        notes = pur.append(folder, row, "cli", "power-up (operator)")
        res.outcome, res.message = "recorded", f"appended to {Path(folder) / pur.HISTORY_FILE}"
    finally:
        port.close()
    return res, notes


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_power_up_record",
                                description="Read-only power-up record of X and Y (index distance, holding current).")
    p.add_argument("--instrument-on-2min", action="store_true", help="the EVOS has been on and homed >= 2 minutes")
    p.add_argument("--folder", type=Path, default=Path.cwd() / "openevos-scans" / "stage-monitor")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("needs Windows.", file=sys.stderr)
        return 2
    res, notes = run(on_for_2_min=a.instrument_on_2min, folder=a.folder, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.final:
        f = res.final
        print(f"  index-to-hard-stop: X {f['x_lhomeindexdelta']}, Y {f['y_lhomeindexdelta']} counts")
        print(f"  holding current: X {f['x_iq_mean']} ({f['x_iq_min']}..{f['x_iq_max']}), "
              f"Y {f['y_iq_mean']} ({f['y_iq_min']}..{f['y_iq_max']})")
    for n in notes:
        print(f"  {n}")
    return 0 if res.outcome == "recorded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
