# ADR-0024: Physical X/Y stage Stop and fixed-step jog in the desktop app

- **Status:** Accepted (2026-11-13). Implemented and tested against a simulated drive chain. **Not yet run against the instrument from the app.**
- **Builds on:**
  - ADR-0023 (read-only motor telemetry);
  - ADR-0018 to ADR-0021, which verified on the hardware every frame used here: the controlled stop at rest and in motion, target and move request, completion by `SRH` bit 9, and the encoder settle band.

## Decision
**A session may opt in** (`--motor-telemetry PORT --motor-control`) to exactly two operations on the physical **X/Y stage** (`motor.stage`):
- **Stop:**
  - the drive's controlled stop (`IPENDINGMOVES` = 1, `CACCSTOP` = 1.0, `ESTATE` = 6) on X then Y;
  - available as the page's **Stop stage** button (`CoreGateway.motor_stop` → `router.stop_device`) and as the global **STOP (Esc)** (`router.stop_all` → `halt()`);
  - cancel of a running jog uses the same stop.
- **Jog:**
  - one axis, a fixed step of ±0.1, 1 or 5 mm (`CoreGateway.motor_jog`), resolved from the store's observed position into a `stage.move_absolute`;
  - **submitted through the unchanged router and SafetyManager.** These require: a VERIFIED capability record citing the gate evidence; the PC travel box **0…22.0 mm** on X and Y (the drives' power-up limits less a margin); the `stage` frame; a valid calibration; a 3 µm readback tolerance; connected, normal, idle, APPLICATION firmware and homed; no STOP latch; the actor is not AI.

**How the backend executes a jog** (the Technosoft service thread, which owns the port):
1. Fresh reads, then refuse unless each drive is homed, at rest and the target is inside its **own** `LPOSMIN`/`LPOSMAX`.
2. Per moving axis: write the target, request the move, poll `SRH` bit 9 every 50 ms until the deadline (distance / 8.27 mm/s + 3 s), then check the encoder is within 5 counts of the target.
3. The verified stop at rest returns the drive to state 0, then completion with a fresh encoder position (the router's readback).
4. Any failure: one controlled stop, then fail.

**Writes:**
- They are acknowledged by a single `O`.
- They are possible only through `protocol.is_motion_write` (fixed request and stop frames, or a target in the bounded encoding), and only on a port opened with `allow_motion`.
- Speed, acceleration, limits, focus, turrets, halt/unhalt, `AXISOFF` and EEPROM cannot be built.

**UI:** the Devices page shows a step selector, X−/X+/Y−/Y+ and Stop stage, only in sessions with motor control. Placeholders for the operator's new UI.

## Consequences
- **The first operator-driven physical motion in the app.** Every jog and stop is audited (`motor.stage.jog`, `motor.stage.stop`).
- **The travel box is the power-up box** even if the calibrated limits are loaded (Gate 7). Widening it is a new review.
- **Directions** (Gates 4 and 6): +X = right, +Y = away from the operator.
- **The modes are unchanged:** the session stays in Simulation mode for the optics. Motor control is an explicit, separately flagged opt-in.

  Moving it under a "validated control" mode is future work, once more of the instrument is validated.
