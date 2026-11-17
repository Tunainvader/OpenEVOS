# Motor Gate 6: first Y movement, +12 mm and back to 0 (specification)

- **Status:** implemented (ADR-0021) and **run once on 2026-11-13: PASS** (§6). Y moved +12 mm and back with 0 counts error.
- **Date:** 2026-11-13. The operator asked to "move y 12 mm".
- **Builds on:**
  - Gate 4 (X 1 mm, 0 error);
  - Gate 5 (stop during motion on X; speed units confirmed);
  - Gate 2 (Y power-up limits 0…50,000 ≈ 22 mm; `CACCSTOP` 1.0).
- **Classification:** L4, moves hardware, drive RAM writes only.

## 1. Why Y can reuse X's verified behaviour (checked offline, 2026-11-13)
- **Same variable addresses:** every variable used (`ESTATE`, `ESTATEV`, `IPENDINGMOVES`, `CACCSTOP`, `CPOSCMD`, `LCONVERGEBAND`, `SRH`, `APOS`, `CPOS`, `LPOSMIN`/`LPOSMAX`, `CSPDCMD`/`CACCCMD`) is at the same address in Y's `variables.cfg` as in X's.
- **Same program:** Y's `.sw` has the same 128 labels at the same addresses. It differs from X's in only 21 words:
  - one start-up constant (`0x400C`: `0x0457` on X, `0x08AE` on Y, probably the homing start delay);
  - setup-table tuning values (`0x5Exx`/`0x5Fxx`).

  The state-machine code (move, stop, clamp, convergence) is identical.
- **Same units:** 2,880 counts per 1.27 mm. 12 mm = **27,213 counts** (12.0002 mm).
- **Direction is unknown.** It is either towards or away from the operator; the operator records it.

## 2. Writes (constants, Y only: destination `00 20`; each after a line sync, each acknowledged by `O`)

| Write | Frame |
|---|---|
| Y1 `CPOSCMD` = 27,213 | `08 00 20 25 D4 6A 4D 00 00 D8` |
| Y2 `CPOSCMD` = 0 | `08 00 20 25 D4 00 00 00 00 21` |
| Y3 `ESTATE` = 8 | `06 00 20 21 B3 00 08 02` |
| Y stop | `06 00 20 21 CD 00 01 15` · `08 00 20 25 E8 00 00 00 01 36` · `06 00 20 21 B3 00 06 00` |

**Reads:** readiness on all six axes, plus Y's own reads. No X frame and no other write can be sent; the per-link allowlist enforces this.

## 3. Sequence
**As Gate 4, on Y:**
- the phrase `MOVE Y 12MM AND BACK ON COMn`;
- the operator confirms the instrument has been on ≥ 2 minutes, **the stage is empty, ~13 mm is clear in both Y directions**, and they are watching with a hand near the power switch;
- readiness;
- the Y baseline: at exactly 0, the limits cover 0…27,213, power-up speed, acceleration and stop deceleration, band 1…50, `SRH` bit 9 set;
- out to +27,213: completion = `SRH` bit 9 and `ESTATEV` 13; **timeout 5 s** (expected ≈ 1.5 s at ≈ 8.3 mm/s); then the position within band;
- pause 1.5 s; back to 0;
- the Y stop to idle;
- the final checks.

**Faults:** one Y stop, then nothing else; recovery by power cycle.

## 4. Hazards
- **Collision over 12 mm:** the stage is empty and the path is clear in both directions (the direction is unknown).
- **Runaway:** constant targets; the drive clamps to 0…50,000 (≈ 22 mm); a 5 s timeout leads to the stop.
- **The stop on Y is untested on hardware,** but its code is identical to X's, which was verified idle (Gate 3) and in motion (Gate 5). It is used only on a fault path.

## 5. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Build (fake device only) | Implemented, per "move y 12 mm" | operator | 2026-11-13 |
| Run once on the instrument | **Run: PASS (§6)** | operator | 2026-11-13 |

## 6. First run (record `motor-gate6-20261112T195237-pass.json`): PASS

- **Baseline:** Y at exactly 0; band 5 counts.
- **Out:** to **27,213 (12.000 mm)**. `SRH` bit 9 was clear on polls 1–2 and set on poll 3; the move took ≈ 1.2–1.6 s, as predicted at ≈ 8.3 mm/s. End `APOS` = `CPOS` = 27,213: **0 counts error**.
- **Back:** to 0, with **0 counts error**.
- **Afterwards:** the Y stop returned Y to idle; all six axes idle; parameters unchanged. All writes were acknowledged by `O`.
- **Operator observation (2026-11-13):** **Y moved away from the operator** (facing the instrument) and back. So **+Y counts = stage moves away, towards the back**; with X, +X = right.
- **What this establishes:** Y moves, settles and returns exactly like X. The Y program's equivalence to X holds in practice, and the Y stop frames work (as the final idle stop, not in motion).
