# ADR-0041: Motor Gate 12, F carriage to the mirror via the drive's GOTO_MIRR

- **Status:** Accepted (2026-11-15). Run 1 `--to mirror` passed on the instrument (`motor-gate12-20261115T183309-pass`:
  `APOS` 128,000, `MER` 0, no limit switch). Run 2 `--to slot1` passed (`motor-gate12-20261115T183621-pass`:
  `APOS` 0, `MER` 0). With F at 128,000 the colour camera saw flashlight light; at slot 1 it read 0.
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate12_f_mirror_spec.md`.
- **Evidence:** offline decode of `FAxis.sw` (SHA-256 `ada4e3ee…80b5d9b`); `motor-gate9-20261113T175045`
  (`LMIRROR1POS` 128,000, limits 0–128,000).

## Context
The colour camera reads 0 with light on the stage while F is at slot 1 (ADR-0039). The channel model puts
brightfield at the mirror. Gate 11 (ADR-0035) reaches slots 1–4 only.

## Decision
- **Use the drive's own `STATE_17_GOTO_MIRR`:** the PC writes only `ESTATE` = 17. The drive sets `LTARGETPOS` =
  `LMIRROR1POS` and runs the clamped `STATE_2_GOTO_POS`. The PC never sends a position.
- **Not PearlScope's `FAxisMoveToPos` path** (`LTARGETPOS` = absolute + `ESTATE` = 2): it would put a position in a PC frame.
- **Return option:** `--to slot1` reuses Gate 11's reviewed frames (`LTARGETPOS` = 1, `ESTATE` = 16).
- **Refuse with nothing written** unless the drive holds the reviewed cube positions, `LMIRROR1POS` = 128,000 and
  limits 0/128,000 exactly.
- **Completion, fault and confirmation** as Gate 11: `ISTATUSFLAGS` 0x4000 plus `APOS` within `LCONVERGEBAND`;
  F's own `STATE_15_STOP` on a fault; one destination per approved run with its own phrase and `--nobody-looking`.
- Registered as `motor.f_goto_mirror`.

## Consequences
- The camera session can test whether the mirror routes light to the colour camera.
- `LMIRROR1POS` = 128,000 is a hard-coded program default and sits on `LPOSMAX`. PearlScope's calibration suggests
  the mirror is ≈ 10,900 counts further out. Whether 128,000 is on the mirror is **Unknown**; if the image is
  vignetted or dark, moving further would need a separate gate that changes the drive's limit, and its own review.
- F is still not in the app; that comes after the gate runs pass.
