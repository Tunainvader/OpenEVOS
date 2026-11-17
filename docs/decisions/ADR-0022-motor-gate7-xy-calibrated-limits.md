# ADR-0022: Motor Gate 7, load the calibrated X/Y limits into the drives

- **Status:** Accepted: implemented and tested against a fake drive chain (2026-11-13). **Nothing sent.** Running it on the instrument is a separate operator approval.
- **Builds on:** ADR-0017, ADR-0019 to ADR-0021; the spec [`fl1_v1.6_motor_gate7_xy_limits_spec.md`](../evidence/fl1_v1.6_motor_gate7_xy_limits_spec.md).

## Decision
`python -m openevos.maintenance.motor_gate7_xy_limits` (phrase `LOAD XY CALIBRATED LIMITS ON COMn`; flags `--instrument-on-2min` and `--stage-clear`; dry run by default).

- **Source values:** the X/Y `mcba` position limits, read **at run time** from the git-ignored parsed calibration. No calibration value is in code, tests or docs.
- **Validation before opening the port:** lMin within (−2 mm, 0]; span 119.1 / 78.1 mm ± 0.5 mm; lMax > 50,000.
- **Writes:** the single builder makes only `25 B4`/`25 B6` (X/Y `LPOSMIN`/`LPOSMAX`) to axis 1 or 2. The link admits only the four validated frames. Each write is acknowledged by `O` and then read back exactly; any mismatch stops further writes.
- **Preconditions:** all axes idle and homed; X/Y at rest at home; the current limits either power-up (0…50,000) or already equal to the calibration (then nothing is written).
- **Afterwards:** limits loaded, no motion, all axes idle, Z/F limits untouched.
- **RAM only:** lost at power-off.
- **Registry:** `REVIEWED_MAINTENANCE["motor.xy_load_calibrated_limits"]`.

## Not covered
Z/F limits; moves over the full range; EEPROM.
