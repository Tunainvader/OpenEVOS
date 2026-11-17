# Motor Gate 11: F carriage (cube changer) to one cube slot

ADR-0035. Level L4 (first F motion). Module `src/openevos/maintenance/motor_gate11_f_cube.py`.

## Why
The user wants to see each cube light up (LED Gate 4). That needs the cube in the light path, so the F carriage has
to move. No F motion had been reviewed.

## What the F drive does (offline decode of `FAxis.sw`; control flow Strongly supported)
- **`STATE_16_GOTO_CUBE`** (`ESTATE` = 16):
  - `LTARGETPOS` = 1–4 selects `LCUBE1POS`–`LCUBE4POS`;
  - any other value: no move, `ESTATEV` 0;
  - then `STATE_2_GOTO_POS`.
- **`STATE_2_GOTO_POS`:**
  - `CLAMPTOLIMITS` (`LPOSMIN`/`LPOSMAX`);
  - clears the convergence flags;
  - `AXISON`;
  - loads its own `CSPDCMD`/`CACCCMD`;
  - starts the move and sets `ESTATEV` 0 at once.
- **Idle** (`STATE_0_IDLE`): sets `ISTATUSFLAGS` |= 0x4000 when |`APOS` − `LTARGETPOSV`| ≤ `LCONVERGEBAND`. After `IAXOFFTIMEOUT` it switches the axis off (flag 0x8000). F is normally off when idle.
- **`STATE_15_STOP`** (`ESTATE` = 15): `STOP`, `AXISOFF`, `CPOS = APOS`, `ESTATEV` 0.

## Drive values read on 2026-11-13 (Gate 9 rev 3, `motor-gate9-20261113T175045`)
- **Stored cube positions:** `LCUBE1POS`–`LCUBE4POS` = **0 / 34,000 / 68,000 / 102,000**; `LMIRROR1POS` 128,000.
- **Limits:** 0–128,000.
- **Motion and settle:** `CSPDCMD` 20.0; `LCONVERGEBAND` 5; `ERRMAX` 113.
- **State:** idle, homed, `MER` 0.
- **The calibration file's `FAxisLocationMotorPosition[1..4]`** (−2,757 / 31,167 / 65,302 / 99,222) sit ~2,770 counts lower, so they are measured from another zero. They are NOT used.
- **Slot 1 = 0 is confirmed physically:** the user's photo shows RFP in the light path at F = 0.

## Cube contents (LED Gate 3 `0x47` + user)
| Slot | Content | Status |
|---|---|---|
| 1 | RFP (AMEP4652) | Verified by photo |
| 2 | empty | user saw the gap |
| 3 | DAPI | user saw it (near-UV: nobody may look into the light path) |
| 4 | GFP | Unconfirmed |

## Frames (axis 5 = `00 50`)
| Write | Frame |
|---|---|
| `LTARGETPOS` = 1 | **`08 00 50 25 B7 00 01 00 00 35`** |
| `LTARGETPOS` = 2 | **`08 00 50 25 B7 00 02 00 00 36`** |
| `LTARGETPOS` = 3 | **`08 00 50 25 B7 00 03 00 00 37`** |
| `LTARGETPOS` = 4 | **`08 00 50 25 B7 00 04 00 00 38`** |
| `ESTATE` = 16 (GOTO_CUBE) | **`06 00 50 21 B5 00 10 3C`** |
| `ESTATE` = 15 (STOP) | **`06 00 50 21 B5 00 0F 3B`** |

The PC never sends a position: the drive moves to its own stored slot position.

## Procedure (one slot per approved run)
1. Dry run by default. With `--slot n --send --instrument-on-2min --nobody-looking --confirm "MOVE F TO CUBE n ON COMn"`:
   Gate 1 rev 2 transport, all-axes readiness, F baseline.
2. **Refuse with nothing written** unless all of these hold:
   - the stored slot positions are exactly 0/34,000/68,000/102,000;
   - the target is inside `LPOSMIN`/`LPOSMAX`;
   - F is at rest;
   - `MER` is clear;
   - `LCONVERGEBAND` is between 1 and 50.
3. Write `LTARGETPOS` = n, then `ESTATE` = 16. Poll `ISTATUSFLAGS` and `APOS` every 0.1 s.
   - **Done:** bit 0x4000 set and `APOS` within `LCONVERGEBAND` of `LCUBEnPOS`.
   - **Fault:** moving more than 200 counts *away* from the target, or a timeout (distance / 5,000 counts/s + 5 s). Then send `ESTATE` = 15 (F's own stop).
4. Final: `ESTATEV` 0, `APOS` within band, `MER` clear.
5. Then the LED session confirms the slot with `led_status_read` (the one-hot cube sensor) and runs `led_light_test`.

## Hazards
| ID | Hazard | Control |
|---|---|---|
| G11-1 | Near-UV (DAPI) exposure | `--nobody-looking` required; LED off during the move (the LED tests are separate) |
| G11-2 | Wrong target | PC sends only 1–4; the drive's stored positions are checked first; the drive clamps to its limits |
| G11-3 | F runs away | Wrong-way guard, timeout, F `STOP` |
| G11-4 | Only F has been reviewed for this | X/Y are not touched; Y's open fault is unaffected |
