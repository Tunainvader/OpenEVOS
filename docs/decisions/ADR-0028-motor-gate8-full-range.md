# ADR-0028: Motor Gate 8, full-range X/Y traverse

- **Status:** Accepted: implemented (2026-11-13); dry run OK; the run on the instrument is pending approval.
- **Builds on:** ADR-0019 to ADR-0022 (Gates 4–7); the spec [`fl1_v1.6_motor_gate8_full_range_spec.md`](../evidence/fl1_v1.6_motor_gate8_full_range_spec.md).

## Decision
`python -m openevos.maintenance.motor_gate8_full_range` (phrase `TRAVERSE XY FULL RANGE ON COMn`; flags `--instrument-on-2min` and `--stage-empty-watching`):
- moves X, then Y, from home to the calibrated max minus 2 mm and back;
- uses Gate 4's verified link, leg, stop and final-check code (axis-generic plans), with targets computed at run time from the git-ignored calibration;
- refuses unless the drives hold exactly the Gate 7 limits;
- registered as `REVIEWED_MAINTENANCE["motor.xy_full_range_traverse"]`.

A pass is the evidence needed for a later review that widens the app's PC travel box (ADR-0024) from 0–22 mm to the calibrated envelope.
