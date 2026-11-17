# Motor Gate 5: stop during motion, X towards +10 mm, stopped partway, then back to 0 (specification, for review)

- **Status:** implemented (ADR-0020) and **run once on 2026-11-13: PASS** (§8). X was stopped mid-move at 4.80 mm and returned to 0 exactly.
- **Date:** 2026-11-13. The operator asked for "a longer one" after Gate 4 passed.
- **Builds on:**
  - Gate 3 (controlled stop on an idle axis, ADR-0018);
  - Gate 4 (X +1 mm and back, 0 counts error, ADR-0019; +X = stage moves right, seen from the front).
- **Classification:** L4, moves hardware, drive RAM writes only.

## 1. Purpose
Verify **the controlled stop on a moving axis**, which is what a stop exists for, and exercise a 10× longer move. X is commanded towards +22,680 counts (10.00 mm, about 1.2 s at the power-up speed). Gate 3's verified stop is sent about 0.5 s into the move. The drive must decelerate to rest **short of the target**, settle, and report idle. Then X returns to 0 and is stopped back to idle.

## 2. Expectations (Probable: 0.8 ms sampling period, speed in counts/s; see motor L0 §3a)
- **Speed:** `CSPDCMD` 15.0 ≈ 18,750 counts/s ≈ **8.3 mm/s** (to the right).
- **Acceleration:** `CACCCMD` 1.0 ≈ 1.56 × 10⁶ counts/s²; ramp ≈ 12 ms.
- **Stop:** `CACCSTOP` 1.0 gives the same deceleration. The stopping distance is ≈ **110 counts (0.05 mm)**, so the stop is near-instant to the eye.
- **Stop timing:** the stop request goes out about 0.5 s after the move request is acknowledged. **Expected rest position ≈ 9,000–10,000 counts (≈ 4 mm).** Polling is slow (each read round trip ≈ 0.4 s), so no read happens between the move request and the stop: the timing is set by a fixed delay.
- **Gate 4 finding:** `ESTATEV` shows 13 while still moving, so completion = **`SRH` bit 9 set**, plus a position check.

## 3. The only writes (constants, X only; each after its line sync, each acknowledged by `O`)

| Write | Frame |
|---|---|
| **T1** `CPOSCMD` = 22,680 | **`08 00 10 25 D4 58 98 00 00 01`** |
| M2 `CPOSCMD` = 0 (Gate 4) | `08 00 10 25 D4 00 00 00 00 11` |
| M3 `ESTATE` = 8 (Gate 4) | `06 00 10 21 B3 00 08 F2` |
| Stop W1–W3 (Gate 3) | `06 00 10 21 CD 00 01 05` · `08 00 10 25 E8 00 00 00 01 26` · `06 00 10 21 B3 00 06 F0` |

No other target, axis, speed, acceleration, limit, halt or power-stage write can be built. The reads are the same as Gate 4's.

## 4. Sequence
1. **Phase 0, before opening:**
   - the operator confirms the instrument has been on ≥ 2 minutes;
   - **the stage is empty and nothing within ~12 mm to the right of the carriage obstructs it**;
   - **they are watching X with a hand near the power switch**;
   - the phrase `STOP X DURING 10MM MOVE ON COMn`;
   - dry run by default.
2. **Phases A and B, as Gate 4:** all axes idle and homed. X:
   - `APOS` = `CPOS` = 0;
   - **`LPOSMIN` ≤ 0 and `LPOSMAX` ≥ 22,680** (power-up 50,000);
   - speed, acceleration and stop deceleration at their power-up values;
   - 0 < band ≤ 50;
   - `SRH` bit 9 set.
3. **Phase C, move and stop:**
   1. T1, then M3; wait **0.5 s**;
   2. stop W1, W2, W3;
   3. then poll `SRH`, `ESTATEV` and `APOS` for **≤ 2 s** until `SRH` bit 9 is set.
4. **Phase D, judge the stop:**
   - **interrupted:** 500 < `APOS` < 22,180, `ESTATEV` = 0, and \|`APOS` − `CPOS`\| ≤ band. The stop worked mid-move;
   - **not interrupted:** `APOS` within the band of 22,680. The move finished before the stop arrived, so the stop under motion is **not tested**. This is reported, and the sequence continues to the return;
   - **anything else** (still moving after 2 s, out of band, or past the target): **fault**.
5. **Phase E, return, as Gate 4's home leg:** M2, then M3. Poll for ≤ 3 s until bit 9 is set. Require \|`APOS`\| ≤ band and `CPOS` = 0.
6. **Phase F:** stop W1–W3, so X returns to `ESTATEV` 0.
7. **Phase G, final checks (read-only):** all axes idle; X within the band of 0; parameters unchanged.

**Pass** = interrupted, plus a clean return and final checks. **"Not interrupted" is not a pass** for this gate.

## 5. Faults
- **Stop not acknowledged, or X not at rest 2 s after the stop:** send **nothing more**; read the state; report. The operator decides recovery: a power cycle re-homes. If anything is still moving, switch the instrument off.
- **Fault on the return leg:** one stop, as Gate 4; then nothing else.
- No halt, unhalt or `AXISOFF` in any path. The drive's clamp bounds every target to 0…50,000 (≈ 22 mm).

## 6. Hazards

| # | Hazard | Control |
|---|---|---|
| G5-1 | Collision over the longer travel | Max 10 mm, to the right; the operator clears the stage and the path; inside the power-up box |
| G5-2 | The stop fails while moving | The move is bounded anyway: it ends at 10 mm (the target) or at the drive clamp (22 mm). 2 s check; operator's power switch |
| G5-3 | Wrong speed or units (Probable) | Power-up speed only; if the units are wrong, the move simply finishes before or after the stop: "not interrupted" or a still-bounded move |
| G5-4 | Completion misjudged (the state-13 trap) | `SRH` bit 9 plus position checks; the stop result is judged on bit 9, `ESTATEV` 0 and position |

## 7. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Build the tool (fake device only) | **Implemented** (`maintenance/motor_gate5_x_stop_in_motion.py`, ADR-0020), per the operator's "let's try a longer one"; fake chain only | operator | 2026-11-13 |
| Run it once on the instrument | **Approved; run once: PASS (§8)** | operator | 2026-11-13 |

## 8. First run (record `motor-gate5-20261112T194543-pass.json`): PASS, the stop works on a moving axis

- **Writes:** all 10 were acknowledged by `O` (T1, M3, stop, M2, M3, stop).
- **Stop in motion:** X was commanded towards 22,680 (10 mm); the stop was sent about 0.5 s later.
  - First poll: `SRH` `0x013F` (still moving or settling), `ESTATEV` 0.
  - Second poll: `0x033F` (at rest).
  - **Rest position `APOS` = `CPOS` = 10,891 counts (4.80 mm)**, short of the target. Result: **interrupted**.
- **Return:** to 0 with **0 counts error**. X was then stopped to idle; all six axes idle; parameters unchanged.

### What this establishes
- **Verified: the controlled stop (`ESTATE` = 6 with `CACCSTOP` 1.0) halts a moving axis cleanly.** X came to rest with no gap between commanded and actual position, and returned to idle at once.
- **The speed estimate is confirmed.** The stop request reached the drive about 0.55–0.6 s after the move request: the 0.5 s delay plus three synced writes at about 16 ms each. At the predicted ≈ 8.27 mm/s that gives ≈ 4.6–5.0 mm, and the drive came to rest at **4.80 mm**. The 0.8 ms sampling period and the speed units (counts/s × 0.0008) move from Probable to **Strongly supported**.
- **Operator observation (2026-11-13):** the stop "looked normal". The operator will watch more closely in later tests.
- **`ESTATEV` returns to 0 at the stop request,** while `SRH` bit 9 can still be clear for a moment. So completion is again judged by bit 9.
