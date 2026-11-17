# ADR-0045: Motor Gate 8 rev 6, T1 correlation runs with Y switched off

- **Status:** Accepted (2026-11-16). Implemented and tested against a fake drive chain. **Not run on the instrument;**
  each run needs the operator's approval.
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate8_rev6_t1_runs_spec.md`.
- **Code:** `src/openevos/maintenance/motor_gate8_t1_runs.py`; tests in `tests/test_maintenance_motor_gate8_t1_runs.py`.
  Registered as `motor.x_t1_runs_y_off` in `REVIEWED_MAINTENANCE`.
- **Builds on:** ADR-0028/0030 (Gate 8), ADR-0032 (Gate 10 speeds), ADR-0044 (Gate 15 `AXISOFF`, passed
  2026-11-16), ADR-0040 (fast-read timing, CSV format).

## Decision
1. **A separate module.** Gate 8's leg polls with Gate 4's reads (~65 ms each), too slow for the plan's ≥ 20 Hz.
   This module reuses Gate 8's targets and frames, Gate 4's move request, stop and baseline checks, Gate 10's speed
   frames, and Gate 15's `AXISOFF`, fast read and Y baseline.
2. **Y off first.** Gate 15's W1 goes out before any X write. Y `SRL` bit 15 must read 0 within 100 ms; otherwise
   the outcome is `y_not_off`, with nothing written to X.
3. **The only new writes: X `CACCCMD`** with raw 190 (Run C), 1,573 (Run A) or 1.0 (the restore).
   - `cacc_frame()` refuses every other value.
   - Speed uses Gate 10's slow and fast frames only; fast is the power-up value, so Run A writes no speed.
   - Both are restored and read back on every exit after they are written.
4. **The ramp check:** a 2 mm (Run C) or 10 mm (Run A) X move. The ramp from 10 % to 90 % of the expected speed
   must be 0.3–1.0 s before any long move. This is needed because the acceleration scale is Probable.
5. **Logging:** X `APOS`, Y `APOS` and X `IQ` back to back (~20 Hz), plus X `CPOS` and Y `SRL` every 10th sample.
   - The output is a CSV in the `y_drift_correlate.py` format plus `x_iq`, and a JSON record without the rows.
   - Y's value at each leg end is read fresh.
6. **Y drift is logged, not refused.** The run stops X (one controlled stop) only if:
   - Y comes back on;
   - Y moves more than 100 counts from its baseline;
   - X has a fault: timeout, wrong direction, landing outside the band, or a link failure.

   X's own pre-move check (\|`APOS` − `CPOS`\| ≤ 3) still applies.
7. **Every run ends with Y off** and the message to power-cycle. Nothing in the module can switch Y on.
8. **Also read:** `HOMEDONE` (0x03B0) and `LHOMEINDEXDELTA` (0x03F4) on X and Y, from the plan's T0 power-on list,
   as T4 data.

## Consequences
- **Before each run:**
  - a fresh power cycle, then Gate 7;
  - the app closed;
  - the operator's confirmations and the phrase `RUN T1 <C|A> X ONLY Y OFF ON COMn`.
- **After each run:** a power cycle.
- **Duration:** Run C ≈ 16 min (4 cycles at 1 mm/s), Run A ≈ 10 min (20 cycles at 8.27 mm/s).
- **Not covered:** Run B (Y powered), T1b, and a 25 % speed.
