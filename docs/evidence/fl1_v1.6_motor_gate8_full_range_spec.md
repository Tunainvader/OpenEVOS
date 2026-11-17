# Motor Gate 8: full-range X/Y traverse inside the calibrated limits (specification)

- **Status:** implemented (ADR-0028); dry run OK. **Not run on the instrument.** The run needs explicit approval.
- **Date:** 2026-11-13. The operator asked to "continue with the full range motor test".
- **Prerequisite:** Gate 7 has loaded the calibrated limits in this power-up (done 2026-11-13, `motor-gate7-20261113T103701-pass`).

## What it does
1. Checks readiness on all six axes. X and Y must be exactly at home, idle (`SRH` bit 9), at the power-up speed, acceleration and stop deceleration, with a sane settle band, and **the drives must hold exactly the calibrated limits** (else "run Gate 7 first").
2. **X:** out to the calibrated max **minus 2 mm** (from the calibration file: 115.299 mm), a 1.5 s pause, back to 0, then the verified stop to idle.
3. **Y:** the same, to 74.288 mm.
4. Final checks: all axes idle, X/Y at 0 within the band, parameters unchanged.

| Item | Value |
|---|---|
| Expected motion | ≈ 46 s in total at ≈ 8.3 mm/s (power-up speed, never written) |
| Per-leg timeout | distance / 8.27 mm/s + 6 s |
| Completion | `SRH` bit 9 + `ESTATEV` 13, then the encoder within `LCONVERGEBAND` (5 counts) |
| Faults | one controlled stop, nothing else; recovery = power cycle |
| Writes | the far targets, 0, the move request and the verified stop, X/Y only; targets are rebuilt and bounded (0…280,000 counts) |

## Hazards
| # | Hazard | Control |
|---|---|---|
| G8-1 | Collision over the full travel (vessel holder, objective, frame) | Empty stage; the operator confirms the **whole** X/Y travel is clear and watches with a hand near the power switch; Z is not moved and stays homed |
| G8-2 | Wrong limits | Targets come only from the validated calibration and stay 2 mm inside it; the drive's own clamp holds the same calibrated limits (Gate 7) |
| G8-3 | Runaway or stall | Distance-based timeout, then the stop; encoder check per leg |
| G8-4 | Long move misjudged as done | `SRH` bit 9 + position, not state 13 alone (Gate 4 finding) |

## Decision record
| Item | Decision | By | Date |
|---|---|---|---|
| Build | Implemented (`maintenance/motor_gate8_full_range.py`) | operator ("continue") | 2026-11-13 |
| Run once on the instrument | **Approved; run once: PASS** | operator | 2026-11-13 |

## Result (record `motor-gate8-20261113T104204-pass.json`): PASS
| Leg | Target (counts) | End `APOS` | Error |
|---|---|---|---|
| X out (115.299 mm) | 261,465 | 261,465 | **0** |
| X home | 0 | 0 | **0** |
| Y out (74.288 mm) | 168,465 | 168,465 | **0** |
| Y home | 0 | 0 | **0** |

All writes were acknowledged; X/Y were stopped back to idle; all axes idle; parameters unchanged. **The full calibrated X/Y range is verified on the instrument with zero position error.** This is the evidence for widening the app's PC travel box (ADR-0024), which needs a separate review. **Operator observation (2026-11-13):** "Gate 8 successful, good." The operator then asked for 4 continuous full-range cycles (`--cycles`, below).

## Rev 3: stepped Y test with peak-current capture (ADR-0030, 2026-11-13)

- **Why:** Y's drive switched itself off on the return from the far end; its software over-current protection
  (`IMAXPROT` = 17,117) is the strongly supported cause (return-move peak -16,244 = 95 %; X full range peaks at 24 %).
- **`--axes {XY,X,Y}`** (default XY) and **`--y-target-mm`** (5 mm ... calibrated max - 2 mm; refused outside).
- **After every leg** the tool reads the drive's `IIQMAX`/`IIQMIN` (reset by the drive at each move start) and records
  `current_peak` and `current_fraction` of `IMAXPROT`.
- **Margin stop:** if any leg reaches **70 %** of `IMAXPROT`, the axis still returns home, then the run ends with
  outcome `current_margin_low` (no further cycles).
- No new writes: the target frames are the same validated `CPOSCMD` form, only the Y value changes.

## Rev 4: watch the idle axis' encoder (2026-11-13)

- On every poll of a leg, the tool also reads the **other** axis' `APOS` (read-only, an already-allowed frame) and
  records `<axis>_APOS_range` per leg. Why: Y's encoder moved -2 counts while only X moved, and Y's runaways
  happened on the first Y move after X.
- With `--axes X` or `--axes Y`, only the moving axis must be exactly at home; the idle axis is just watched.

## Rev 5: commutation-drift interlock (ADR-0030, 2026-11-13)

**Findings recorded:**
- Y over-current handler decode (`IMAXPROT` 17,117). Peak captures: −16,244 (95 %, `motor-gate9-20261113T121350`) and 17,198 (> 100 %, `motor-gate8-20261113T124221`).
- Y-only: stepped 40/60/70/72/73/74.3 mm plus 4 full cycles (`motor-gate8-20261113T131104`). 0 counts error on every move; peak current 15–31 % of `IMAXPROT`.
- X then Y: 3 of 4 runs failed on the first Y move after X. Y ran the wrong way: −940 (`...124221`), −5,805 (`...131452`).
- X-only watch (`motor-gate8-20261113T134051`): idle Y encoder 0 → −3 (c3 out) → −8 (c3 home) → −9 (c4), never corrected by the drive.
- Mechanism (strongly supported): 50 pole pairs, 2,880 counts/rev → 57.6 counts per electrical cycle, so 1 count = 6.25° electrical, and > 14.4 counts reverses torque.

**Interlock:**
- `_leg` refuses (`DriftRefused`, nothing written) when the moving axis' |`APOS` − `CPOS`| > 3 counts.
- Gate 8 ends the run with `cross_axis_encoder_drift` when the idle axis drifts > 3 counts from its command during a leg.
- The app backend applies the same pre-move check and locks a drifted idle axis until restart after a power-cycle re-home.

**Serial:** a leading late `0D` before a read reply is dropped (Gate links and app telemetry). Seen during X's long moves, it is also a possible sign of electrical noise.

## Rev 6: T1 correlation runs, X-only with Y switched off (draft for review, 2026-11-16)
Specified separately: `fl1_v1.6_motor_gate8_rev6_t1_runs_spec.md` (Runs C and A, Y off through Gate 15's
`AXISOFF`, fast logging, new X `CACCCMD` writes for 0.5 s ramps).
