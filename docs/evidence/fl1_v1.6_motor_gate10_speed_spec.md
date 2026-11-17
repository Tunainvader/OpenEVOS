# Motor Gate 10: X/Y jog speed presets (CSPDCMD)

ADR-0032. Level L4 (a new write class, RAM only). Module `src/openevos/maintenance/motor_gate10_speed.py`.

## Why

The app's joystick has a Slow / Medium / Fast choice. Until now OpenEVOS never wrote a speed: the drives run at
their power-up `CSPDCMD` = 15.0 (8.27 mm/s), and every motor gate requires that value at its start.

## What is written (Strongly supported)

| Item | Evidence |
|---|---|
| `CSPDCMD` = 0x03D6, 32-bit 16.16 fixed, same address on X and Y | `variables.cfg` (Verified); Gate 2 reads it |
| The drive program loads it at every move start: `STATE_8_MOVE_POS` / `MOVEPOSAGAIN` do `CSPD = CSPDCMD` | `.sw` decode (ADR-0030 spec) |
| Units: counts per 0.8 ms sample. 15.0 → 15 / 0.0008 / 2,267.7 = **8.27 mm/s** | Gate 5 measured ≈ 8.3 mm/s (Strongly supported) |
| The vendor writes it before a move (`25 D6`, only when changed) | `FUN_180030510` decode |
| RAM only: `VAR_INIT` sets 15.0 at power-on | `.sw` decode (Verified address `0x4074`) |

## The only three values (frames for X; Y uses `00 20` and its own checksum)

| Preset | mm/s | `CSPDCMD` | Frame |
|---|---|---|---|
| slow | 1.0 | 1.8142 | **`08 00 10 25 D6 D0 6E 00 01 52`** |
| medium | 4.0 | 7.2567 | **`08 00 10 25 D6 41 B7 00 07 12`** |
| fast | 8.27 | 15.0 (power-up) | **`08 00 10 25 D6 00 00 00 0F 22`** |

Nothing above the power-up speed exists. `is_motion_write` accepts only these frames; any other `25 D6` value
is refused before it reaches the port.

## Gate 10 procedure (on X)

1. Dry run by default. With `--send --instrument-on-2min --stage-empty-watching --confirm "MEASURE X SPEEDS ON COMn"`:
   Gate 1 rev 2 transport, readiness, X baseline (Gate 4 rules, including `CSPDCMD` = 15.0; for this timing gate
   X may start at rest (`APOS` = `CPOS`, as the verified stop leaves it) within `LCONVERGEBAND` of 0).
2. For slow, medium, fast: write the speed frame, move X 0 → +10 mm → 0 with Gate 4's leg (completion,
   drift interlock, abandon detection), timing each leg.
3. Write the power-up 15.0 back, verified stop, final checks, read back `CSPDCMD` = 15.0.
4. On any fault: one controlled stop, then 15.0 is written back.

*Pass (analysis rev 2):* every leg within the settle band; a straight-line fit of leg time against 1/preset gives
slope = 10 mm / speed_scale and intercept = fixed overhead (the gate's own reads, poll interval, acceleration).
Required: speed_scale 0.85–1.05 (never more than 5 % faster than set), overhead 0–6 s, every leg within 0.3 s of the
fit; `CSPDCMD` back to 15.0. (Rev 1 divided 10 mm by the whole leg time and wrongly failed the real run.)

## Result (2026-11-13, `motor-gate10-20261113T165520`) — **pass on re-analysis**

| Preset | Leg times out / home | True speed (fit) |
|---|---|---|
| slow | 12.843 / 12.828 s | 1.009 mm/s |
| medium | 5.390 / 5.375 s | 4.037 mm/s |
| fast | 4.125 / 4.156 s | 8.345 mm/s |

Speed scale **1.0094**, fixed overhead 2.925 s per leg, worst residual 0.032 s. All legs within the settle band;
final checks all true; `CSPDCMD` read back 15.0. The run itself printed `fail` under the rev 1 criterion; the
recorded leg times were re-checked with `--reanalyse` (nothing moved). **Verified.**

## In the app (after Gate 10 passes)

- `--speed-control` enables the Speed box for the real stage (off by default).
- Before a move the backend reads each moving axis' `CSPDCMD` and writes the chosen preset only if it differs.
- The move timeout is sized from the speed actually set.
- On exit the app writes 15.0 back, so the motor gates find the power-up value.

## Hazards

| ID | Hazard | Control |
|---|---|---|
| G10-1 | An arbitrary speed is written | Closed set of 3 frames; builder refuses anything else; tests |
| G10-2 | Speed left non-default after the app | Restore 15.0 on exit and after any Gate 10 fault; power-off also restores it |
| G10-3 | A slow move times out | Timeout sized from the speed read from the drive |
