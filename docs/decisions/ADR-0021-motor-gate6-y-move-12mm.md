# ADR-0021: Motor Gate 6, first Y movement: +12 mm and back to 0

- **Status:** Accepted: implemented and tested against a fake drive chain (2026-11-13). **Nothing sent.**
- **Builds on:** ADR-0019 and ADR-0020; the spec [`fl1_v1.6_motor_gate6_y_move_spec.md`](../evidence/fl1_v1.6_motor_gate6_y_move_spec.md).

## Decision
- **Gate 4's machinery is made axis-generic** (`AxisPlan`, `axis_reads`; per-link read and write allowlists) without changing its behaviour; Gates 4 and 5 still pass.
- `python -m openevos.maintenance.motor_gate6_y_move` (phrase `MOVE Y 12MM AND BACK ON COMn`) runs Gate 4's sequence on Y, with Y-only constants:
  - `CPOSCMD` = 27,213 / 0;
  - `ESTATE` = 8;
  - the Y stop.

  The leg timeout is 5 s. Y's program is identical to X's in its state machine, and the variable addresses are equal.
- **Registry:** `REVIEWED_MAINTENANCE["motor.y_move_12mm_and_back"]`.

## First run (2026-11-13): PASS
Y moved to +27,213 (12 mm) and back to 0 with 0 counts error on both legs (band 5). The Y stop returned Y to idle; all axes idle; parameters unchanged. Details: spec §6.
