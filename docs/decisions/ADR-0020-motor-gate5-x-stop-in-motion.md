# ADR-0020: Motor Gate 5, controlled stop during a 10 mm X move

- **Status:** Accepted: implemented and tested against a fake drive chain (2026-11-13). **Nothing sent.** Running it on the instrument is a separate operator approval.
- **Builds on:** ADR-0018 and ADR-0019; the spec [`fl1_v1.6_motor_gate5_x_stop_in_motion_spec.md`](../evidence/fl1_v1.6_motor_gate5_x_stop_in_motion_spec.md).

## Decision
`python -m openevos.maintenance.motor_gate5_x_stop_in_motion` (phrase `STOP X DURING 10MM MOVE ON COMn`; flags `--instrument-on-2min` and `--stage-empty-watching`; dry run by default).

- **Writes:** constants to X only: `CPOSCMD` = 22,680 (10 mm) or 0, `ESTATE` = 8, and Gate 3's stop. Gate 4's link layer is reused with a per-link write allowlist.
- **Preconditions:** Gate 4's baseline checks, plus `LPOSMAX` ≥ 22,680.
- **The stop test:**
  - X is commanded towards +10 mm; Gate 3's stop is sent 0.5 s later, with no reads in between;
  - then the tool waits ≤ 2 s for `SRH` bit 9;
  - **interrupted** = 500 < `APOS` < 22,180, `ESTATEV` = 0 and \|`APOS` − `CPOS`\| ≤ band.
- **Return:** to 0, as Gate 4's home leg, then the stop to idle and the final checks.
- **Outcomes:**
  - `pass` only if the stop was interrupted mid-move and the return was clean;
  - `not_interrupted` if the move finished first (the stop under motion is untested);
  - any other rest state, or X not at rest after the stop: **nothing further is sent**.

## First run (2026-11-13): PASS
- **Result:** the stop sent ~0.5 s into a 10 mm move brought X to rest at **10,891 counts (4.80 mm)**, with `APOS` = `CPOS`; then back to 0 with 0 error; idle.
- **Finding:** the rest position matches the predicted speed (≈ 8.3 mm/s), so the 0.8 ms period and the speed units are now Strongly supported.
- **Details:** spec §8.

## Not covered
Other axes; any other target; speed, acceleration or limit writes; halt.
