# ADR-0019: Motor Gate 4, first movement: X +1 mm and back to 0

- **Status:** Accepted: implemented and tested against a fake drive chain (2026-11-13). **Nothing sent.** Running it on the instrument is a separate operator approval.
- **Builds on:** ADR-0016 to ADR-0018; motor L0 §3a ("Move completion, status and units"); the spec [`fl1_v1.6_motor_gate4_x_move_spec.md`](../evidence/fl1_v1.6_motor_gate4_x_move_spec.md).

## Decision
`python -m openevos.maintenance.motor_gate4_x_move` (phrase `MOVE X 1MM AND BACK ON COMn`; flags `--instrument-on-2min` and `--stage-empty-watching`; dry run by default).

- **Writes:** constants to X only, each after its line sync and each acknowledged by `O`:
  - `CPOSCMD` = 2,268 / 0;
  - `ESTATE` = 8;
  - Gate 3's stop (W1–W3).

  Speed, acceleration and limits are **never** written.
- **Before moving:** readiness on all six axes. On X:
  - `APOS` = `CPOS` = 0;
  - limits covering 0…2,268;
  - `CSPDCMD` = 15.0, `CACCCMD` = 1.0 and `CACCSTOP` = 1.0;
  - 0 < `LCONVERGEBAND` ≤ 50;
  - `SRH` bit 9 set.
- **Each leg:** poll `SRH` and `ESTATEV` every 50 ms for ≤ 3 s, until bit 9 is set and `ESTATEV` = 13. Then require \|`APOS` − target\| ≤ band and `CPOS` = target.
- **Order:** out to +2,268, a 1.5 s pause, back to 0, then the controlled stop (X leaves state 13 for 0), then the final checks.
- **Fault path:** any fault after the first write sends **one** controlled stop and reads the state. **Nothing else** is sent: no return move and no retry. If the stop is not acknowledged, the operator is told to switch the instrument off.
- **Guards:** `assert_allowed` admits only the reviewed reads and these constants. The tool is registered as `REVIEWED_MAINTENANCE["motor.x_move_1mm_and_back"]`. The command core still refuses motor commands.

## First run (2026-11-13): PASS
- **Result:** out to +2,268 and back to 0, with **0 counts error** on both legs (band 5); every write acknowledged; X stopped back to idle; all axes idle; parameters unchanged.
- **Finding:** `ESTATEV` reads 13 while the axis is still moving. `SRH` bit 9 is the real completion signal.
- **Details:** spec §10.

## Not covered
Other axes and targets; speed, acceleration or limit writes; halt; a deliberate stop during motion (next gate); the desktop app.
