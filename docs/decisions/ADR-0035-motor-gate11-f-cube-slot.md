# ADR-0035: Motor Gate 11, F carriage to a cube slot via the drive's GOTO_CUBE

- **Status:** Proposed (2026-11-13). Tested with a fake F drive; not yet run on the instrument.
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate11_f_cube_spec.md`.
- **Evidence:** `motor-gate9-20261113T175045` (F stored slot positions 0/34,000/68,000/102,000, limits 0–128,000).

## Context
Lighting each filter cube (LED Gate 4) needs that cube in the light path. No F motion had been reviewed.

## Decision
- **Use the drive's own `STATE_16_GOTO_CUBE`:** the PC writes only the slot number (`LTARGETPOS` = 1–4) and `ESTATE` = 16. The drive moves to its stored `LCUBEnPOS`, clamped to its limits.
- **Not the calibration file's positions:** `FAxisLocationMotorPosition` sits ~2,770 counts lower, measured from another zero.
- **Refuse with nothing written** unless the drive's stored positions equal the reviewed values.
- **Completion:** `ISTATUSFLAGS` 0x4000 plus `APOS` within `LCONVERGEBAND`.
- **Fault:** F's own `STATE_15_STOP`.
- **One slot per approved run,** with the operator confirming nobody looks into the light path (DAPI is near-UV).
- Registered as `motor.f_goto_cube_slot`.

## Consequences
- The LED session can confirm each slot independently with its one-hot cube sensor (`0x47`) and then light it.
- F is not in the app yet; that comes after the gate runs pass.
