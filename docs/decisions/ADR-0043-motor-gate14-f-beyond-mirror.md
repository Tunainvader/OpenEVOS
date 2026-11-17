# ADR-0043: Motor Gate 14, F past 128,000 by raising the drive's RAM limit in steps

- **Status:** Accepted (2026-11-15). Run 1 `--up-to 130000` passed (`motor-gate14-20261115T193848-pass`):
  - no contact (peak |`IQ`| 2,915 / 2,977 vs reference 3,061);
  - `LPOSMAX` restored to 128,000;
  - the field edge left the frame at 130,000.
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate14_f_beyond_mirror_spec.md`.
- **Evidence:** Gate 13 run 1 (`motor-gate13-20261115T192240-pass`, `colour-centring-20261115T192123-log.json`): the mirror
  aperture is centred beyond `LPOSMAX` 128,000.

## Context
The colour camera's field is cut on the right at F = 128,000, because the mirror is centred further out. The drive
clamps every move to `LPOSMAX` = 128,000. That value is a RAM default written by `INIT_ONCE` at power-up. The
physical end of travel is not known.

## Decision
- **First write to a drive limit.** Only `LPOSMAX` (RAM) is written, in fixed 1,000-count steps from 129,000 to at most
  134,000. That is just inside PearlScope's own limit, ≈ 134,270 drive counts. `--up-to` defaults to 130,000.
- **Each step writes `LPOSMAX` = *v* before the move to *v*,** so the drive clamp still bounds every move.
  `LMIRROR1POS` and the cube positions are never written.
- **Contact guard:**
  - per-step timeout;
  - `ISTATUSFLAGS` 0x0101 (the drive's own over-current / position-error ISRs, which clear `MER`);
  - `MER` and `SRH` LSP;
  - peak |`IQ`| above 2 × that of a free reference move.
  Any one → F `STOP`.
- **Always restore `LPOSMAX` = 128,000** with read-back; F returns to 128,000 after a pass.
- **Not used:** the drive's `STATE_9_MAX` hard-stop search, which finds the stop by driving into it.
- Registered (when built) as `motor.f_beyond_mirror_steps`.

## Consequences
- The step where the field becomes fully open estimates the true mirror position.
- Adopting it as the working mirror position is a later decision: a start-up write of `LPOSMAX`/`LMIRROR1POS`, or an
  app-side target. Until then, Gates 11–13 and the app keep the 128,000 default.
