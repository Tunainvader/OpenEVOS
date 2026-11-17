# ADR-0018: Motor Gate 3, controlled stop on the idle X axis (first write to a drive)

- **Status:** Accepted: implemented and tested against a fake drive chain (2026-11-13). **Nothing sent.** Running it on the instrument is a separate operator approval.
- **Builds on:** ADR-0016 and ADR-0017; motor L0 §3a; the spec [`fl1_v1.6_motor_gate3_stop_spec.md`](../evidence/fl1_v1.6_motor_gate3_stop_spec.md).

## Decision
`python -m openevos.maintenance.motor_gate3_stop` (phrase `STOP IDLE X AXIS ON COMn`; flags `--instrument-on-2min` and `--stage-clear-watching`; dry run by default).

- **Writes:** exactly three **constant** frames to X, each sent once after its own line sync, and each acknowledged by a single `O` byte or the run stops:
  - `IPENDINGMOVES` = 1 (`06 00 10 21 CD 00 01 05`, the vendor's frame);
  - `CACCSTOP` = 1.0 (`08 00 10 25 E8 00 00 00 01 26`, the value just read);
  - `ESTATE` = 6 (`06 00 10 21 B3 00 06 F0`, `STATE_6_STOP`).
- **Before any write:** the transport and driver-state check of Gate 1 rev 2; all six axes idle and X/Y/Z/F homed; X `APOS` = `CPOS`, inside `LPOSMIN`…`LPOSMAX`, and `CACCSTOP` raw `00 00 00 01`.
- **After the writes (read-only):** X `ESTATEV` polled ≤ 10 × 50 ms; then `APOS`, `CPOS` and `CACCSTOP` re-read, and every axis' `ESTATEV`.
  - **Pass** only if X is idle, the positions are unchanged, `CACCSTOP` is unchanged and all axes are idle.
  - Any failure: **nothing further is sent**: no halt, unhalt, move or retry. Recovery is the operator's power cycle.
- **Guards:** `assert_allowed` admits only the sync byte, the reviewed reads and W1–W3. The tool is registered as `REVIEWED_MAINTENANCE["motor.x_controlled_stop_idle"]`. The command core still refuses every motor command.

## First run (2026-11-13): PASS
- **Writes:** W1–W3 each acknowledged by `O` in about 15 ms.
- **Result:** X `ESTATEV` already 0 at the first poll; `APOS` = `CPOS` = 0 and `CACCSTOP` unchanged before and after; all axes idle.
- **Details:** spec §10.
- **Still unverified:** a stop during motion; it belongs to Gate 4.

## Not covered
Moves, halt/unhalt, other axes, limit/speed/acceleration writes, EEPROM/setup writes, the desktop app.
