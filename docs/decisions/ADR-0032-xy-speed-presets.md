# ADR-0032: X/Y jog speed presets (CSPDCMD), behind Motor Gate 10

- **Status:** Accepted (2026-11-13). **Gate 10 passed on X** (`motor-gate10-20261113T165520`, re-analysed): true speeds 1.009 / 4.037 / 8.345 mm/s, scale 1.009. The app enables the Speed box with `--speed-control`. Y uses the same frames and drive program but was not measured (its hardware fault is open).
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate10_speed_spec.md`.

## Context
The Capture joystick offers Slow / Medium / Fast. The drives only ever ran at their power-up speed
(`CSPDCMD` 15.0 = 8.27 mm/s), and no speed write had been reviewed.

## Decision
- **Three fixed values only:** 1.0, 4.0 and 8.27 mm/s (the last is exactly the power-up 15.0). Nothing faster.
- **New write class:** `CSPDCMD` (0x03D6), RAM only, X/Y only. Registered as `motor.x_speed_presets` for Gate 10.
- **Gate 10 first:** it measures the three speeds on X (10 mm out and back each) and restores 15.0.
- **App:** the Speed box works only with `--speed-control`.
  - The backend writes the preset before a move when the drive holds another value.
  - It sizes the move timeout from the actual speed.
  - It restores 15.0 on exit.

## Consequences
- The motor gates' power-up-speed precondition still holds after the app exits normally. After a crash, a power cycle restores it.
- One extra 16 ms read per moving axis before each move.
