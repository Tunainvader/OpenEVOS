# Motor Gate 13: F centring test, stepping short of the mirror under brightfield

ADR-0042 (Proposed). Level L4. Module `src/openevos/maintenance/motor_gate13_f_centring.py`, registered as
`REVIEWED_MAINTENANCE["motor.f_centring_steps"]`; tests `tests/test_maintenance_motor_gate13_f_centring.py` (6, fake
F drive). Status: **built and dry-run checked (2026-11-15); not run on the instrument.**

Camera recorder (operator tool, not in the repo; it uses the app's colour backend): `centring_record.py --seconds 90
--exposure 20` writes `colour-centring-<time>-log.json` (per frame: UTC, mean R/G/B, 3×3 grid, 32 column means) plus
a full frame every 4 s. A 5 s trial in the dark: 12 frames (≈ 2.4 fps with saves).

## Why
Gate 12's brightfield sweep (2026-11-15 19:03 UTC; Gate 12 spec, Results) showed the right-hand ~20–25 % of the
colour field cut off by a soft arc at F = 128,000. Two explanations:
- **A (Probable):** the mirror is not centred at 128,000. PearlScope's calibration puts it ≈ 10,900 counts further
  out, beyond the drive's `LPOSMAX`.
- **B:** misalignment in the brightfield illumination (condenser or field stop), independent of F.

**Test:** image the same brightfield field at F positions short of 128,000.
- If the arc **moves** with F (it should cover more of the field as F moves away from the mirror's centre), A holds.
- If it **stays put**, B holds.
- The step size per position also gives the counts → field-shift scale. That tells us how far out the true centre is,
  and whether raising `LPOSMAX` (a separate, larger review) would be worth it.

## Why a new gate (and what it gives up)
Gates 11 and 12 never send a position: the drive moves to its own stored `LCUBEnPOS`/`LMIRROR1POS`. No stored position
lies between 102,000 and 128,000, so this test **needs the PC to send an absolute target**. It uses the path PearlScope
itself uses (`FAxisMoveToPos`, motor L0 §"PC-side positioning methods"): a 32-bit `LTARGETPOS`, then `ESTATE` = 2
(`STATE_2_GOTO_POS`).

The extra risk is a wrong target. Three controls bound it:
1. **Fixed constants only.** The gate holds four precomputed target frames; there is no CLI argument for a position.
   `_Link.write` refuses any other frame (allow-list, as all motor gates).
2. **All targets are inside the reviewed range**, between cube 4 (102,000) and the mirror (128,000). They are below
   the `LPOSMAX` that the gate reads back first.
3. **The drive clamps** every `STATE_2_GOTO_POS` target to `LPOSMIN`/`LPOSMAX` (`CLAMPTOLIMITS` 0x44E0, decoded for
   Gate 12). Even a corrupted frame cannot leave 0–128,000.

## Drive behaviour relied on (offline decode, Gate 12 spec)
- `ESTATE` = 2: idle copies it to `ESTATEV`; `STATE_2_GOTO_POS` (0x4147) reads `LTARGETPOS`, clamps, sets
  `LTARGETPOSV`, clears flags 0x4000/0x8000, `AXISON`, `CPOS` = target, drive's own `CSPDCMD`/`CACCCMD`, `UPD`,
  `ESTATEV` 0.
- This is the same state that GOTO_CUBE and GOTO_MIRR jump into. Gate 11 and Gate 12 runs 1–3 exercised it with targets
  0 and 128,000. Only the source of `LTARGETPOS` differs.
- Completion: `ISTATUSFLAGS` 0x4000 with |`APOS` − `LTARGETPOSV`| ≤ `LCONVERGEBAND`.

## Positions and frames (axis 5 = `00 50`)
32-bit writes put the low word first, each word big-endian (Gate 11 `slot_frame` encoding). Frames below are computed
with the Gate 11 builder. The `ESTATE` = 2 frame equals PearlScope's decoded `FAxisMoveToPos` frame byte for byte.

| Step | Target (counts) | Short of mirror | Frame |
|---|---|---|---|
| P1 | 124,000 | 4,000 | **`08 00 50 25 B7 E4 60 00 01 79`** |
| P2 | 120,000 | 8,000 | **`08 00 50 25 B7 D4 C0 00 01 C9`** |
| P3 | 116,000 | 12,000 | **`08 00 50 25 B7 C5 20 00 01 1A`** |
| P4 | 112,000 | 16,000 | **`08 00 50 25 B7 B5 80 00 01 6A`** |
| go | `ESTATE` = 2 (GOTO_POS) | — | **`06 00 50 21 B5 00 02 2E`** |
| back | `ESTATE` = 17 (GOTO_MIRR, Gate 12) | — | **`06 00 50 21 B5 00 11 3D`** |
| fault | `ESTATE` = 15 (STOP) | — | **`06 00 50 21 B5 00 0F 3B`** |

- **Why these values:** 4,000-count steps (≈ 12 % of the 34,000 cube pitch) span 16,000, half a pitch. That is enough to
  see the arc move or not move. They stay ≥ 10,000 counts clear of cube 4 (102,000) so the cube holder is not in the
  path.
- The first and last frames of the run are at 128,000, giving a repeatability check.

## Procedure (one approved run, ~60 s, inside one 120 s brightfield hold)
Three tools run together, each with its own approval:
- the LED session's brightfield hold;
- this gate (COM3);
- a camera recorder (USB).

1. **Before:** F at the mirror (Gate 12 `--to mirror`, `APOS` 128,000). App closed. Empty stage, lid closed.
2. **LED session:** `led_light_test --only brightfield --hold 120`; it messages the ON time.
3. **Camera recorder** (Gate 12's `bf_sweep.py` pattern, fixed exposure 20 ms, gain 1x):
   - records continuously for the run (no exposure change, so the one-frame lag does not matter);
   - per frame: UTC time, mean R/G/B, a 3×3 grid and a 1-D column profile (mean per 64-pixel column), for locating the
     arc edge;
   - full frames are saved only for the last frame of each dwell (6 × 9.4 MB).
4. **Gate**, dry run by default. Phrase `STEP F SHORT OF MIRROR ON COM3`, plus `--instrument-on-2min` and
   `--nobody-looking`.
   - Transport, all-axes readiness and baseline (refuse with nothing written unless all hold):
     - `LCUBEnPOS` 0/34,000/68,000/102,000;
     - `LMIRROR1POS` 128,000;
     - limits 0/128,000;
     - **F at 128,000** (|`APOS` − 128,000| ≤ band);
     - at rest;
     - `MER` clear;
     - band 1–50.
   - For P1…P4 in order:
     - write the target frame, then `ESTATE` = 2;
     - poll every 0.1 s until converged at the target;
     - record the UTC time;
     - **dwell 8 s** (camera frames);
     - read `APOS`, `MER`, `SRH`.
   - Then `ESTATE` = 17 back to 128,000, and dwell 8 s.
   - Per step: wrong-way guard (> 200 counts away from the target), timeout (distance / 5,000 + 5 s), and `MER` checked
     after each dwell. Any fault → `ESTATE` = 15, and the run stops where it is.
   - Moves are 4,000 counts (≈ 0.2 s at 25,000 counts/s), so about 50 s in all, inside the 120 s hold.
5. **Final:** F at 128,000, idle, `MER` clear. Evidence goes to `openevos-scans/motor-gate13-*.json`, with every move's
   start and converged UTC times.
6. **Analysis (offline):**
   - match the camera frames to dwell windows by UTC time;
   - find the arc edge position (column where the profile falls to 50 % of the left-half mean) per F position.
   - **Result:** edge column vs F counts. A linear shift means A; a flat line means B.
   - If A holds, extrapolate where the edge leaves the frame. That estimates the true mirror centre and the size of any
     future `LPOSMAX` change.

## Hazards
| ID | Hazard | Control |
|---|---|---|
| G13-1 | PC-chosen position | Four fixed constants in the allow-list; no position argument; the drive clamps to 0–128,000 |
| G13-2 | A cube enters the path | Lowest target 112,000, 10,000 clear of cube 4; empty stage; brightfield only (epi off) |
| G13-3 | Near-UV | Epi LEDs never on in this run (LED session `--only brightfield`); `--nobody-looking` still required |
| G13-4 | F runs away | Per-step wrong-way guard, timeout, `MER` check, F `STOP` |
| G13-5 | Three tools at once | Separate devices (COM3 / camera USB / LED USB); each refuses if its device is busy; the gate never touches the LED or camera |
| G13-6 | LED hold ends mid-run | Gate fits in ≈ 50 s of the 120 s; start the gate within 30 s of LED ON. A late finish only loses camera data; motion is unaffected |
| G13-7 | Ends away from the mirror after a fault | Reported; recovery is Gate 12 `--to mirror` or Gate 11 `--slot 1`, each separately approved |

## Not in scope
- Changing `LPOSMAX` or any stored position (that would be a larger review, and only if A holds and the extrapolation
  says it is worth it).
- LED level control (not reviewed; board default only).
- The colour backend's one-frame lag (separate session).

## Results

### Run 1: **PASS** (2026-11-15 19:21–19:22 UTC, user approved; `motor-gate13-20261115T192240-pass`)
- Brightfield hold by the LED session: on 19:21:03Z, default level, off 19:23:03Z, confirmed by read-back
  (`led-application-gate4-light-20261115T192303-complete`); the epi LED was never on.
- Recorder `colour-centring-20261115T192123-log.json`: 150 frames, 20 ms exposure, 15 full frames.
- The gate visited 124,000 / 120,000 / 116,000 / 112,000 with 8 s dwells, then returned to 128,000 with `ESTATE` = 17.
  Every step converged, `MER` 0 throughout, final `APOS` 128,000.

Arc edge = the column where the 64-px column profile falls below 50 % of the left-half mean. Frames taken from
converged + 1 s to the end of the dwell.

| F (counts) | Frames | Mean R/G/B | Left-half mean | 50 % edge (px of 2048) |
|---|---|---|---|---|
| 128,000 (back) | 13 | 118/96/82 | 110 | ≈ 1,950 |
| 124,000 | 14 | 93/76/65 | 105 | ≈ 1,570 |
| 120,000 | 14 | 50/41/35 | 65 | ≈ 1,380 |
| 116,000 | 16 | 16/13/11 | 26 | ≈ 1,060 (whole field dimming) |
| 112,000 | 18 | 0.3 | 0.6 | dark: the aperture has left the field |

**Conclusion (Strongly supported): explanation A. The cut-off is the carriage, not the illumination.**
- The dark arc moves left across the field as F moves away from 128,000 (≈ 380 px for the first 4,000 counts). By
  112,000 the light is gone.
- Moving F *up* would move the edge right, out of the field.
- So **the mirror's clear aperture is centred beyond 128,000**, the drive's `LPOSMAX`. This agrees with PearlScope's
  calibration (≈ 138,900 drive counts).

Other observations:
- **Rough scale:** about 0.1 px per count near 128,000. The edge would probably leave the 2,048-px frame within a
  further ~1,000–3,000 counts. That is an estimate: the edge is curved and soft, so it is not linear.
- **Repeatable:** the return to 128,000 matched the 19:03 sweep at 20 ms (mean R 118.2 vs 117.8).

Getting a fully open field needs F > 128,000, which means raising the drive's `LPOSMAX`/`LMIRROR1POS`. These are RAM
variables, re-set by `INIT_ONCE` at every power-up. Raising them is out of scope here and needs its own reviewed gate.
PearlScope's own `lMax` 131,500 (≈ 134,270 drive counts) suggests the mechanism travels that far.
