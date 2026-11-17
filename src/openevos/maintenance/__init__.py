"""Operator-run maintenance reads (ADR-0009 L3, ADR-0011).

This package is deliberately outside the command core and the UI: the core quarantines a board
in bootloader mode and sends it nothing, and the import guard keeps `core/`, `hal/` and `ui/`
from importing this package. Each tool here performs one reviewed read, only after an explicit
operator confirmation, and writes its raw bytes as evidence.

* `led_bootloader` — Gate 1: the version query `0x0A` to the LED board bootloader `0DAD:0057`.
* `win_hid`        — minimal Windows HID transport (overlapped I/O, 500 ms limits, no retry).
"""
