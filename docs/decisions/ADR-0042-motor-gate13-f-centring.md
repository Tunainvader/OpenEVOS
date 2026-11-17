# ADR-0042: Motor Gate 13, F centring test with PC-sent fixed targets

- **Status:** Accepted (2026-11-15). Run 1 passed (`motor-gate13-20261115T192240-pass`). The arc edge moved left with F:
  ≈ 1,950 px at 128,000, ≈ 1,570 at 124,000, ≈ 1,380 at 120,000, and dark by 112,000. So the mirror is centred beyond
  128,000 (Strongly supported).
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate13_f_centring_spec.md`.
- **Evidence:** Gate 12 brightfield sweep `colour-bf-20261115T190306` (right ~20–25 % of the field cut by an arc at
  F = 128,000).

## Context
Gate 12 showed an arc cutting off part of the colour field at the mirror position. Either the mirror is off-centre
at 128,000 (PearlScope's calibration says it is ≈ 10,900 counts further out), or the brightfield illumination is
misaligned. Imaging at F positions short of 128,000 separates the two. The drive has no stored position there.

## Decision
- **First OpenEVOS gate that sends an F position.** It uses PearlScope's own path: 32-bit `LTARGETPOS`, then
  `ESTATE` = 2 (`STATE_2_GOTO_POS`, drive-clamped).
- **Only four fixed targets** (124,000 / 120,000 / 116,000 / 112,000) as precomputed allow-listed frames. There is no
  position argument.
- **Refuse unless F starts at 128,000**, with the reviewed stored positions and limits.
- **Return to the mirror with Gate 12's `ESTATE` = 17.** Fault → `ESTATE` = 15.
- **Run inside one brightfield hold** of the LED session, with a fixed-exposure camera recorder. Each tool is approved
  separately.
- Registered (when built) as `motor.f_centring_steps`.

## Consequences
- The arc-edge shift per count says whether F centring is the cause and estimates the true mirror centre.
- If the mirror proves off-centre, raising `LPOSMAX`/`LMIRROR1POS` would be a further gate. That is a RAM write to the
  drive's limits, not a stored or firmware change, and it needs its own review. Not decided here.
