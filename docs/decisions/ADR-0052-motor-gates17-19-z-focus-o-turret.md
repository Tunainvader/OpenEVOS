# ADR-0052: Motor Gates 17–19, first Z (focus) and O (objective turret) reads and moves

- **Status:** Proposed (2026-11-17). Nothing built or run.
- **Spec:** `docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md`.
- **Builds on:**
  - ADR-0016–0021 (the X/Y gate pattern);
  - ADR-0035/0041 (the F gates);
  - ADR-0030 (Gate 9 reads);
  - ADR-0051 (motor control from the app).

## Context
- **The request:** the operator wants focus and objective changes driven from the app.
- **What exists:** OpenEVOS has only ever read Z and O, and only their state and position. Their program variables
  have never been read on the unit.
- **What is missing:**
  - The vendor host's Z-move and objective-change code is not decompiled.
  - No turret-safe Z height exists in the vendor data.
  - The physical direction of Z is unknown.

## Decision
1. **Three gates, in order, each built and run only with the operator's approval:**
   - **Gate 17:** reads only. Z and O program variables against the decode.
   - **Gate 18:** first Z move. Idle stop, then +400 counts (49 µm) and back to 0. The operator records the
     direction.
   - **Gate 19:** first objective change, one position via the drive's own GOTO_POS (state 3, which has stuck
     detection). Abort is the vendor's `AXISOFF`.
2. **Frames are computed from each axis' `variables.cfg`** and pinned by tests, as for X/Y. Each tool can build only
   its own few frames:
   - Gate 18: two Z targets (+400 and 0), `ESTATE` 8 and `ESTATE` 6;
   - Gate 19: `ITARGETPOS` 1–5, `ESTATE` 3 and `AXISOFF`.
   Speed, acceleration, limits and states 2 or 4 on O are never written.
3. **Interim objective-change rule** until a turret-safe Z is measured: Z at its home position (0) and an empty stage.
4. **Not before all three gates pass:** Z and O in the app, which gets its own ADR.

## Consequences
- Each moving gate needs the operator present, with an empty stage and the app closed (COM3).
- Gate 17 can run at any time the app is closed. It moves nothing.
- If a decompile of PearlScope's `ZAxisMoveToPos` / `GoToObjectiveTurretPosition` becomes available, its frames
  replace the computed ones before Gates 18/19 run.
