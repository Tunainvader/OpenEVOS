# ADR-0050: LED brightness sweep measured with the mono camera

- **Status:** Accepted for implementation: built and tested against a fake LED board and a fake camera (2026-11-17). Not run on the instrument; each run needs the operator's approval.
- **Spec:** [`fl1_v1.6_led_app_command_table.md` §8](../evidence/fl1_v1.6_led_app_command_table.md)

## Context

- The operator asked why brightness isn't sensed automatically now that the cameras work.
- The LED level commands are verified (Gate 5, ADR-0049).
- The mono camera has a reviewed RAM-only settings gate (Camera Gate 2, ADR-0036).

## Decision

1. **New maintenance tool** `led_camera_sweep`. It combines both reviewed gates without adding any new device command.
2. **Per run:**
   - one light;
   - a strictly increasing list of levels (brightfield ≤ 20, epi ≤ 10);
   - one reviewed shutter value (1, 10 or 50 ms).
3. **Fail safe:**
   - a dark frame first;
   - stop at saturation;
   - on any failure or at the end: light off, LED level restored and read back, camera restored and verified;
   - unconfirmed restores tell the operator to power-cycle.
4. **Relative measurement only:** mean minus dark per level, monotonicity, and a straight-line fit.
5. Registered in `REVIEWED_MAINTENANCE["led_application.camera_sweep"]`.

## Consequences

- Gives a first relative linearity curve for M8 without extra hardware.
- Absolute power and long-term stability still need a power meter.
- The result depends on the light path (F position, objective, sample). Each run records the cube in the path and the camera settings.
