"""Power-up record for X and Y (ADR-0040 §9): read-only, once per homing.

Two indirect checks on the Y fault, since these closed-loop steppers have no rotor sensor apart from the encoder:

* **Index audit (plan v3 T4, §6.7).** `LHOMEINDEXDELTA` is the distance from the homing hard stop to the encoder's
  index mark. It should repeat between power cycles. A jump of more than 10 counts on one axis means the encoder
  (or its disk) moved relative to the mechanics.
* **Holding current.** `IQ` at rest, sampled over ~0.5 s. With `APOS` = `CPOS`, a larger holding current than the
  other axis' means a load on the carriage or a commutation phase error (torque falls as cos(phase error)).

`take()` reads; `append()` writes one row to `power-up-history.csv` and returns notes comparing it with the last
row. Used by the app's telemetry thread (at connect and after each observed re-home) and by the read-only CLI
`maintenance/motor_power_up_record.py`. Qt-free.
"""

from __future__ import annotations

import csv
import statistics
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

HISTORY_FILE = "power-up-history.csv"
INDEX_JUMP_COUNTS = 10  # plan v3 §6.7
IQ_SAMPLES = 20
AXES = ("X", "Y")
COLUMNS = ("utc", "source", "trigger",
           *(f"{ax.lower()}_{k}" for ax in AXES for k in (
               "lhomeindexdelta", "homedone", "iq_mean", "iq_min", "iq_max", "apos", "cpos", "istatusflags", "srl")))


def _signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def take(read: Callable[[str, str], int]) -> dict:
    """One record. `read(axis, var)` returns the raw value (16-bit unsigned or 32-bit signed)."""
    row: dict = {}
    for ax in AXES:
        a = ax.lower()
        row[f"{a}_lhomeindexdelta"] = read(ax, "LHOMEINDEXDELTA")
        row[f"{a}_homedone"] = read(ax, "HOMEDONE")
        row[f"{a}_apos"], row[f"{a}_cpos"] = read(ax, "APOS"), read(ax, "CPOS")
        row[f"{a}_istatusflags"], row[f"{a}_srl"] = f"0x{read(ax, 'ISTATUSFLAGS'):04X}", f"0x{read(ax, 'SRL'):04X}"
    iq: dict[str, list[int]] = {ax: [] for ax in AXES}
    for _ in range(IQ_SAMPLES):  # interleaved, so both axes span the same ~0.5 s
        for ax in AXES:
            iq[ax].append(_signed16(read(ax, "IQ")))
    for ax in AXES:
        a = ax.lower()
        row[f"{a}_iq_mean"] = round(statistics.fmean(iq[ax]), 1)
        row[f"{a}_iq_min"], row[f"{a}_iq_max"] = min(iq[ax]), max(iq[ax])
    return row


def history(folder: Path) -> list[dict]:
    p = Path(folder) / HISTORY_FILE
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def homing_faulted(row: dict, ax: str) -> bool:
    """True if the row's ISTATUSFLAGS has bit 0 (over-current trip) set for the axis: the drive switched off during
    or after homing, so its LHOMEINDEXDELTA is not a homing result (2026-11-21: X tripped while homing and read 0)."""
    try:
        return bool(int(str(row[f"{ax.lower()}_istatusflags"]), 0) & 0x1)
    except (KeyError, ValueError):
        return False


def _reference(ax: str, previous: list[dict]) -> dict | None:
    """The newest earlier row whose homing on this axis did not fault."""
    return next((r for r in reversed(previous) if not homing_faulted(r, ax)), None)


def compare(row: dict, previous: dict | list[dict] | None) -> list[str]:
    """Notes for the operator: an index jump (> 10 counts) and the holding currents side by side.

    `previous` is the earlier history (or one row). Per axis, the reference is the newest earlier row whose homing
    did not fault; a faulted homing in this row is reported instead of compared."""
    notes = []
    earlier = previous if isinstance(previous, list) else [previous] if previous else []
    for ax in AXES:
        k = f"{ax.lower()}_lhomeindexdelta"
        if homing_faulted(row, ax):
            notes.append(f"{ax} drive over-current flag set at this record: its homing index distance "
                         f"({row.get(k)}) is not compared and not used as a reference")
            continue
        ref = _reference(ax, earlier)
        if ref is None:
            continue
        try:
            d = int(row[k]) - int(ref[k])
        except (KeyError, ValueError):
            continue
        if abs(d) > INDEX_JUMP_COUNTS:
            notes.append(f"{ax} index-to-hard-stop distance changed {d:+d} counts since the last good homing "
                         f"({ref[k]} -> {row[k]}): the {ax} encoder may have moved relative to the mechanics")
    x, y = abs(float(row["x_iq_mean"])), abs(float(row["y_iq_mean"]))
    notes.append(f"holding current at rest: X {row['x_iq_mean']}, Y {row['y_iq_mean']}"
                 + (f" (Y {y / x:.1f}x X)" if x >= 1 else ""))
    return notes


def append(folder: Path, row: dict, source: str, trigger: str) -> list[str]:
    """Append the row (with time, source and trigger); return compare() against the earlier history."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    prev = history(folder)
    full = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source, "trigger": trigger,
            **row}
    p = folder / HISTORY_FILE
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: full.get(k, "") for k in COLUMNS})
    return compare(row, prev)
