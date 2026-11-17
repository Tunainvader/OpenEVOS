# Motor Gates 17–19: Z focus and O objective turret, first reads and first moves

ADR-0052 (Proposed). Levels: Gate 17 L1 (reads only), Gate 18 L4 (first Z motion), Gate 19 L4 (first O motion).
Gate 16 and ADR-0046 are reserved for another session.

## Why
- **The request:** the operator wants focus (Z, axis 4) and objective rotation (O, axis 6) driven from the app.
- **Nothing has moved yet:**
  - OpenEVOS has never sent either axis a write.
  - Their program variables have never been read on the unit.
  - The vendor host's Z-move and objective-change code is not decompiled.
- **So, as for X/Y and F,** the order is: read first, then one small reviewed move per axis, then the app.

## What is known (source: offline decode of `ZAxis.sw` / `OAxis.sw` and their `variables.cfg`; unit reads)
Control-flow readings are **Strongly supported**; opcode meanings follow L0 §3a, so they are inferred.

### Z (focus, axis 4, destination `00 40`)

**Variables:**

| Variable | Address | Notes |
|---|---|---|
| `HOMEDONE` | 0x03B0 | |
| `ESTATE` / `ESTATEV` | 0x03B1 / 0x03B2 | |
| `CPOSCMD` | 0x03B3 | LONG, the target |
| `CSPDCMD` / `CACCCMD` | 0x03B5 / 0x03B7 | |
| `CACCSTOP` | 0x03BB | |
| `LPOSMIN` / `LPOSMAX` | 0x03BD / 0x03BF | |
| `IIQMAX` / `IIQMIN` | 0x03C4 / 0x03C5 | |
| `IPENDINGMOVES` | 0x03C8 | |
| `LCONVERGEBAND` | 0x03D3 | |
| `BDOAXISOFF` | 0x03D7 | |
| `LTARGETPOSV` | 0x03DB | |
| `IVERSION` | 0x03E2 | |
| `LHOMEINDEXDELTA` | 0x03E8 | |
| `ISTATUSFLAGS` | 0x03F5 | |
| `IINITONCEDONE` | 0x03F6 | |

**States:**
- **`STATE_8_MOVE_POS`:** clamps `CPOSCMD` to `LPOSMIN`..`LPOSMAX`, switches the axis on, moves, then `ESTATEV` 13.
- **`STATE_6_STOP`:** decelerates at `CACCSTOP`, sets `CPOS` = `APOS`, `ESTATEV` 0. The axis stays powered.
- **Z is powered at rest:** `SRL` 0xC400 in all 12 Gate 9 reads.

**Homing at power-up:**
- Z drives negative to the limit switch, then seeks the encoder index; the index becomes 0.
- **`LPOSMIN` = −`LHOMEINDEXDELTA`.** Gate 2 read −1,770 … −1,793 over 7 power-ups.
- `LPOSMAX` is 67,000 (8.18 mm). Z sits at 0 after homing.

**Other values:**
- **Units:** 5,000 counts per 0.61 mm, so 1 count = 0.122 µm.
- **Speed:** `CSPDCMD` 10.0 ≈ 1.5 mm/s (calculated, unverified for Z).
- **Calibration:** `ZAxisPositionLimits` −1,990 … 67,000.

**Direction:** **Unknown physically.**
- Homing goes negative to the switch.
- A PearlScope string implies lower Z is "below the focus point".
- Probable: negative = objective away from the sample, so Z = 0 is close to fully retracted. **Gate 18 settles this.**

### O (objective turret, axis 6, destination `00 60`)

**Variables:**

| Variable | Address | Notes |
|---|---|---|
| `ESTATE` / `ESTATEV` | 0x03B1 / 0x03B2 | |
| `INUMTURRETPOSITIONS` | 0x03C9 | 5 |
| `IHF` | 0x03CA | raw 3-bit position sensor |
| `ICURRENTPOSHF` | 0x03CB | 1–5; 0 = unknown |
| `ITARGETPOS` | 0x03CC | |
| `IMOVEPOSITIVE` | 0x03CD | 1 |
| `LGOPASTAMOUNT` | 0x03CE | 9,600 |
| `LDEBOUNCEJUMP` | 0x03D0 | 1,000 |
| `LTICKSPERGENEVAREV` | 0x03D2 | 76,800 |
| `LTICKSPERTURRETREV` | 0x03D4 | 384,000 |
| `IPENDINGMOVES` | 0x03DE | |
| `LSTUCKDELTA` | 0x03E0 | 76,800 |
| `ISTATUSFLAGS` | 0x03E4 | |
| `IVERSION` | 0x03E8 | 7 |

O has no `IINITONCEDONE` and no stored positions: the position is absolute, from the sensor.

**`STATE_3_GOTO_POS`** (`ESTATE` = 3):
- switches the axis on and rotates one way (`IMOVEPOSITIVE`) until `ICURRENTPOSHF` == `ITARGETPOS`;
- then a debounce jump, a re-check, and `LGOPASTAMOUNT`;
- then `AXISOFF`, `ESTATEV` 0, `IPENDINGMOVES` 0.

**Stuck detection:** no sensor change within `LSTUCKDELTA` sets `ISTATUSFLAGS` |= 2, `AXISOFF`, state 0.

**Hazards in the program:**
- `ITARGETPOS` outside 1–5 can never match, so it probably rotates forever.
- State 2 (`FIND_POS`) has no stuck check, and state 4 never exits. Neither may be sent.

**At rest:** O is switched off (the program ends every move with `AXISOFF`; power-up also ends with `AXISOFF`).

**Abort:** O has no stop state. The vendor's `HaltAllMotors` sends O only `AXISOFF` (`04 00 60 00 02 66`, vendor-observed). The drive then coasts, and the turret detent holds.

**Same sensor as the LED board:** probable. The LED board's 0x47 reads it independently (byte 3; it has read position 2, raw 7, nine times), and the decode tables agree pair for pair.

### This unit's objectives (calibration `tpco` + `-Parfocalities`; Probable)

| Turret position | Objective | Parfocality (colour / mono, units Unknown) |
|---|---|---|
| 1 | 4X Plan PH | −1,120 / −1,120 |
| 2 | 10X LPlan FL PH | −1,551 / −1,522 |
| 3 | 20X LPlan FL PH | −1,491 / −1,462 |
| 4 | 40X LPlan FL PH | −1,528 / −1,517 |
| 5 | 60X Plan FL | −1,464 / −1,488 |

The sensor says position 2 is in the path (10X). The operator's note says position 1 = 4X and position 4 = 40X, which agrees. Gate 17 and a look at the turret settle which lens is in the light path.

### No turret-safe height exists in vendor data
- The app's `turret_safe_z_um` = 1,500 is a simulation default.
- Until a measured value exists, **an objective change is allowed only with Z at its home position (|`APOS`| ≤ band, Z = 0)**, which is believed to be the most retracted position (confirmed by Gate 18), and with **no vessel on the stage**.

## Gate 17: Z and O reads (L1, no writes)
- **Reads (allowlisted 16/32-bit reads only):**
  - Z: every variable in the Z table above, plus `APOS`, `CPOS`, `SRL`, `SRH`, `MER`;
  - O: every variable in the O table above, plus `APOS`, `CPOS`, `SRL`, `SRH`, `MER`;
  - each read 3 times, 1 s apart.
- **Pass (reads only):**
  - every decoded constant reads as decoded: Z `IVERSION` 21, `LCONVERGEBAND` 5, `LPOSMAX` 67,000, `LPOSMIN` = −`LHOMEINDEXDELTA`; O `INUMTURRETPOSITIONS` 5, the tick constants, `IVERSION` 7, `IMOVEPOSITIVE` 1;
  - Z idle at 0, homed, with `MER` 0;
  - O `ICURRENTPOSHF` in 1–5 and stable, O `ESTATEV` 0, `IPENDINGMOVES` 0, O `SRL` bit 15 = 0 (off at rest).
- **Recorded, not judged:**
  - Z `ISTATUSFLAGS`, `HOMEDONE`, `IIQMAX`/`IIQMIN`;
  - O `IHF` against the LED board's raw sensor value (`led_status_read`, run separately by the LED session).
- **Any constant that differs** stops Gates 18 and 19 until it is reviewed.
- **Needs:** the app closed (COM3), and the instrument on and homed ≥ 2 min. Nothing moves.

### Running Gate 17
With the OpenEVOS app closed, and the instrument on and homed for at least 2 minutes:

```
python -m openevos.maintenance.motor_gate17_z_o_reads --send --instrument-on-2min --confirm "READ Z AND TURRET ON COM3"
```

- **What it sends:** 42 reads, three times each, 1 s apart. Nothing moves.
- **Evidence:** `openevos-scans/motor-gate17-<time>-<pass|fail>.json` (git-ignored).
- **Output:** a failure lists every mismatch. Notes report the turret position and Z's homing result.

## Gate 18: first Z move, 1 mm out and back (L4)
**Frames** (computed from the cfg like Gates 3/4 for X; never vendor-observed):

| Write | Frame |
|---|---|
| `CPOSCMD` = n | `08 00 40 25 B3 <lo word BE> <hi word BE> cs` |
| `ESTATE` = 8 (MOVE_POS) | `06 00 40 21 B1 00 08 20` |
| `ESTATE` = 6 (STOP) | `06 00 40 21 B1 00 06 1E` |

Only two targets can be built: **+8,200 counts (1.000 mm)** and **0**. The first draft said +400 (49 µm); that is too small to see by eye, and the point of the gate is to see the direction. 1 mm is an eighth of Z's 8.2 mm travel, so it is still far from any focus position with an empty stage. Speed and acceleration are never written; Z uses its power-up 10.0 / 0.25.

**Sequence:**
1. **Phase A, idle stop** (as Gate 3 did for X): `ESTATE` = 6 at rest. Expect no motion, `ESTATEV` 0, `APOS` unchanged, `SRL` still on.
2. **Phase B, the move:**
   1. `CPOSCMD` = +8,200, then `ESTATE` = 8;
   2. done when `SRH` motion complete, `ESTATEV` 13 and `CPOS` = 8,200, then `APOS` within `LCONVERGEBAND`;
   3. hold 3 s at +1 mm, so the operator can see where the objective went;
   4. `CPOSCMD` = 0, `ESTATE` = 8, back to 0;
   5. `ESTATE` = 6 at rest, returning Z to state 0, as Gate 4 does for X.
3. **The operator watches the objective** and records the direction ("towards the stage" / "away"). This sets the sign convention for everything after.

**Refuse with nothing written** unless:
- all axes are idle and homed;
- Z is at 0 ± band, with `MER` 0;
- `LPOSMIN` < 0 < 8,200 < `LPOSMAX`;
- Z's speed, acceleration and stop deceleration are the power-up 10.0 / 0.25 / 1.0, and Z is powered (`SRL` bit 15);
- **the stage is empty** (no vessel, no slide; operator flag) and **nobody's hands are near the objectives**.

**Fault handling (as built, Gate 4's leg logic):**
- Each of these sends Z's `ESTATE` = 6 once, reads the state, and sends nothing else:
  - the idle stop changes Z;
  - a leg does not complete within 5 s (the move takes < 1 s at ~12,500 counts/s);
  - the drive ends a leg short of its target;
  - a leg lands outside `LCONVERGEBAND`;
  - `APOS` and `CPOS` disagree by more than 3 counts before a leg.
- Final checks: Z idle at 0 ± band, still powered, `MER` clear, speed/acceleration/limits unchanged, and every axis
  idle.

**Main hazard:** collision with the vessel holder if positive is up. 1 mm from the homed bottom of an 8.2 mm travel, with an empty stage, cannot reach it.

**Running it** (app closed; empty stage; operator watching the objective, with a hand near the power switch):

```
python -m openevos.maintenance.motor_gate18_z_move --send --instrument-on-2min --stage-empty-watching --confirm "MOVE Z 1MM AND BACK ON COM3"
```

Evidence: `openevos-scans/motor-gate18-<time>-<outcome>.json`. Afterwards the operator reports which way the objective
moved on the +1 mm leg.

## Gate 19: first objective change, one position (L4)
**Frames** (computed from the cfg; never vendor-observed):

| Write | Frame |
|---|---|
| `ITARGETPOS` = n (1–5 only) | `06 00 60 21 CC 00 0n cs` |
| `ESTATE` = 3 (GOTO_POS, the state with stuck detection) | `06 00 60 21 B1 00 03 3B` |
| Abort: `AXISOFF` | `04 00 60 00 02 66` (vendor `HaltAllMotors`) |

States 2 and 4 cannot be built.

**Target:** one step from the current position in the drive's direction (`IMOVEPOSITIVE` 1), for example 2 → 3. A second approved run returns to the start; that run goes all the way round through 4, 5 and 1, because the program only turns one way.

**Refuse with nothing written** unless:
- Gate 17 passed;
- **Z is at 0 ± band and idle** (retracted), and Gate 18 has confirmed that Z = 0 is the retracted end;
- the stage is empty;
- O is idle and off, with `ICURRENTPOSHF` in 1–5;
- every other axis is idle.

**Done:** `ICURRENTPOSHF` = n, `ESTATEV` 0, `IPENDINGMOVES` 0, `ISTATUSFLAGS` bit 1 (stuck) clear, and O off again. Then the LED board's 0x47 objective position must agree (LED session).

**Fault:** stuck flag, or a timeout (one Geneva revolution at the slowest plausible speed + 5 s) sends `AXISOFF`. Any position other than n means stop and review.

**Hazards:**
- the turret sweeps the objectives under the stage, so a vessel or hands there can be struck: empty stage, Z retracted;
- the 60X lens may be immersion: inspect for oil first.

### Gate 19 as built
- **Target:** `--to n`, 1–5, not the current position. The turret turns one way (`IMOVEPOSITIVE` 1) and passes the
  positions in between. 4X → 10X is one step; 10X back to 4X goes round through 20X, 40X and 60X.
- **Refuse with nothing written** unless:
  - every axis is idle and homed;
  - Z is retracted at 0 ± band, with `MER` clear;
  - O's constants are as Gate 17 read them;
  - O's position is known (`ICURRENTPOSHF` 1–5, agreeing with `IHF`), and O is idle, off, not stuck, with `MER` clear.
- **Poll** every ~0.5 s: O state, position, flags, pending moves, `APOS`, `SRL`, and Z `APOS`.
- **Done:** position = n, `ESTATEV` 0, `IPENDINGMOVES` 0, and O switched off, on 2 polls in a row.
- **Fault:** each of these sends `AXISOFF` to O once, and nothing else:
  - the stuck flag;
  - a position not on the path, such as going past n;
  - Z moving;
  - a timeout (steps × (76,800 + 9,600 + 1,000) ticks at a slowest-plausible 10,000 ticks/s + 5 s).
- **Not judged:** whether every intermediate position was seen, because a fast pass can fall between polls. It is
  recorded.

**Running it** (app closed; empty stage; no oil on any objective; hands clear of the turret; operator watching with a
hand near the power switch):

```
python -m openevos.maintenance.motor_gate19_o_turret --to 2 --send --instrument-on-2min --stage-empty-z-down --confirm "TURN TURRET TO POSITION 2 ON COM3"
```

## After the gates (separate ADR)
- **Z in the app:**
  - the focus device becomes `motor.focus`, moving inside `LPOSMIN`..`LPOSMAX` less a margin, with the stop on STOP;
  - a Z drift/landing check like X/Y.
- **O in the app:**
  - an objective change runs Z → 0 (retract), O → n, then Z → the old focus + Δparfocal, once the parfocality units are known (until then, Z stays at 0 and the operator refocuses);
  - the objective buttons follow `ICURRENTPOSHF`.

## Open items
- the physical Z direction (Gate 18);
- which lens is in the light path (Gate 17 + a look);
- the parfocality units;
- a measured turret-safe Z;
- whether the 60X is an oil lens;
- PearlScope's `GoToObjectiveTurretPosition` / `ZAxisMoveToPos` bodies (not decompiled; a decompile could replace the computed frames with vendor-observed ones).

## Decision record
| Item | Decision | By | Date |
|---|---|---|---|
| Build Gate 17 (reads only) | **Approved and built** (`maintenance/motor_gate17_z_o_reads.py`, tests in `tests/test_maintenance_motor_gate17_z_o_reads.py`) | operator | 2026-11-17 |
| Run Gate 17 | **Approved; run once: PASS** (`motor-gate17-20261117T094907-pass`) | operator | 2026-11-17 |
| Build Gate 18 | **Approved and built** (`maintenance/motor_gate18_z_move.py`; registered `motor.z_move_1mm_and_back`) | operator | 2026-11-17 |
| Run Gate 18 | **Approved (empty stage, hands clear, operator watching); run once: PASS** (`motor-gate18-20261117T100721-pass`): encoder +8,200 and back, 0 counts error; **operator saw no movement** at 1 mm | operator | 2026-11-17 |
| Gate 18 `--mm 5` (41,000 counts, 61 % of LPOSMAX 67,000; phrase `MOVE Z 5MM AND BACK ON COMn`; leg timeout 10 s) | **Asked for by the operator and built; run once: PASS** (`motor-gate18-20261117T101842-pass`): encoder +41,000 and back to 0 exactly, Z powered, `MER` clear, all axes idle | operator | 2026-11-17 |

### Gate 18 result: Z direction (Verified, operator observation 2026-11-17)
- **Positive Z moves the objective TOWARDS the stage.** The operator saw it rise on the +5 mm leg and come back down
  on the return.
- **Z = 0, the homed position just above the negative limit switch, is the most retracted position.** Z cannot go
  below `LPOSMIN` (−1,779), about 0.22 mm lower.
- This confirms the interim rule for Gate 19: an objective change happens with Z at 0, the retracted end.
- In the app, focus "up" = positive counts. A focus move may only increase Z towards a vessel within the drive's
  `LPOSMAX` 67,000 (8.18 mm). A per-objective safe ceiling still needs measuring before a vessel is on the stage.
| Build Gate 19 | **Approved and built** (`maintenance/motor_gate19_o_turret.py`; registered `motor.o_turret_goto_position`; `--to n`, any position 1–5, the turret turning one way through the others) | operator | 2026-11-17 |
| Run Gate 19 `--to 2` | **Approved (empty stage, no oil, hands clear, watching); run once: FAULT_STOPPED** (`motor-gate19-20261117T103802-fault_stopped`) | operator | 2026-11-17 |

### Gate 19 first run (2026-11-17 10:38 UTC): fault-stopped, turret left between positions
**Trace** (each poll ~3.4 s, because Gate 4's link waits 50 ms per read and syncs before every read):

| t (s) | `ESTATEV` | position | `IHF` | `SRL` | O `APOS` | Z `APOS` |
|---|---|---|---|---|---|---|
| 3.36 | 3 | 1 | 4 | 0xC000 (on) | 0 | 0 |
| 6.76 | 3 | **5** | 12 | 0xC000 | 0 | 0 |

**What happened:**
- The drive turned the turret from 1 to **5**, not to 2. The guard saw a position off the assumed path (1 → 2) and
  sent `AXISOFF` once.
- **After the `AXISOFF`:** `SRL` 0x4000 (off), position **0** (`IHF` 14, between positions), `ESTATEV` still 3,
  `ISTATUSFLAGS` 0x1000 (stuck flag not set), `MER` not read.
- Z stayed at 0 throughout.

**Findings:**
1. **Direction:** with `IMOVEPOSITIVE` 1 the turret turns so that position numbers **decrease**: 1 → 5 → 4 → 3 → 2.
   The spec's "positive = increasing" was wrong. The drive would have reached 2 the long way round. The fault was in
   the gate's path check, not in the drive.
2. **O `APOS` stays 0 while turning.** O has no encoder feedback in `APOS`, so `APOS` cannot be used to watch O.
3. **The poll is too slow** (3.4 s) to see each position pass. A revision needs the app's fast reads (Gate 15's
   `fast`, ~16 ms per read).
4. **The turret is now between positions, with the drive program still in state 3** and the motor off. Whether the
   program's stuck detection will end state 3 by itself, with the axis off, is Unknown.
5. **Recovery is a power cycle.** But O does not move at power-up: it only reads its sensor. So after a power cycle
   the turret will still be between positions (position 0), and Gate 19 as built will refuse to move it.
6. **COLLISION (operator, 2026-11-17): an objective struck the underside of the stage while the turret turned,**
   with Z fully retracted at 0 and an empty stage. So **Z = 0 is NOT a turret-safe height on its own.**
   - The likely factor is the X/Y stage position: the objectives swing on an arc, and need the stage opening, not the
     stage frame, above them. Not yet established.
   - **All O motion is stopped until the cause is understood and a clearance rule is measured.** Gate 19 must not be
     run again, in any form, before then.
   - **Do not power-cycle while an objective may still touch the stage.** Power-on homing moves X/Y (and Z
     slightly), which could drag the stage across the objective.
   - **Next steps:**
     1. The operator inspects the objective and the stage for damage, and frees the turret by hand if it is pressing.
     2. **X/Y at the time: 0, 0** (the homed corner): the last app session's stage log
        (`stage-log-20261117T092948.csv`) ends at X 0, Y 0, and no X/Y move has run since. So the turret turned under
        the stage's homed corner, not under the middle of its opening.
     3. Find how PearlScope makes objective changes safe (a decompile of `GoToObjectiveTurretPosition`, or the manual).
   - **Operator, 2026-11-17:** the objective looks undamaged. The operator turned the turret **by hand** onto the
     objective that had struck the stage, so it now has clearance. The drive has not been read since; its program was
     last seen in state 3, with the motor off.

   - **Gate 17 re-read after the hand move** (`motor-gate17-20261117T110410-fail`):
     - O is at **position 4 (40X)**, `IHF` 10 (consistent), motor off (`ISTATUSFLAGS` 0x1000, `MER` clear).
     - **The program is still in state 3 (GOTO_POS), with `ITARGETPOS` 2** from run 1. Nothing turns it while the axis
       is off, but the state must be cleared before any further O write.
     - A power cycle clears it: O's INIT reads the sensor, sets `ITARGETPOS` = current, `AXISOFF`, state 0, and does not
       move.
     - Z is unchanged: homed at 0.

   - **After the power cycle** (`motor-gate17-20261117T110636-pass`): **PASS.**
     - O at position 4 (40X), state 0, `ITARGETPOS` 4: the leftover state 3 is cleared.
     - Z homed at 0, `LHOMEINDEXDELTA` 1,798 (1,779 before: index scatter of 19 counts, about 2.3 µm).
     - The LED board came up in its bootloader (0DAD:0057).

### Before Gate 19 rev 2 run 2 (2026-11-17)
- The app's drift lock from 08:15 UTC was still set. Y had drifted −7 → −25 counts at rest, with `IQ` 3,042.
- Two power cycles had happened since, but none with the app watching. With the operator's OK, the lock file was
  archived (`stage-latch-cleared-20261117T1138-after-two-power-cycles.json`) rather than waiting for a third.
- Gate 7 then loaded the calibrated limits (`motor-gate7-20261117T113842-pass`). The stage was moved in the app to
  X 59.500, Y 39.000 mm (verified by read-back), and the app was closed.

### Gate 19 rev 2, run 2 (`--to 3`, 11:43 UTC): PASS (`motor-gate19-20261117T114342-pass`)
- **Setup:** stage centred, Z at 0; run in a Terminal tab with the operator on Ctrl+C.
- **Result:** turret 4 (40X) → 3 (20X) in 5.6 s, sensor positions seen [4, 3]. O off and idle, not stuck, `MER`
  clear. Z unchanged at 0; all axes idle.
- **Direction confirmed:** positions decrease as the turret turns.
- **Operator: clean, no contact.**

### Gate 19 rev 2, full cycle (11:45–11:47 UTC): 5 of 5 PASS
Stage centred (X 59.5, Y 39.0 mm), Z at 0, one step per run, in a Terminal tab with Ctrl+C:

| Step | Time | Evidence |
|---|---|---|
| 3 → 2 (10X) | 5.28 s | `motor-gate19-20261117T114514-pass` |
| 2 → 1 (4X) | 6.09 s | `…T114550-pass` |
| **1 → 5 (60X)**, the step that struck the stage at X/Y 0, 0 in run 1 | 6.09 s | `…T114625-pass` |
| 5 → 4 (40X) | 5.28 s | `…T114659-pass` |
| 4 → 3 (20X) | 5.30 s | `…T114734-pass` |

- **On every step:** sensor positions seen [from, to]; O off, idle, not stuck, `MER` clear; Z unchanged at 0; all axes
  idle.
- **One step takes 5.3–6.1 s,** so a Geneva step turns at about 14,000–16,000 ticks/s (76,800 + 9,600 + 1,000 ticks).
- **Rule established:** the turret turns safely with the stage centred over its opening and Z retracted at 0. Run 1's
  collision was with the stage at its homed corner.
- **Still open:** the exact clear region (only the centre is proven), and the lowest Z that is safe per objective for
  turning with a vessel on the stage.

### Gate 19 rev 2 (built 2026-11-17, after run 1)
- **Direction:** positions **decrease** as the turret turns (1 → 5 → 4 → 3 → 2). The path check follows that.
- **Stage:** the stage must be inside X 40–80 mm, Y 20–60 mm (over its opening, away from the homed corner where the
  collision happened), as well as Z retracted at 0.
- **Polling:** the app's fast reads, ~0.15 s per poll (run 1's was 3.4 s). O `APOS` is no longer polled (it stays 0).
- **Ctrl+C:** during the turn it sends `AXISOFF` at once. Run it in a terminal the operator can reach.
- **Starting between positions:** `--from-between k` lets the turret start between positions, given the last one it
  passed.
- Tests: `tests/test_maintenance_motor_gate19_o_turret.py` (8).

### Gate 17 result (2026-11-17 09:49 UTC): PASS
- **Decoded constants:** every one read as decoded, and stayed the same across the three reads:
  - Z: `IVERSION` 21, `LCONVERGEBAND` 5, `LPOSMAX` 67,000, `CSPDCMD` 10.0, `CACCCMD` 0.25, `CACCSTOP` 1.0;
  - O: `IVERSION` 7, 5 positions, the tick constants, `IMOVEPOSITIVE` 1.
- **Z:**
  - homed (`HOMEDONE` 1), idle at `APOS` = `CPOS` = 0;
  - `LPOSMIN` −1,779 = −`LHOMEINDEXDELTA` (1,779), confirming the homing decode;
  - powered at rest (`SRL` 0xC400), `ISTATUSFLAGS` 0xD000, `MER` 0;
  - peak current since homing 1,674 / −1,557.
- **O:**
  - idle and **switched off at rest** (`SRL` 0x4400), as decoded;
  - `ICURRENTPOSHF` **1** (`IHF` 4, which decodes to 1), `ITARGETPOS` 1, `ISTATUSFLAGS` 0x1000, `MER` 0.
- **Open: the turret position disagrees with the LED board's earlier reads.**
  - The LED board's 0x47 reported objective position **2** (raw 7) in nine reads on 13 and 15 Nov. The drive now reports **1**.
  - Either the turret was turned since, or the drive's and the board's sensor tables do not match as assumed.
  - To settle it: read 0x47 again now, and have the operator look at which objective is under the light path. This must be resolved before Gate 19.
  - **Operator, 2026-11-17: the 4X is under the light path.** That agrees with the drive (position 1) and with `tpco`
    (position 1 = 4X). The drive's own sensor decode is now Verified for position 1.
  - Still open: whether the LED board's "2" on 13–15 Nov meant the turret was at the 10X then, or that the board's
    0x47 position table is shifted by one. A fresh 0x47 read now would tell. Until then, the app must take the turret
    position from the drive (`ICURRENTPOSHF`), not from the LED board.
