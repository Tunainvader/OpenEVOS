# Motor Gate 4: first movement, X +1 mm and back to 0 (specification, for review)

- **Status:** implemented (ADR-0019) and **run once on 2026-11-13: PASS** (§10). X moved +2,268 counts and back with 0 counts error; it was stopped back to idle.
- **Date:** 2026-11-13.
- **Builds on:** Gates 1–3 (ADR-0016 to ADR-0018). Gate 3 verified the write path and the controlled stop on an idle axis.
- **Classification:** the first **L4 operation that moves hardware**. It writes drive RAM variables only.

## 1. Purpose
Move the X stage carriage **+2,268 counts (1.000 mm)** and back to 0, and verify every step from the drive's own readings. This is the smallest real move that proves:
- the target write;
- the move request;
- completion detection;
- position accuracy;
- the return trip;
- leaving the drive idle afterwards.

## 2. Offline findings this spec relies on (2026-11-13)

| Finding | Label |
|---|---|
| **Move request:** write `CPOSCMD` (`0x03D4`, 32-bit), then `ESTATE` = 8. The drive's `STATE_8_MOVE_POS` clamps the target to `LPOSMIN`…`LPOSMAX` (`CLAMPTOLIMITS`) and starts the move with its current `CSPDCMD`/`CACCCMD` | Strongly supported (`.sw`, vendor `FUN_180030510`) |
| **After a move X does not return to 0.** It sits in **`ESTATEV` = 13** (`STATE_13_MOVEPOSIDLE`): it keeps checking convergence and waits for the next request. From 13, `ESTATE` = 8 goes to `MOVEPOSAGAIN` (a new move, also clamped), and `ESTATE` = 6 runs the stop and returns to 0 | Strongly supported (`.sw` at `0x437C`) |
| **The vendor's completion test** (`XYStageXAxisIsMoving`, `FUN_18003AF60`) reads X's 16-bit status register **`SRH`** (`0x090F`, frame `08 00 10 B0 04 00 11 09 0F F5`, byte-identical). It reports *moving* while **bit 9 is clear** | Strongly supported (decompiled) |
| **The settle band** is the drive variable `LCONVERGEBAND` (`0x03CF`, 32-bit). `CHECK_CONVERGE` compares \|`APOS` − `LTARGETPOSV`\| with it | Strongly supported (`.sw`) |
| **Speed and acceleration:** the vendor converts speed as value × 0.0008 into 16.16 fixed point. This implies a 0.8 ms sampling period, with speed in counts/s (**Probable**). The power-up `CSPDCMD` = 15.0 is then ≈ 18,750 counts/s ≈ **8.3 mm/s** on X, and `CACCCMD` = 1.0 ≈ 1.56 × 10⁶ counts/s² ≈ 690 mm/s². **Expected move time:** about 0.15 s each way. The acceleration scale (1/3,600,000) is not yet explained | Probable |
| **The vendor's stop deceleration constant is exactly 1.0**, the value Gate 3 wrote | Verified (read from the DLL) |

**Gate 4 writes neither speed nor acceleration.** It uses the drive's power-up values, read and checked first: `CSPDCMD` = 15.0 and `CACCCMD` = 1.0. That is exactly what the vendor does when they are unchanged.

## 3. The only frames this tool can send (all to X, `00 10`)

| Purpose | Frame |
|---|---|
| Line sync, axis-ID query, readiness and X reads | as Gates 1–3 (constants) |
| Extra reads | `SRH` `08 00 10 B0 04 00 11 09 0F F5` · `LCONVERGEBAND` `08 00 10 B0 05 00 11 03 CF B0` · `CPOSCMD` `08 00 10 B0 05 00 11 03 D4 B5` · plus Gate 2's X `CSPDCMD`/`CACCCMD` |
| **M1** `CPOSCMD` = 2,268 | **`08 00 10 25 D4 08 DC 00 00 F5`** |
| **M2** `CPOSCMD` = 0 | **`08 00 10 25 D4 00 00 00 00 11`** |
| **M3** `ESTATE` = 8 | **`06 00 10 21 B3 00 08 F2`** |
| **Stop** (Gate 3's W1, W2, W3, unchanged) | `06 00 10 21 CD 00 01 05` · `08 00 10 25 E8 00 00 00 01 26` · `06 00 10 21 B3 00 06 F0` |

- Every write is a constant, sent once after its line sync, and acknowledged by a single `O`.
- The tool cannot build any other target, axis, state, speed, acceleration or limit write, nor `AXISOFF`, halt or unhalt.
- The 32-bit values use the vendor order (Verified on hardware). Checksums match `FUN_18003A4E0` and `FUN_18003A5F0`.

## 4. Sequence (fail fast; the line sync is the only retry)

**Phase 0, refuse before opening:**
- exactly one FTDI port;
- the operator confirms:
  - the instrument has been on ≥ 2 minutes;
  - **no vessel, slide or plate is on the stage, and nothing obstructs the carriage**;
  - **they are watching X with a hand near the instrument power switch**;
- the phrase `MOVE X 1MM AND BACK ON COMn`;
- dry run by default.

**Phase A, readiness:** as Gate 3. All six axes idle (`ESTATEV` = 0), X/Y/Z/F homed.

**Phase B, X baseline.** Read `APOS`, `CPOS`, `CPOSCMD`, `LPOSMIN`, `LPOSMAX`, `CACCSTOP`, `CSPDCMD`, `CACCCMD`, `LCONVERGEBAND` and `SRH`. Refuse with nothing written unless all of these hold:
- `APOS` = `CPOS` = **0** exactly. Gate 4 is defined only from home: the targets are absolute, and going out to +2,268 and back must stay in [0, 50,000];
- `LPOSMIN` ≤ 0 and `LPOSMAX` ≥ 2,268, so neither target is clamped;
- `CSPDCMD` = 15.0 and `CACCCMD` = 1.0 exactly (the power-up values, so the speed is the known one);
- `CACCSTOP` = 1.0 exactly, so the stop is the verified one;
- 0 < `LCONVERGEBAND` ≤ 50 counts (≤ 22 µm), a sane settle band, recorded;
- `SRH` bit 9 set (not moving).

**Phase C, outward leg:**
1. M1 (`CPOSCMD` = 2,268), then M3 (`ESTATE` = 8).
2. Poll `SRH` and `ESTATEV` every 50 ms, **up to 3 s** (about 20× the expected move time).
3. Success when `SRH` bit 9 is set **and** `ESTATEV` = 13.
4. Then read `APOS` and `CPOS`. Require |`APOS` − 2,268| ≤ `LCONVERGEBAND` and `CPOS` = 2,268.

**Phase D, pause:** 1.5 s, so the operator can see the new position.

**Phase E, return leg:**
1. M2 (`CPOSCMD` = 0), then M3 (`ESTATE` = 8). From state 13 this is `MOVEPOSAGAIN`.
2. Poll as in Phase C.
3. Require |`APOS`| ≤ `LCONVERGEBAND` and `CPOS` = 0.

**Phase F, back to idle:** send Gate 3's verified controlled stop (W1, W2, W3) so X leaves state 13 for **0**. Later gates require `ESTATEV` = 0, and the stop retargets to `APOS`, which is within the band of 0.

**Phase G, final checks (read-only):**
- `ESTATEV` = 0 on all six axes;
- X `APOS`/`CPOS` within the band of 0;
- `CACCSTOP`, `CSPDCMD` and `CACCCMD` unchanged.

**Pass** only if every phase passed.

## 5. Abort rules
- **Any failure before M1:** nothing is written.
- **A move does not complete within 3 s**, a bad acknowledgement, a lost sync, or an out-of-band position after a leg:
  1. send **Gate 3's controlled stop once**, the verified stop, now for the first time possibly during motion;
  2. read the state;
  3. **send nothing else**: no return move and no retry.

  Report it. The operator decides the recovery: power cycle, which re-homes the axis.
- **If the stop's own acknowledgement fails:** send nothing more. The operator should switch the instrument off if anything is still moving.
- No halt, unhalt or `AXISOFF` in any path.

## 6. Hazards

| # | Hazard | Control |
|---|---|---|
| G4-1 | **Collision** with a vessel, the objective or an obstruction | 1 mm move inside the power-up box (0–22 mm); the operator confirms an empty stage and a clear path; Z is untouched |
| G4-2 | **Runaway** or a wrong target | The targets are constants (2,268 and 0); the drive clamps to `LPOSMIN`…`LPOSMAX`; refuse unless home = 0 and the limits cover the move; 3 s timeout, then the stop; the operator's power switch |
| G4-3 | **Unexpected speed** | No speed or acceleration write; refuse unless the power-up values (15.0 / 1.0) are read; expected ≈ 8 mm/s (Probable) |
| G4-4 | **Wrong axis** | X destination only (constants); all-axis checks before and after |
| G4-5 | **Completion misjudged**, so the next command lands mid-move | The vendor's own test (`SRH` bit 9) **and** `ESTATEV` = 13 **and** a position check; the timeout leads to a stop, not a next move |
| G4-6 | **Position not reached or lost** (stall, missed steps on this closed-loop stepper) | Encoder `APOS` check after each leg, within `LCONVERGEBAND`; on failure, stop and report, with no return move |
| G4-7 | **X left in state 13**, blocking later gates | Phase F returns it to 0 through the verified stop |
| G4-8 | **The stop during motion is untested** | Only used on a fault path in Gate 4. Its deliberate test is the next gate (§8) |
| G4-9 | **Direction unknown** (whether +counts means left or right) | Both targets lie in [0, 2,268], inside the clamp; the operator records the direction for the docs |

## 7. Evidence
Git-ignored JSON with:
- every frame, acknowledgement and reply, with timings;
- the baseline;
- the poll trace for each leg (`SRH`, `ESTATEV` and `APOS` per poll);
- the end positions and checks.

The operator's observation (direction, distance, smoothness, noise) is added to the spec afterwards. The tool would be registered in `REVIEWED_MAINTENANCE` (ADR-0019); the command core still refuses motor commands.

## 8. Not covered, and what comes next
- **Not covered:** Y, Z, F, O and P; any other target; speed, acceleration or limit writes; halt; the desktop app.
- **Next gate (proposed):** a **stop during motion** test on X. A longer move (for example to 10 mm, still inside 22 mm) with Gate 3's controlled stop sent partway, then a check that X decelerates to rest short of the target, and a return to 0. A 1 mm move (≈ 0.15 s) is too short to interrupt reliably.

## 9. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement the §4 tool (fake device only) | **Approved and implemented** (`maintenance/motor_gate4_x_move.py`, ADR-0019); fake chain only | operator | 2026-11-13 |
| Run it once on the instrument (empty stage, operator watching X) | **Approved; run once: PASS (§10)** | operator | 2026-11-13 |

## 10. First run (record `motor-gate4-20261112T193713-pass.json`): PASS, the first OpenEVOS-commanded motion

- **Baseline:** all axes idle and homed; X `APOS` = `CPOS` = 0; limits 0…50,000; `CSPDCMD` 15.0, `CACCCMD` 1.0, `CACCSTOP` 1.0; **`LCONVERGEBAND` = 5 counts (≈ 2.2 µm)**; `SRH` = `0x033F` (bit 9 set).
- **Writes:** all 7 (M1, M3, M2, M3, W1–W3) were each acknowledged by `O`.

| Leg | Poll 1 (`SRH`, `ESTATEV`) | Poll 2 | End `APOS` / `CPOS` | Error |
|---|---|---|---|---|
| Out to +2,268 | `0x013F` (bit 9 clear = moving), 13 | `0x033F` (bit 9 set), 13 | 2,268 / 2,268 | **0 counts** |
| Home to 0 | `0x013F`, 13 | `0x033F`, 13 | 0 / 0 | **0 counts** |

- **Final:** X `ESTATEV` = 0 after the stop; all six axes idle; `CACCSTOP`, speed and acceleration unchanged.

### What this establishes
- **Verified:** OpenEVOS can command a closed-loop X move and its return. The encoder lands **exactly** on target (0 counts error) both ways, well inside the 5-count band.
- **Verified: `ESTATEV` is already 13 while the axis is still moving.** The first poll, about 0.1 s after the request, showed 13 with `SRH` bit 9 clear. So **state 13 alone does not mean "finished"**; `SRH` bit 9 (the vendor's own test) does. Gate 4 required both, which was correct, and later tools must keep using `SRH` bit 9.
- **Timing:** each leg completed between the first and second poll, i.e. within about 0.2–0.5 s of the request. That is consistent with the ≈ 0.15 s estimate at ≈ 8 mm/s.
- **Operator observation (2026-11-13):**
  - **it moved, about 1 mm, to the operator's right** (facing the instrument), and back;
  - nothing unusual was reported;
  - **so +X counts = stage moves to the right, seen from the front.**
