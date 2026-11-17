# Motor Gate 12: F carriage (epi carriage) to the mirror, and back to cube slot 1

ADR-0041. Level L4 (second F motion; first move beyond slot 4). Module `src/openevos/maintenance/motor_gate12_f_mirror.py`.
Registered as `REVIEWED_MAINTENANCE["motor.f_goto_mirror"]`.

## Why
The colour camera (UI-146xLE-C, ADR-0039) reads 0 even with a phone flashlight on the stage at 199 ms. F sits at
slot 1 (F = 0, RFP). The project's channel model (`core/channels.py`) puts brightfield at the mirror, so the mirror
position is the prime suspect for routing light to the colour camera. Gate 11 cannot reach it: `STATE_16_GOTO_CUBE`
accepts only `LTARGETPOS` 1–4.

## What the F drive does (offline decode of `FAxis.sw`, 2026-11-15; nothing sent)
Source: `FAxis.sw` (11,469 B, SHA-256 `ada4e3ee…80b5d9b`, matches the inventory) and `FAxis.t.zip/variables.cfg`.
The label names come from `variables.cfg`; the instruction reading follows the patterns already decoded for
Gate 11 (each `2Dxx yyyy` = 32-bit copy of variable `yyyy` into `0x03xx`; `21xx vvvv` = 16-bit immediate;
`7400 aaaa` = GOTO). Control flow **Strongly supported**.

- **Main-loop dispatch** (`MAINLOOP` 0x4025): `ESTATEV` 0…18 jumps to `STATE_0_IDLE` … `STATE_18_RESET_VARINIT`.
  Entry 17 (0x4082) is **`STATE_17_GOTO_MIRR` (0x41A8)**. Gate 11's spec did not list it.
- **`STATE_0_IDLE`:** if `ESTATE` ≠ 0, `ESTATEV` = `ESTATE`, `ESTATE` = 0 (`IDLE_FOUNDNEWSTATE`). So the host writes
  `ESTATE` = 17 and the drive enters state 17 on its next loop.
- **`STATE_17_GOTO_MIRR`** (0x41A8–0x41AD, three instructions):
  | Address | Words | Meaning |
  |---|---|---|
  | 0x41A8 | `2DB7 03F6` | `LTARGETPOS` = `LMIRROR1POS` |
  | 0x41AA | `21B6 0002` | `ESTATEV` = 2 |
  | 0x41AC | `7400 4147` | GOTO `STATE_2_GOTO_POS` |

  It is the same shape as `S16_POS1`–`S16_POS4` (`2DB7 03EE` … then `S16_TARGET_SET`: `ESTATEV` = 2, GOTO 0x4147).
  **It reads nothing the host writes**: `LTARGETPOS` is overwritten from the drive's own variable.
- **`STATE_2_GOTO_POS`** (0x4147), unchanged from Gate 11: `LSTACK1` = `LTARGETPOS`; CALL `CLAMPTOLIMITS`
  (0x44E0: bound to `LPOSMIN`/`LPOSMAX`); `LTARGETPOSV` = result; clear `ISTATUSFLAGS` bits 0x0101 and 0xC000;
  `AXISON`; `CPOS` = `LTARGETPOSV`; `CACC` = `CACCCMD`; `CSPD` = `CSPDCMD`; `UPD`; arm the axis-off timer;
  `ESTATEV` = 0.
- **Idle convergence:** `ISTATUSFLAGS` |= 0x4000 once motion is complete (`SRH` bit 9) and
  |`APOS` − `LTARGETPOSV`| ≤ `LCONVERGEBAND`; after `IAXOFFTIMEOUT` the drive switches the axis off (0x8000).
- **`STATE_15_STOP`:** `STOP`, `AXISOFF`, `CPOS` = `APOS`, `ESTATEV` 0 (as Gate 11).

### Where 128,000 comes from
`INIT_ONCE` (0x40D6) **hard-codes** the F positions; they are program defaults, not per-unit calibration:

| Variable | Words | Value |
|---|---|---|
| `LPOSMIN` | `25EA 0000 0000` | 0 |
| `LPOSMAX` | `25EC F400 0001` | 128,000 |
| `LCUBE1POS`–`LCUBE4POS` | `25EE…`, `25F0 84D0 0000`, `25F2 09A0 0001`, `25F4 8E70 0001` | 0 / 34,000 / 68,000 / 102,000 |
| `LMIRROR1POS` | `25F6 F400 0001` | **128,000** |
| `LCONVERGEBAND` | `25E1 0005 0000` | 5 |
| `CSPDCMD` | `25BE 0000 0014` | 20.0 (≈ 25,000 counts/s) |

Gate 9 rev 3 (`motor-gate9-20261113T175045`) read exactly these values back from the running drive.

### Open question: is 128,000 the mirror's centre? (Unknown)
PearlScope's calibration (`led-application-stepd-20261112T162611`) holds `FAxisLocationMotorPosition[5]` = **136,130**
and `FAxisPositionLimits` −2,000…131,500. Slots 1–4 in that file sit 2,757/2,833/2,698/2,778 counts below the
drive's (offset ≈ 2,770), so its mirror maps to **≈ 138,900 drive counts**, about 10,900 counts past `LPOSMAX`.
Its own `lMax` (131,500) is also below its mirror value, so the two files do not agree with each other either.
Whatever PearlScope sent, the drive's clamp ends any F move at 128,000. So 128,000 is the furthest F can go without
changing the drive's limits, which this gate never does. Whether that is fully on the mirror, partly on it, or
still beside it is **Unknown** until the camera looks. HW_DATA `EPI_MIRROR_POS` reads 0 (unprogrammed), so it does not
help.

## Command choice
| Option | PC sends | Verdict |
|---|---|---|
| **`ESTATE` = 17 (`STATE_17_GOTO_MIRR`)** | one 16-bit state request | **Chosen.** No position from the PC; the drive uses its own `LMIRROR1POS`, then the clamped GOTO_POS path. Same trust model as Gate 11 |
| `LTARGETPOS` = 128,000 + `ESTATE` = 2 (`STATE_2_GOTO_POS`, PearlScope's `FAxisMoveToPos`) | a 32-bit absolute position | Rejected. The PC would choose the position; a typo or wrong frame is bounded only by the clamp |
| `LTARGETPOS` = 5 + `ESTATE` = 16 | — | Not possible: `STATE_16_GOTO_CUBE` with 5 does nothing (`ESTATEV` 0, no move) |

## Frames (axis 5 = `00 50`)
| Write | Frame | Used by |
|---|---|---|
| `ESTATE` = 17 (GOTO_MIRR) | **`06 00 50 21 B5 00 11 3D`** | `--to mirror` |
| `LTARGETPOS` = 1 | **`08 00 50 25 B7 00 01 00 00 35`** | `--to slot1` (Gate 11's frame) |
| `ESTATE` = 16 (GOTO_CUBE) | **`06 00 50 21 B5 00 10 3C`** | `--to slot1` (Gate 11's frame) |
| `ESTATE` = 15 (STOP) | **`06 00 50 21 B5 00 0F 3B`** | fault only |

The PC never sends a position. Reads are Gate 9 rev 3's F reads plus the all-axes readiness reads, through the
Gate 4 link (every write sync-checked and acknowledged by `O`).

## Procedure (one destination per approved run)
1. Dry run by default; it prints the frames it would send and the phrase. To run:
   - mirror: `--to mirror --send --instrument-on-2min --nobody-looking --confirm "MOVE F TO MIRROR ON COM3"`
   - return: `--to slot1 --send --instrument-on-2min --nobody-looking --confirm "RETURN F TO CUBE 1 ON COM3"`

   Then: Gate 1 rev 2 transport, all-axes readiness (idle, `IINITONCEDONE` 12,345), F baseline.
2. **Refuse with nothing written** unless all of these hold:
   - `LCUBE1POS`–`LCUBE4POS` = 0/34,000/68,000/102,000 and `LMIRROR1POS` = 128,000;
   - `LPOSMIN`/`LPOSMAX` = 0/128,000 exactly (the mirror sits on `LPOSMAX`, so any other limit changes the result);
   - the target is inside the limits;
   - F is at rest (|`APOS` − `CPOS`| ≤ band);
   - `MER` is clear;
   - `LCONVERGEBAND` is between 1 and 50.
3. Write the destination's frames. Poll `ISTATUSFLAGS` and `APOS` every 0.1 s.
   - **Done:** bit 0x4000 set and `APOS` within `LCONVERGEBAND` of the stored target.
   - **Fault:** moving more than 200 counts *away* from the target, or a timeout (distance / 5,000 counts/s + 5 s,
     ≈ 30.6 s for the full 128,000). Then `ESTATE` = 15 (F's own stop).
4. **Report:** `ESTATEV`, `APOS`, `CPOS`, `LTARGETPOSV`, `ISTATUSFLAGS`, `MER`, `SRL`, `SRH`. Pass = `ESTATEV` 0,
   `APOS` within band, `MER` clear. Evidence JSON goes to the git-ignored `openevos-scans/` (`motor-gate12-*.json`).
5. With F at the mirror, the camera session repeats its light check (flashlight or transmitted LED; colour camera live
   view). Then `--to slot1` returns F to RFP whenever wanted (Gate 11 `--slot n` also works from the mirror).

## Hazards
| ID | Hazard | Control |
|---|---|---|
| G12-1 | Near-UV (DAPI) exposure as the carriage passes slot 3 | `--nobody-looking` required; LEDs off (separate tools) |
| G12-2 | Wrong target | PC sends no position; stored positions and limits checked first; the drive clamps to 0–128,000 |
| G12-3 | Mirror at the end of travel | 128,000 is the drive's own limit and inside PearlScope's own range (≈ 134,270 drive counts); `MER` (incl. LSP bit 6) and `SRH` are read after the move; fault → F `STOP` |
| G12-4 | F runs away | Wrong-way guard, timeout, F `STOP` |
| G12-5 | 128,000 may be off the mirror's centre | Reported as Unknown; this gate does not change limits or stored positions. Any correction would be a separate, reviewed gate |
| G12-6 | Only F is touched | X/Y/Z are read for readiness only |

## Results
### Run 1: `--to mirror`, **PASS** (2026-11-15 18:33 UTC, user approved; `motor-gate12-20261115T183309-pass`)
- Baseline exactly as reviewed: F at `APOS` = `CPOS` = 0 (slot 1); cubes 0/34,000/68,000/102,000;
  `LMIRROR1POS` 128,000; limits 0/128,000; `LCONVERGEBAND` 5; `MER` 0; `ISTATUSFLAGS` 0xD000 (converged, axis off).
- One write, `ESTATE` = 17, acknowledged. F moved steadily the right way, to 1,174, 21,293, 44,113 … 111,212, 127,982,
  then 128,000 with flag 0x5000 at t = 7.2 s. The gate reported completion at 10.38 s, a time that includes the reads.
- Final: `APOS` = `CPOS` = `LTARGETPOSV` = 128,000; `ESTATEV` 0; `ISTATUSFLAGS` 0x5000 (converged, axis still on);
  `MER` 0; `SRH` 0x033F (bit 9 target reached; LSP/LSN bits 6/7 clear, so no limit switch at 128,000).
- The drive clamp did not change the target (128,000 = `LPOSMAX`). Return with `--to slot1`.

### Run 2: `--to slot1`, **PASS** (2026-11-15 18:36 UTC, user approved; `motor-gate12-20261115T183621-pass`)
- Start `APOS` 128,000. Writes: `LTARGETPOS` = 1, then `ESTATE` = 16, both acknowledged.
- Steady motion the right way: 126,816 → 107,066 → … → 17,565 → 125 → 0 with flag 0x5000 at t = 7.2 s.
- Final: `APOS` 0, `ESTATEV` 0, `ISTATUSFLAGS` 0x5000, `MER` 0, `SRH` 0x033F. F is back at slot 1 (RFP).

### Run 3: `--to mirror`, **PASS** (2026-11-15 18:37 UTC, user approved; `motor-gate12-20261115T183732-pass`)
- From `APOS` 0 to 128,000 in 10.38 s (as run 1); `ESTATEV` 0, `ISTATUSFLAGS` 0x5000, `MER` 0, `SRH` 0x033F.
  F is left at the mirror position.

### Colour camera light check with F at 128,000 (2026-11-15 18:34–18:35 UTC, user approved)
Same tool and settings as the failed check at slot 1 (ADR-0039: 94 snaps at 199 ms with a phone flashlight, max 9).
- **Flashlight, 30 s at 199 ms:** 83 snaps; the brightest frame is fully saturated (mean R/G/B 255, every pixel
  255; `colour-sweep-20261115T183454-record-199ms`). At slot 1 the result was 0.
- **Sweep (light held or moved by hand, so the levels are not comparable across exposures):**
  - 5 ms: saturated.
  - 50 ms: mean R/G/B 183/182/170 (`…183505-50ms`). The frame shows a defocused bright disc edge and a few dark dust
    specks, so this is a real optical image.
  - 199 ms: mean 24/23/18, max 80 (`…183506-199ms`). A dim gradient, from room light or the flashlight moved away.
- **Conclusion (Strongly supported):** the colour camera's light path is selected by the F carriage at its
  mirror/end position. At slot 1 (RFP cube) no light reaches it. 128,000 passes enough light for a bright image;
  whether the field is fully unvignetted (the open centring question above) needs an even transmitted-LED field.
- **In the app (2026-11-15, after run 3, F at 128,000):** the user opened the colour live view with the flashlight
  and saw "just a bright white light": the frame was saturated, as in the headless 5 ms and 199 ms frames. The app's
  live colour path works end to end. A phone flashlight is too bright and too uneven to judge the image or vignetting.
- A later app session at the saved 5 ms, with the light aimed indirectly, showed black. Headless checks at 18:49–18:51:
  room light only gave 0 at 5/50/199 ms, as at slot 1. With the flashlight directly on the stage, 30 s at 50 ms gave
  92 snaps and a fully saturated brightest frame (`colour-sweep-20261115T185104-record-50ms`). The path is still open;
  the black was too little light for 5 ms.

### Brightfield (transmitted LED) sweep with F at 128,000 (2026-11-15 19:02–19:03 UTC, user approved)
The LED session ran LED Gate 4 `--only brightfield`: on at 19:02:14Z, board default level, off at 19:04:14Z, confirmed by
read-back (`led-application-gate4-light-20261115T190414-complete`); the epi LED was never on.
Empty stage. Colour camera, gain 1x, one frame per exposure, mean of R/G/B.
- **Camera note:** the first sweep (`colour-bf-20261115T190233`) showed a **one-frame lag**: each snap returned the
  frame from the previous exposure. The rerun (`colour-bf-20261115T190306`) discards one snap per exposure, and only
  it is used below. Affects the app backend: the first frame after an exposure change is stale.
- **Response is linear:**

  | Exposure (ms) | Mean R/G/B |
  |---|---|
  | 0.5 | 1.7/1.4/0.9 |
  | 1 | 4.6/3.7/3.0 |
  | 2 | 10.5/8.5/7.0 |
  | 5 | 28.4/23.1/19.4 |
  | 10 | 58.1/47.2/40.0 |
  | 20 | 117.8/95.7/81.9 (max 149, no clipping) |
  | 50 | 79 % saturated |
  | 100 | 98 % saturated |
  | 199 | 100 % saturated |

  Red is about 5.9 DN/ms. **Usable brightfield exposure at this LED level: about 20–30 ms.** Colour is warm (R > G > B), and white balance is untuned.
- **Field: the right-hand ~20–25 % is cut off** by a soft circular edge (`colour-bf-20261115T190306-20ms.png`).
  3×3 grid means at 20 ms:

  | Row | Left | Centre | Right |
  |---|---|---|---|
  | Top | 104.5 | 106.6 | 72.4 |
  | Middle | 110.8 | 110.9 | 82.7 |
  | Bottom | 114.1 | 112.3 | 72.1 |

  The right column is 25–35 % darker, and the edge is an arc. **Probable cause: the mirror is not centred at 128,000.** This
  fits the open question above: PearlScope's mirror is about 10,900 counts further out, so the aperture edge stays in view.
  Not ruled out: condenser or field-stop misalignment. **Test:** an image at a few positions short of 128,000
  (e.g. 120,000 and 124,000). If the edge moves into the field, the carriage is the cause. Going *beyond* 128,000
  would need a reviewed change to `LPOSMAX` and is out of scope.
