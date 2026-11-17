# ADR-0017: Motor Gate 2, read-only check of the drives' safety-relevant variables

- **Status:** Accepted: implemented and tested against a fake drive chain (2026-11-13). **Nothing sent.** Running it on the instrument is a separate operator approval.
- **Builds on:** ADR-0016 (Motor Gate 1 rev 2), motor L0 §3a, and the spec [`fl1_v1.6_motor_gate2_spec.md`](../evidence/fl1_v1.6_motor_gate2_spec.md).

## Context
The drive programs clamp every X/Y/Z/F move to `LPOSMIN`/`LPOSMAX`, and for relative X/Y moves and for Z/F moves that clamp is the **only** position check. A controlled stop uses `CACCSTOP`. *(Corrected after the run: the drive programs' `VAR_INIT` does set them, with short-form writes that the offline search missed; PearlScope then overwrites them at start-up.)* So their values after a power-up without PearlScope are unknown, and no stop or move tool may be designed until they are read.

## Decision
`python -m openevos.maintenance.motor_gate2` (phrase `READ MOTOR LIMITS ON COMn`):
- **Transport:** reuses Gate 1 rev 2 unchanged: port identity, driver-state check, vendor DCB and timeouts, baud passes, and a line sync before every frame.
- **Requests:** the axis-ID query, then the readiness reads on all six axes; it **stops before any parameter read** unless every axis is idle and X/Y/Z/F are homed. Then 41 fixed reads in total, generated per axis from each axis' `variables.cfg`:
  - on X/Y/Z/F, `APOS`, `CPOS`, `LPOSMIN` and `LPOSMAX`, plus `CSPDCMD` and `CACCCMD`;
  - `CACCSTOP` on X/Y/Z only (F has none);
  - on P/O, readiness, `APOS` and `CPOS` only.

  A test pins every frame against the spec table.
- **Guards:** `assert_allowed` passes only those frames and the sync byte. No write opcode can be built.
- **Decoding:** values are decoded in the vendor's verified 32-bit order; `FIXED` = integer high word + fraction / 65,536.
- **Offline checks, in the same run:** limits ordered and non-zero; span against the `mcba` spans; an optional comparison with the decoded `mcba` values; `CACCSTOP` > 0; `|APOS − CPOS|`.
- **Output:** git-ignored evidence. Values are CANDIDATE.
- **Registry:** `REVIEWED_L3_READS["motor.gate2_read"]`.

## First run (2026-11-13): complete
- **Values read:** all 41 reads answered at 115,200 baud; every drive idle and homed.
- **Power-up values (`VAR_INIT`; the context statement above was wrong):**
  - X/Y limits 0…50,000 counts (≈ 22 mm);
  - Z −1,775…67,000 (8.39 mm, which matches the stored `mcba` span);
  - F 0…128,000;
  - `CACCSTOP` = 1.0 on X/Y/Z;
  - `APOS` = `CPOS` = 0 everywhere.
- **Verified on hardware:** the vendor's 32-bit word order.
- **Details:** spec §8.

## Not covered
Any write, stop, halt, unhalt, move or limit write; a motor backend for the command core.
