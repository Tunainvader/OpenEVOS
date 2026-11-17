# Motor Gate 14: F past 128,000, raising the drive's RAM limit in small steps

ADR-0043 (Proposed). Level L4, the first OpenEVOS write to a drive *limit*. Module
`src/openevos/maintenance/motor_gate14_f_beyond_mirror.py`, registry `motor.f_beyond_mirror_steps`; tests
`tests/test_maintenance_motor_gate14_f_beyond_mirror.py` (7, fake F drive). The tests cover:
- a stepped pass with restore;
- a hard stop (timeout);
- high peak current;
- a drive ISR flag with `MER` clear;
- baseline refusals.

**Status: built and dry-run checked (2026-11-15, default `--up-to 130000`); not run on the instrument.**

## Why
Gate 13 (ADR-0042, Strongly supported) showed the mirror's clear aperture is centred **beyond** 128,000, the drive's
`LPOSMAX`.
- At 128,000 the right-hand ~5–25 % of the colour field is cut by the aperture edge (50 % point ≈ 1,950 of 2,048 px).
- The edge moves about 0.1 px per count near 128,000.
- PearlScope's calibration puts the mirror ≈ 10,900 counts further out.

A fully open field needs F > 128,000, and the drive refuses that: `CLAMPTOLIMITS` bounds every move to `LPOSMAX`.

## What limits F beyond 128,000 (offline decode of `FAxis.sw`, 2026-11-15)
- **`LPOSMAX` (0x03EC) is a RAM user variable.**
  - `INIT_ONCE` writes 128,000 at every power-up (`STATE_1_INIT` runs it while `IINITONCEDONE` ≠ 12,345).
  - It is referenced only by `CLAMPTOLIMITS` (0x44E0).
  - Writing it changes nothing stored: no EEPROM, setup or `.sw` write. A power cycle restores 128,000.
- **The physical end of travel is Unknown.**
  - The program homes to the negative limit switch (`HOMEEPI_LSN`, F = 0).
  - It has a *find the max hard stop* state (`STATE_9_MAX` → `FINDMAXHARDSTOP` 0x45F4). That state drives into the
    stop under a torque limit (homing mode 29, `HOME_TORQUE_LEVEL`). `INIT_ONCE` never runs it, and `LMAXHARDSTOP`
    stays 0.
  - **This gate does not use it:** finding the stop by hitting it is the risk we want to avoid.
- **Best available evidence of safe travel:** PearlScope's own `FAxisPositionLimits` `lMax` = 131,500 in its frame.
  With the ≈ 2,770 offset measured on slots 1–4 (spread ±70), that is ≈ **134,270 drive counts**. The vendor software
  allowed moves to there. **This gate's ceiling is 134,000**, just inside it.
- **Backstops already in the drive (decoded):**

  | Handler | Fires on | It does | Flag set | MER bit cleared |
  |---|---|---|---|---|
  | `_INT2_ISR` (0x46D1) | Probable: over-current, as on Y | `CPOS` = `APOS`, `STOP`, `AXISOFF`, `ESTATEV`/`ESTATE` 0 | `ISTATUSFLAGS` 0x0001 | 8 |
  | `_INT3_ISR` (0x46F8) | Probable: control error, position error > `ERRMAX` 113 | same | 0x0100 | 3 |

  **These handlers clear their `MER` bit**, so after a stall `MER` can read clear. This gate checks `ISTATUSFLAGS`
  0x0101, not only `MER`. `STATE_2_GOTO_POS` clears 0x0101 at each move start, so a set bit belongs to the current move.
- **Peak current per move:** `UPDATECURRENTMAXMIN` (0x46BA), called every main loop, keeps `IIQMAX` (0x03FA) /
  `IIQMIN` (0x03FB) = max/min of `IQ` since the last `STATE_2_GOTO_POS` (which zeroes both). Contact with a stop
  shows as a peak well above a free move's.

## Method: raise the limit one step at a time, with the drive clamp still bounding each step
Six fixed steps of 1,000 counts: **129,000 … 134,000**. For each step *v*:
1. `LPOSMAX` = *v*;
2. `LTARGETPOS` = *v*;
3. `ESTATE` = 2.

The clamp therefore still stops the move at exactly *v*, even if `LTARGETPOS` were wrong. `LMIRROR1POS` and the cube
positions are never written.

- `--up-to` chooses the last step from {129,000, …, 134,000} (default **130,000**). The first run is the shortest;
  later runs go further only after the previous result is reviewed.
- **Reference move first:** 128,000 → 124,000 (Gate 13's frames) → back with `ESTATE` = 17 (Gate 12). This is a free
  4,000-count move in the travel direction. Its peak |`IQ`| (`IREF`) sets the contact threshold.

### Stop conditions per step (any one → `ESTATE` = 15, then restore `LPOSMAX` = 128,000, F left where it is)
- not converged within 1,000 / 5,000 counts/s + 5 s (a stall at a hard stop shows here first);
- `ISTATUSFLAGS` & 0x0101 (a drive ISR fired) or `MER` ≠ 0;
- `SRH` bit 6 (LSP event): a positive limit switch, if F has one (Unknown);
- peak |`IQ`| of the step > **2 × `IREF`**. A 1,000-count step peaks lower than the 4,000-count reference, so this is
  conservative;
- the wrong-way guard (> 200 counts away), or `APOS` drifting > band during the dwell.

### Restore (always, after a pass or a stop)
1. **After a pass:** `ESTATE` = 17 returns F to `LMIRROR1POS` = 128,000 (inside the raised limit).
2. Then `LPOSMAX` = 128,000, read back.
3. **After a stop:** only step 2. A clamp change moves nothing, and F stays where it stopped. Recovery is Gate 12
   `--to mirror`, which refuses unless `LPOSMAX` is back to 128,000.

The run ends with the drive exactly as Gates 11–13 expect it.

## Frames (axis 5 = `00 50`; 32-bit = low word first, Gate 11 encoding)
| Write | Frame |
|---|---|
| `LPOSMAX` = 129,000 | **`08 00 50 25 EC F7 E8 00 01 49`** |
| `LPOSMAX` = 130,000 | **`08 00 50 25 EC FB D0 00 01 35`** |
| `LPOSMAX` = 131,000 | **`08 00 50 25 EC FF B8 00 01 21`** |
| `LPOSMAX` = 132,000 | **`08 00 50 25 EC 03 A0 00 02 0E`** |
| `LPOSMAX` = 133,000 | **`08 00 50 25 EC 07 88 00 02 FA`** |
| `LPOSMAX` = 134,000 | **`08 00 50 25 EC 0B 70 00 02 E6`** |
| `LPOSMAX` = 128,000 (restore) | **`08 00 50 25 EC F4 00 00 01 5E`** |
| `LTARGETPOS` = 129,000 | **`08 00 50 25 B7 F7 E8 00 01 14`** |
| `LTARGETPOS` = 130,000 | **`08 00 50 25 B7 FB D0 00 01 00`** |
| `LTARGETPOS` = 131,000 | **`08 00 50 25 B7 FF B8 00 01 EC`** |
| `LTARGETPOS` = 132,000 | **`08 00 50 25 B7 03 A0 00 02 D9`** |
| `LTARGETPOS` = 133,000 | **`08 00 50 25 B7 07 88 00 02 C5`** |
| `LTARGETPOS` = 134,000 | **`08 00 50 25 B7 0B 70 00 02 B1`** |
| `LTARGETPOS` = 124,000 (reference, Gate 13) | **`08 00 50 25 B7 E4 60 00 01 79`** |
| `ESTATE` = 2 / 17 / 15 | **`06 00 50 21 B5 00 02 2E`** / **`06 00 50 21 B5 00 11 3D`** / **`06 00 50 21 B5 00 0F 3B`** |

New reads (16-bit, read-only, F program variables from `variables.cfg`): `IIQMAX` 0x03FA, `IIQMIN` 0x03FB. Every
other read is Gate 9 rev 3's F set.

## Procedure (one approved run inside one brightfield hold, as Gate 13)
1. **Preconditions:**
   - F at 128,000 (Gate 12);
   - app closed; empty stage, lid closed;
   - the LED session's `--only brightfield --hold 120`;
   - the colour recorder at 20 ms (`centring_record.py`). With the colour lag fix (ADR-0039 update) the recorder needs
     no discard; it keeps one anyway.
2. **Gate**, dry run by default. Phrase `RAISE F LIMIT TO <up-to> ON COM3`, `--instrument-on-2min`, `--nobody-looking`.
   - Baseline, refusing with nothing written unless all hold:
     - the reviewed stored positions;
     - limits exactly 0/128,000;
     - F at 128,000 at rest;
     - `MER` 0 and `ISTATUSFLAGS` & 0x0101 clear;
     - band 1–50.
   - Then the reference move, the steps up to `--up-to` (8 s dwell each, read `APOS`/`MER`/`SRH`/`ISTATUSFLAGS`/
     `IIQMAX`/`IIQMIN`), and the restore.
   - Time: reference ≈ 15 s, 6 steps × ≈ 9 s, restore ≈ 3 s. That fits in 120 s.
3. **Evidence:** `motor-gate14-*.json`, with per-step UTC dwell windows, peak currents, flags and the restore read-back.
4. **Analysis:** arc-edge column per step (Gate 13 method). The step where the 50 % edge leaves the frame and the field
   becomes even estimates a new mirror position.
5. **Making it the default** (a new `LMIRROR1POS` and `LPOSMAX` written at each app start) is a later, separate decision.
   It is not part of this gate.

## Hazards
| ID | Hazard | Control |
|---|---|---|
| G14-1 | F hits an unknown hard stop | 1,000-count steps; ceiling 134,000 inside PearlScope's own limit (≈ 134,270); per-step timeout, ISR flags, `MER`, 2 × reference current; F `STOP`; drive ISRs stop and power off on over-current or position error |
| G14-2 | The raised limit is left in place | Restore after both pass and stop, with read-back; a power cycle also restores; Gates 11–13 refuse unless limits are 0/128,000 |
| G14-3 | Wrong target or limit | Only fixed allow-listed frames; `LPOSMAX` written before each move, so the clamp bounds the step to exactly *v*; no position argument |
| G14-4 | A fault clears `MER` | `ISTATUSFLAGS` 0x0101 checked (decoded ISR behaviour) |
| G14-5 | Persisting anything | Only RAM user variables; no EEPROM or setup or `.sw` write (`TS_DownloadSwFile` stays banned, ADR-0009) |
| G14-6 | Near-UV | Brightfield only; epi off; `--nobody-looking` |

## Open questions for the reviewer
- Is 134,000 the right ceiling, given the ±70-count spread in the PearlScope-to-drive offset?
- Is 2 × the reference peak current a sensible threshold, or should the first run log currents only, with no threshold
  (the 1,000-count steps and the drive ISRs as the guard)?

## Results

### Run 1: `--up-to 130000`, **PASS** (2026-11-15 19:38 UTC, user approved; `motor-gate14-20261115T193848-pass`)
- Brightfield hold by the LED session from 19:37:31Z to 19:39:31Z (off confirmed by read-back,
  `led-application-gate4-light-20261115T193931-complete`), default level, epi never on. Recorder: `colour-centring-20261115T193755-log.json`,
  121 frames at 20 ms.
- Reference move 128,000 → 124,000 → 128,000: peak |`IQ`| **3,061** (threshold 6,122).
- Steps 129,000 and 130,000 both converged:
  - peak |`IQ`| **2,915** and **2,977**, slightly *below* the reference, so no sign of contact;
  - `MER` 0, no ISR flag, no LSP event.
- F returned to 128,000, and `LPOSMAX` was restored to **128,000** (read back).

Field (mean of 64-px columns, right-hand edge columns):

| F | Columns at px ~1,700 / ~1,850 / ~1,980 | 50 % edge |
|---|---|---|
| 128,000 (before) | 94 / 77 / 49 (left-half ≈ 110) | ≈ 1,950 |
| 129,000 | 103 / 89 / 58 | ≈ 2,016 |
| 130,000 | 107 / 99 / 68 | **out of frame** (rightmost column 62 % of centre) |

The edge keeps moving right as F goes up, as Gate 13 predicted. At 130,000 the field is fully above 50 % but still
falls off in the last ~300 px, so the optimum lies further out. Next: `--up-to 132000` (and 134,000 if the currents
stay flat), as separately approved runs.
