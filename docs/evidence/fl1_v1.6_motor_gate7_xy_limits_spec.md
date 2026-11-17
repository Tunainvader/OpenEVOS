# Motor Gate 7: load the calibrated X/Y stage limits into the drives (specification, for review)

- **Status:** implementation approved and done (ADR-0022, fake device only). **Nothing sent;** running it is a separate approval.
- **Date:** 2026-11-13. The operator asked for the "full stage range" after Gates 4–6 passed.
- **Classification:** L4. **The first write to a drive safety setting** (the limits that the drive's `CLAMPTOLIMITS` enforces on every move). It writes RAM only. **No motion.**

## 1. Purpose
At power-up the X and Y drives limit every move to 0…50,000 counts (≈ 22 mm, `VAR_INIT`; Gate 2). PearlScope widens this at start-up by writing the stored calibration (`mcba` X/Y position limits) into `LPOSMIN`/`LPOSMAX`. Gate 7 does the same for X and Y, then reads the values back. Afterwards the drives clamp to the calibrated envelope (spans **119.1 mm × 78.1 mm**) until the next power cycle, when `VAR_INIT` restores 0…50,000.

## 2. What is known

| Fact | Label |
|---|---|
| **PearlScope's call:** it passes the `mcba` `XAxisPositionLimits` (lMin, lMax) read from the LED board's section-2 blob **unchanged** to `SetXAxisPositionLimits` (vtable `+0x4B0`; `pearl_mcba.c`), and likewise for Y | Strongly supported (decompiled) |
| **`SetXAxisPositionLimits`** (`FUN_180034EB0`) sends, in order: a 32-bit write of `LPOSMIN` (`25 B4`), a read-back of `0x03B4`, a 32-bit write of `LPOSMAX` (`25 B6`), a read-back of `0x03B6`. It caches both on the PC | Strongly supported (decompiled) |
| **Y** uses the same addresses (`0x03B4`/`0x03B6`) with destination `00 20` | Verified (`variables.cfg`) |
| **The backed-up values** (`openevos-scans/…-parsed.json`, git-ignored; the values are not reproduced here): both minima are slightly **negative** (< 2 mm behind home); the maxima give spans of 119.1 mm and 78.1 mm | Verified (local file) |
| **The limits live in RAM:** `VAR_INIT` sets 0…50,000 at every power-up | Verified (Gate 2 and the `.sw`) |
| **The drive uses them** in `STATE_8_MOVE_POS`, `STATE_5_MOVE_REL` and `MOVEPOSAGAIN` via `CLAMPTOLIMITS` | Strongly supported (`.sw`) |

## 3. The writes (built at run time from the backed-up file, so no calibration value is in the repository)
Four frames, each sent once after its line sync, each acknowledged by `O`, each followed by a read-back:

| # | Write | Frame shape |
|---|---|---|
| L1 | X `LPOSMIN` = file X lMin | `08 00 10 25 B4 lo-hi lo-lo hi-hi hi-lo cs` |
| L2 | X `LPOSMAX` = file X lMax | `08 00 10 25 B6 …` |
| L3 | Y `LPOSMIN` = file Y lMin | `08 00 20 25 B4 …` |
| L4 | Y `LPOSMAX` = file Y lMax | `08 00 20 25 B6 …` |

- **Order:** min first, then max, per axis, as the vendor does.
- **Allowlist:** only these two opcodes (`25B4`, `25B6`), only destinations `00 10`/`00 20`, and **only the four values taken from the validated file**. Tests pin the frame builder.
- **No other write exists in this tool:** no move, stop, `ESTATE`, speed or Z/F limit.

## 4. Validation of the source values (refuse before opening the port)
- The parsed file must exist, have the expected layout, and be the one produced by Step D and `pearl_persistent`.
- **X:** −2.0 mm < lMin ≤ 0, and the span (lMax − lMin) is **119.1 mm ± 0.5 mm**, with lMax > 50,000.
- **Y:** −2.0 mm < lMin ≤ 0, and the span is **78.1 mm ± 0.5 mm**, with lMax > 50,000.

(Unit conversion: 2,880 counts per 1.27 mm.)

## 5. Sequence
1. **Phase 0:**
   - the operator confirms the instrument has been on ≥ 2 minutes and the stage is clear (no motion is expected);
   - the phrase `LOAD XY CALIBRATED LIMITS ON COMn`;
   - dry run by default; the dry run prints the four frames with the values in mm.
2. **Phase A, readiness:** all six axes idle and X/Y/Z/F homed.
3. **Phase B, baseline:** X and Y `APOS`, `CPOS`, `LPOSMIN`, `LPOSMAX`, `SRH`.
   - Refuse unless each axis is at rest (`SRH` bit 9) and at home within band.
   - Refuse unless the current limits are either the power-up 0…50,000 or already equal to the file values. In the second case nothing is written ("already loaded").
4. **Phase C, writes:** L1 + read-back, L2 + read-back, L3 + read-back, L4 + read-back.
   - Each read-back must equal the written value exactly.
   - Any mismatch or missing acknowledgement: **stop writing**; read X/Y limits and state; report.
5. **Phase D, final checks:**
   - X/Y limits equal the file values;
   - all six axes idle;
   - X/Y `APOS`/`CPOS` unchanged (no motion);
   - Z/F limits untouched.

## 6. Hazards

| # | Hazard | Control |
|---|---|---|
| G7-1 | **A wrong value opens a dangerous envelope**, letting later moves crash into the stage frame | Values come only from the backed-up calibration, validated against the independently derived spans (§4); exact read-back; the drive is otherwise unchanged |
| G7-2 | **A negative minimum** allows travel up to ≈ 1.8 mm behind home, towards the home or limit switch | This is the vendor's own value, used every PearlScope session. The hardware limit switches remain. Later move tools must still choose targets in their own reviewed ranges |
| G7-3 | **Partial write** (min written, max not) | The intermediate states are still bounded (for example min = file value, max = 50,000). Stop and report; a power cycle restores the defaults |
| G7-4 | **Unintended motion** | Writing limits does not start a move (`CLAMPTOLIMITS` runs only inside the move states); X/Y are checked idle and unchanged before and after |
| G7-5 | **False sense of permanence** | RAM only; lost at power-off; the report says so, and later tools re-check the limits before moving (as Gate 4 does) |
| G7-6 | **Calibration values leaking into the repository** | Read at run time from git-ignored scans; never written into code, tests or docs |

## 7. Not covered
- Z and F limits (their power-up values are close to the stored ones; a later review).
- Any move over the full range (a later gate, for example a long X/Y traverse).
- Persisting anything to EEPROM.

## 8. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement (fake device only) | **Approved and implemented** (`maintenance/motor_gate7_xy_limits.py`, ADR-0022) | operator | 2026-11-13 |
| Run once on the instrument | **Approved ("continue with the full range motor test"); run once: PASS (§9)** | operator | 2026-11-13 |

## 9. First run (record `motor-gate7-20261113T103701-pass.json`): PASS
- **Dry run** (`…103626-dry_run.json`): the calibration file validated as X −1.764 … 117.299 mm and Y −1.764 … 76.288 mm (spans 119.06 / 78.05 mm, within ± 0.5 mm of the stored spans).
- **Writes:** four limit writes, each acknowledged and **read back exactly**. No motion; all six axes idle; Z/F limits untouched.
- **The drives now clamp X/Y moves to the calibrated envelope** until the next power-off (`VAR_INIT` then restores 0…50,000).
- **Not yet changed:** the app's PC-side travel box is still 0–22 mm (ADR-0024). Widening it to the calibrated envelope needs its own review and a full-range traverse test.
- **Also found this session:** the app's telemetry read the 13-byte axis-ID reply as 9 bytes, so it never connected to the real drives. Fixed: it now reads the length byte first. The simulated chain now replays the real recorded reply.
