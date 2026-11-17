# Motor Gate 8 rev 6: T1 correlation runs, X-only with Y switched off (specification, for review)

- **Status:** implemented (ADR-0045, 2026-11-16) and tested against a fake drive chain. **Not run on the
  instrument.** How to run it: §10.
- **Source:** Y-axis plan v3: §4 run order step 4, T1 Runs C and A, the T0 logging list, and the safety rules
  ("X-only runs switch Y off by default … Confirm `SRL` bit 15 = 0 on Y before X starts; abort the run if it isn't").
- **Builds on:**
  - Gate 8 (full-range X traverse, rev 3–5; ADR-0028, ADR-0030);
  - Gate 10 (X speed presets, ADR-0032);
  - Gate 15 (host `AXISOFF` on idle Y, ADR-0044; **passed 2026-11-16**, Y held at 0 counts while off);
  - Gate 7 (calibrated limits, loaded after each power-up);
  - ADR-0040 (fast-read timing and the CSV format `y_drift_correlate.py` reads).
- **Classification:** L4. Every write is an already-verified frame **except the X acceleration (`CACCCMD`) writes in
  §3, which are new**.

## 1. Purpose
Measure what Y's encoder does while only X travels, with Y's power stage off. With Y off, a drifting count cannot
drive Y anywhere, so the drift can be logged for the whole run instead of stopping at 3 counts as Gate 8 rev 5 must.

The log must let `y_drift_correlate.py` decide between position-locked, current-locked and motion-even drift (plan
T1 table). That needs:
- X and Y encoders, plus X current, at ≥ 20 Hz;
- X ramps of at least 0.5 s, so acceleration phases span several samples. At the power-up acceleration a ramp takes
  about 12 ms, inside one sample.

| Run | X speed | X ramp | Cycles (0 → calibrated max − 2 mm → 0) | Duration |
|---|---|---|---|---|
| **C** | Gate 10 **slow**, 1.0 mm/s (12 % of 8.27) | 0.5 s | 4 | ≈ 16 min |
| **A** | Gate 10 **fast**, 8.27 mm/s (power-up) | 0.5 s | 20 | ≈ 10 min |

**Deviation from the plan:** Run C uses 12 % rather than 25 % speed. That keeps every speed write to Gate 10's
three verified frames, and stays near the plan's 15 min per run. A 25 % speed would need a new `CSPDCMD` value.

## 2. What is known

| Fact | Label |
|---|---|
| **Gate 15:** `04 00 20 00 02 26` switches Y off within one read (15 ms after its `O`), with `SRL` 0x4400, no flag, state or `CPOS` change, nothing on X, and Y holding at 0 counts for 10 s | Verified on the unit (2026-11-16) |
| **X `CSPDCMD` presets:** `08 00 10 25 D6 …` slow / medium / fast, loaded by the drive at every move start, power-up value restored by `VAR_INIT` | Verified on the unit (Gate 10) |
| **`CACCCMD` (0x03D8, 32-bit 16.16 fixed)** is loaded by `STATE_8_MOVE_POS` at every move start, like `CSPDCMD`. It is RAM only, and power-up sets 1.0 | Strongly supported (`.sw` decode; vendor `FUN_180030510` writes it the same way) |
| **Acceleration scale:** 1.0 ≈ 690 mm/s², taking the 0.8 ms sample period as counts per sample². Gate 4 noted an unexplained factor in the vendor's conversion | **Probable.** §4 Phase B measures it before any cycle |
| **The controlled stop uses `CACCSTOP`** (1.0, unchanged), not `CACCCMD` | Strongly supported (Gate 3) |
| **X full range at the power-up speed and acceleration:** 0 counts error each way (Gate 8 rev 1), peak current 24 % of `IMAXPROT` | Verified on the unit |
| **Y drifted 0 → −3 → −8 → −9 counts over 4 X-only cycles with Y powered** (power-up X speed and acceleration) | Verified on the unit (`motor-gate8-20261113T134051`) |

## 3. Writes (constants only)

| Purpose | Frame | Status |
|---|---|---|
| Y off, once, before any X write | **`04 00 20 00 02 26`** | Gate 15 (verified) |
| X speed: slow (Run C) | `08 00 10 25 D6 D0 6E 00 01 52` | Gate 10 (verified) |
| X speed: fast, power-up (Run A and the restore) | `08 00 10 25 D6 00 00 00 0F 22` | Gate 10 (verified) |
| **X `CACCCMD` for Run C**, raw 190 (≈ 2.0 mm/s²; 0.5 s to 1 mm/s) | **`08 00 10 25 D8 00 BE 00 00 D3`** | **new** |
| **X `CACCCMD` for Run A**, raw 1,573 (≈ 16.5 mm/s²; 0.5 s to 8.27 mm/s) | **`08 00 10 25 D8 06 25 00 00 40`** | **new** |
| **X `CACCCMD` restore**, 1.0 (power-up) | **`08 00 10 25 D8 00 00 00 01 16`** | **new** |
| X targets (calibrated max − 2 mm, and 0), move request, verified stop | Gate 8's frames, unchanged | Gate 8 (verified) |

- **No Y write except the one `AXISOFF`.** No Y move, no `AXISON`, nothing on Z, F, O or P.
- The three `CACCCMD` frames are built by `protocol._w32("X", 0x03D8, v)` and pinned byte for byte in tests.
- Every lowered acceleration is gentler than power-up. The stop deceleration (`CACCSTOP`) is not touched, so a stop
  stays as fast as today.

## 4. Sequence

**Before the run (operator):**
- T2 step 0 (the power-off inspection) should come first, per the plan's run order.
- If there is a dial indicator (T6), set it on Y and film it. Read it before and after.

**Phase 0, refuse before opening:**
- the OpenEVOS app is closed;
- exactly one FTDI port;
- `--run C` or `--run A`;
- the operator confirms:
  - a fresh power cycle (Y re-phased) and Gate 7 run since, with no move;
  - the **whole X travel** clear, Z retracted, nothing in Y's path;
  - watching, with the main switch within reach and a second person present;
- the phrase `RUN T1 <C|A> X ONLY Y OFF ON COMn`;
- dry run by default.

**Phase A, readiness and baseline (read-only):**
- Gate 8's checks:
  - all six axes idle, X/Y/Z/F homed;
  - X and Y exactly at home;
  - the drives hold exactly the calibrated limits (else "run Gate 7 first");
  - X `CSPDCMD` 15.0 and `CACCCMD` 1.0.
- Gate 15's Y baseline: on, settled, settle check not armed, |`APOS` − `CPOS`| ≤ 3, and still for 1 s.
- **New reads:** `HOMEDONE` (0x03B0) and `LHOMEINDEXDELTA` (0x03F4, 32-bit) on X and Y, from the plan's T0
  power-on list. This is the first T4 data point.

**Phase B, Y off, then the ramp check:**
1. W1 to Y. **Y `SRL` bit 15 must read 0 within 100 ms, or the run ends with nothing written to X** (the plan's
   abort rule).
2. X `CSPDCMD` (the run's preset) and `CACCCMD` (the run's value), each acknowledged and read back.
3. **Ramp check:** one X move out and back, logged at the fast rate: 2 mm for Run C, 10 mm for Run A. At 8.27 mm/s
   a 0.5 s ramp covers ≈ 2 mm each way, so a 2 mm move would never reach full speed. The measured ramp (time from
   10 % to 90 % of speed) must be **0.3–1.0 s**. Otherwise the run stops X and restores speed and acceleration. That catches a wrong
   acceleration scale (§2) before any long move.

**Phase C, the cycles:**
- Gate 8's legs: out to the calibrated max − 2 mm, a 1.5 s pause, back to 0, then the verified stop at rest.
- Per-leg timeout: (distance / speed + 2 × ramp) × 1.2 + 6 s.
- Gate 8's pre-move check (|`APOS` − `CPOS`| ≤ 3) applies to X only.
- **Logging (T0):** fast reads at the app's timing (Gate 15 / ADR-0040, ~16 ms per read):
  - every sample: X `APOS`, Y `APOS`, X `IQ` (≈ 20 Hz);
  - every 10th sample: X `CPOS` and Y `SRL`;
  - per leg: the X current peaks (`IIQMAX`/`IIQMIN`), as rev 3.
- **Y drift is logged, not refused.** Gate 8 rev 5's 3-count `cross_axis_encoder_drift` stop does not apply while Y
  is confirmed off.
- **The run does stop** (X controlled stop once, then speed and acceleration restored) if:
  - Y `SRL` bit 15 reads 1 (Y came back on);
  - |Y `APOS` − baseline| > 100 counts (44 µm, ten times the largest drift seen): more than a count drift, possibly
    real carriage movement;
  - any Gate 8 fault (timeout, wrong way, landing error, a link failure).

**Phase D, end (read-only, plus the two restores):**
1. X `CSPDCMD` back to 15.0 and `CACCCMD` back to 1.0, both read back.
2. Final reads: everything Phase A read, plus X and Y `ISTATUSFLAGS`, `MER` and the current peaks.
3. Write the files, and print the `y_drift_correlate.py` command for them.
4. **Y stays off.** The run ends with "power-cycle before anything else". The plan requires a re-phase after any
   run that may have changed Y's count, and only the power cycle brings Y back.

## 5. Result

| Outcome | Meaning |
|---|---|
| `complete` | All cycles ran, X ended at 0 within its band, X's speed and acceleration were restored and read back, and every axis is idle. The drift is the data, not a pass condition |
| `y_not_off` | Y was still on 100 ms after W1. No X write was sent |
| `ramp_check_failed` | The measured ramp was outside 0.3–1.0 s. X is restored, no cycle ran |
| `stopped_y_on` / `stopped_y_far` / `fault_stopped` | Stopped mid-run, as §4 Phase C |
| before W1: `refused`, `not_idle`, `baseline_refused` | Nothing written |

**Recorded per run:**
- Y's drift trace and its total at each leg end;
- X position and current, and the measured ramp time;
- Y's `LHOMEINDEXDELTA` before (and after the next power cycle, through `--check-recovered`, which reads it too);
- the dial readings, if the operator reports them.

## 6. Evidence
- `motor-gate8-<time>-t1<run>-<outcome>.json`: baseline, all frames written with their acknowledgements, legs,
  peaks, ramp check, stop reasons and final reads.
- `motor-gate8-<time>-t1<run>.csv`: columns `t, x_apos, y_apos, x_cpos, y_cpos, x_cmd, y_cmd, x_iq`.
  - `y_cpos` is Y's frozen command position.
  - `x_cpos` is carried forward between its reads.
  - Analyse it with `python y_drift_correlate.py <csv> --x-iq x_iq --y-cpos y_cpos --plot <png>`.

## 7. Hazards

| # | Hazard | Control |
|---|---|---|
| R6-1 | Collision over the full X travel | As Gate 8: whole X travel clear, operator watching, main switch, second person; only X moves |
| R6-2 | A new `CACCCMD` value is wrong by the unexplained scale factor, giving a ramp far too long or too short | Lower values only, so only gentler than power-up. The 2 mm ramp check measures it before any long move. The per-leg timeout includes the ramps. The stop uses the unchanged `CACCSTOP` |
| R6-3 | X is left with a low speed or acceleration | Restored and read back at every end path; power-up restores both anyway |
| R6-4 | Y is powered while its count drifts, which is the runaway mechanism | Y off before any X write, confirmed within 100 ms. Y `SRL` is watched through the run, and the run stops if Y comes back on. Nothing in the tool can switch Y on |
| R6-5 | The Y carriage is actually dragged by X travel | The 100-count stop; the dial indicator (T6); the operator watching |
| R6-6 | Y's next move after the run uses a drifted phase | The run ends with Y off. A power cycle is required, and the app's drift monitor latches if Y is found off or drifted |
| R6-7 | The long run hides a link fault | Fast reads resync and retry once, then the run stops; Gate 8's untimed reads stop at the first bad reply |

## 8. Not covered
- **Run B** (Y powered, guarded at 6 counts). It needs the Gate 15 §9 rules implemented in a guarded path, and a
  spec of its own.
- **T1b** (Y-only cycles while logging X).
- **Any app change.**
- **A 25 % speed.**

## 9. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement rev 6 (fake chain only) | **Approved and implemented** (`maintenance/motor_gate8_t1_runs.py`, ADR-0045) | operator | 2026-11-16 |
| Run C once, then Run A once (each after a fresh power cycle and Gate 7) | pending | — | — |

## 10. Running it
The OpenEVOS app must be closed: it holds COM3. For each run:
1. Power-cycle the microscope and wait ≥ 2 minutes.
2. Load the calibrated limits with Gate 7 (its own phrase and flags).
3. Dry run (opens nothing):

   ```
   python -m openevos.maintenance.motor_gate8_t1_runs --run C
   ```
4. The run, with the §4 Phase 0 conditions met:

   ```
   python -m openevos.maintenance.motor_gate8_t1_runs --run C --send --instrument-on-2min --power-cycled-gate7-no-move --x-travel-clear --watching-main-switch-second-person --confirm "RUN T1 C X ONLY Y OFF ON COM3"
   ```
5. Power-cycle again. Gate 15's `--check-recovered` confirms X and Y are back on and homed.
6. Analyse with the command the tool prints:

   ```
   python y_drift_correlate.py <csv> --x-iq x_iq --y-cpos y_cpos --plot <png>
   ```

Run A is the same with `--run A` and the phrase `RUN T1 A X ONLY Y OFF ON COM3`.

## 11. Run C, first run (2026-11-16, `motor-gate8-20261116T044640-t1c-complete.json` + `.csv`): complete

**Setup:**
- after a power cycle and Gate 7 (`motor-gate7-20261116T043018-pass`);
- the operator confirmed the Phase 0 conditions;
- no dial indicator.

**Run:**
- **Y off** 16 ms after the `AXISOFF`.
- **Ramp check:** 0.461 s measured, inside 0.3–1.0 s, so the Probable acceleration scale holds.
- **Cycles:** 4 completed, X landing errors −3…+2 counts, peak X current about 12 % of `IMAXPROT`.
- **Logging:** 16,673 samples over 943 s (17.7 Hz).
- **End state:** speed and acceleration restored and read back; all axes idle; Y still off (`SRL` 0x4400).
- **Homing data:** `LHOMEINDEXDELTA` X 2,184, Y 728 (first T4 data point).

**Y's encoder, with Y unpowered:**

| Leg end | c1 out | c1 home | c2 out | c2 home | c3 out | c3 home | c4 out | c4 home |
|---|---|---|---|---|---|---|---|---|
| Y counts from 0 | −12 | −12 | −21 | −21 | −24 | −24 | −19 | **+36** |

- **Totals:** 203 changes, gross 316 counts, net +36, largest excursion 39 counts.
- **None at idle:** 0 changes in 12 s of X idle. During the ramp check (X 0–2 mm) Y did not change.
- **Position- and direction-locked:**
  - every change happened while X was between about 30 and 80 mm;
  - X going out (30–50 mm): net −19;
  - X coming home (50–80 mm): net +55.
- **Large single steps:** up to +18 counts within one 57 ms sample (X ≈ 55 mm, coming home, cycle 4).
- **`y_drift_correlate.py`:** "mixed or weak" verdict, position lock 4.2×, dither 63 %. Its end-of-travel "hot zone"
  test does not look mid-travel. The plot needs `matplotlib`, which is not installed.

**Reading (Probable):**
- The pattern is the plan's "position-locked" case (cable or mechanical, #1/#2), at a mid-travel X zone rather than
  an end. It reverses sign with X's direction, as a drag or snag would.
- **With Y unpowered, real Y carriage movement also counts.** Gate 15 showed Y holds still at rest, but not under X
  travel. So this run alone cannot tell real movement (#0) from count errors (#1/#2). That needs the dial indicator
  (T6), or the camera image of a stage micrometer.
- **Next:**
  - T2 step 0 (power off): look at what the X carriage passes between 30 and 80 mm, such as cable runs and their
    clamps, the Y encoder cable and anything that could catch.
  - Then repeat Run C with T6.
