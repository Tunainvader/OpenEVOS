# Camera Gate 2 (colour): RAM-only settings, raw Bayer dark frames, then restore (specification, for review)

- **Status:** Revision 1. **Implemented and tested against a fake DLL. Dry run OK on the instrument (2026-11-15 14:54 UTC). Not run with `--send`.**
- **Date:** 2026-11-15. **ADR:** [ADR-0038](../decisions/ADR-0038-camera-gate2-colour-settings.md).
- **Builds on:** colour Gate 1 ([spec](camera_gate1_spec.md) §4e–4f) and the mono Gate 2 pattern ([spec](camera_gate2_mono_settings_spec.md), ADR-0036).
- **Tool (planned):** `python -m openevos.maintenance.camera_gate2_colour`, with the adapter in `backends/ueye/sdk.py` (a settings subclass of `UeyeGateBackend`).
- **Plan items:** M10 step 2. It also gives the app's colour backend its settings path: raw Bayer, manual exposure, known black level.

## 1. Purpose

Gate 1 showed that after `is_InitCamera` the camera runs 8-bit BGR, at 199.7 ms exposure (the maximum at 24 MHz pixel clock), with all auto functions off. Its black-level-corrected output reads **0** in the dark, so dark frames are clipped and carry no noise information. Gate 2 learns, with RAM-only changes inside one camera session:

1. Which **raw Bayer** colour mode does the camera accept? Candidates are tried in order: RAW16, RAW12, RAW10, RAW8.
2. Do **manual exposure**, master gain 0 and a raised **black-level offset** read back as set?
3. What are the **bias level and temporal noise** of unclipped raw dark frames, at 3 exposures?
4. After `is_ExitCamera` and a fresh `is_InitCamera`, does the camera come back in exactly the Gate 1 state? That is the restore check.

## 2. What is known

| Fact | Label |
|---|---|
| UI146xLE-C, Bayer, 2048×1536, 3.2 µm, rolling shutter; R/G/B and master gain present; uEye API 4.97.4111 | **Verified** (Gate 1 §4f) |
| State after init: BGR8_PACKED; area 2048×1536 at 0,0; pixel clock 24 MHz (range 5–43); 5.0 fps; exposure 199.69 ms (range 0.10–199.69, step 0.10); master gain 0; R/G/B 5/0/3; auto gain, shutter, white balance and frame rate all off | **Verified** (Gate 1 §4f) |
| Dark 8-bit output is 0, with 52 / 9.4 M samples at 6 or below | **Verified** (Gate 1 §4f) |
| uEye settings live in driver and camera RAM for the session. `is_ExitCamera` drops them, and the next `is_InitCamera` applies the driver defaults. Nothing persists unless a parameter set is written to the camera EEPROM (`is_ParameterSet` save) or a "load at startup" option is set | Strongly supported (uEye API manual model). Gate 1's state matches driver defaults, which supports that no startup parameter set is active |
| The camera holds its operating image in RAM only. The driver loads it at each power-up ("uEye boot" `1409:1000` → `1409:1465`) | **Verified** (PnP, enumeration evidence §4) |
| The UI-146xLE supports at least one raw Bayer mode; which bit depths it supports is unknown. The sensor ADC is 10-bit | Probable. Answered by step 3 |
| A raised black-level offset lifts raw dark frames off 0 | Probable. If they still clip, the result is "clipped", not a fault |

## 3. Preconditions (any failure: nothing is opened)

1. All Gate 1 preconditions: exactly one `1409:1465`, driver OK, DLL loads, SDK sees one camera, camera not "in use".
2. **A completed colour Gate 1 record exists** in `openevos-scans/`. It is the restore reference: the tool refuses if the state read at step 2 differs from it.
3. **LEDs off, and the stage covered or the room light low**, as stated in the phrase.
4. Dry run by default. Exact phrase: `SET CAMERA COLOUR 1409:1465 RAM ONLY AND RESTORE WITH LED OFF`.

## 4. Sequence (fixed, one attempt; any failure → restore → stop)

| # | Step | Calls |
|---|---|---|
| 1 | connect, identity | as Gate 1 (init with handle 0, no flags) |
| 2 | **Snapshot:** the full Gate 1 read (colour mode, area, exposure and range, pixel clock, frame rate, 4 gains, 4 auto flags), plus black-level mode and offset (and its range), plus gain boost. Compare with the Gate 1 record | read-only |
| 3 | **Colour mode:** try `IS_CM_SENSOR_RAW16` (29), then RAW12 (27), RAW10 (33), then RAW8 (11). Stop at the first one accepted and read it back. A rejected mode returns an error and changes nothing; the tool records it as "unsupported" | `is_SetColorMode` |
| 4 | **Manual settings**, each read back: master gain **0** (R/G/B left as they are and recorded); black-level auto **off**; black-level offset **constant B** (see §7) | `is_SetHardwareGain(0, −1, −1, −1)`, `is_Blacklevel` (SET_MODE off, SET_OFFSET) |
| 5 | **Dark frames:** for exposure **1, 10, 50 ms**: set, read back, discard the first frame, keep **3** | `is_Exposure` SET (12) then GET (7); host buffer as in Gate 1; `is_FreezeVideo` ×4 |
| 6 | **Restore**, *always*, even after a failure at steps 3–5: free the buffer, then `is_ExitCamera` | `is_ExitCamera` |
| 7 | **Verify the restore:** `is_InitCamera` again (handle 0, no flags). Re-read everything from step 2 and compare with the snapshot. Then `is_ExitCamera` | as Gate 1 |

- **Setters allowed, with guards over their arguments, and only these:**
  - `is_SetColorMode`: only the 4 raw candidates;
  - `is_SetHardwareGain`: only `(0, −1, −1, −1)`;
  - `is_Blacklevel`: only SET_MODE = off and SET_OFFSET = B;
  - `is_Exposure` SET: only 1.0, 10.0 or 50.0 ms.

  These are added to the Gate 1 allow-list in a subclass. Gate 1's own adapter stays read-only.
- **Never bound:** `is_ParameterSet` / `is_SaveParameters` / `is_LoadParameters`, EEPROM (`is_WriteEEPROM`), `is_Configuration`, LUT/gamma setters, `is_IO`, trigger, `is_SetCameraID`, `is_PixelClock` SET, `is_SetFrameRate` set, `is_AOI` set, the starter-upload flag, and anything firmware-related. Pixel clock (24 MHz), frame rate (5 fps) and area stay at their defaults. 50 ms fits easily within 199.7 ms.
- **Values are constants in the tool.** The command line can't change them, and each lies inside a range Gate 1 or step 2 read.
- **Frames:** `is_FreezeVideo` with a 5 s timeout, 12 grabs in total. An `IS_TIMED_OUT` (122) or transfer error on a grab is skipped, up to 3 per exposure, and counted (the mono revision 2 rule). A 4th, or any other error, stops the run, followed by the restore.
- **USB 2:** the camera shares the microscope's internal hub with the Chameleon. The tool refuses to start if the OpenEVOS app is running with the real mono camera, a check of the process list. Gate 1 already ran with the hub otherwise idle.

## 5. Evidence

A JSON record in `openevos-scans/` with:
- the snapshot;
- every colour mode tried, with its result;
- every set/read-back pair;
- the restore comparison.

Per exposure, raw (H, W) uint16 (or uint8 for RAW8), split into the four Bayer planes using `upper_left_bayer` from Gate 1 (0 = R? to be confirmed from the frame):
- mean, median and std for each plane and overall;
- temporal noise = std(frame₁ − frame₂) / √2;
- 0.1 % / 99.9 % percentiles;
- histogram step (the bit-alignment check: whether RAW16 is 10-bit data shifted, and by how much);
- column and row means;
- count of pixels > mean + 10 σ at 50 ms (hot-pixel candidates).

Frames are saved as `.npy`. Serial numbers go in the scan only.

## 6. Pass / stop

- **Pass:**
  - a raw mode is accepted;
  - every read-back matches (exposure within one 0.10 ms step);
  - frames are 1536×2048 (2-D raw);
  - at every exposure, the 0.1 % percentile is above 0 (unclipped) and nothing is saturated;
  - after re-init, the restore read equals the snapshot.
- **"Clipped" (not a pass, not a fault):** still 0 at offset B. Revision 2 raises B after review.
- **Stop:**
  - any other SDK error, a read-back mismatch, a 4th skipped grab, or a wrong frame shape;
  - a bright frame (mean > 25 % of range, meaning light is leaking);
  - **no raw mode accepted.** The result then records which modes failed. That is information, not damage; the follow-up uses 8-bit BGR with a raised offset.

  The restore always runs. **If the restore can't be verified:** report it, and tell the operator to power-cycle the instrument. The driver reloads the camera's RAM image and defaults at power-up, which is Verified for the image and Strongly supported for the settings. Then re-run Gate 1.

## 6a. First run, 2026-11-15 14:56 UTC (operator-approved; LEDs off, app closed): stopped, restore verified

Record: `camera-gate2-colour-20261115T145624-stopped.json`.
- **Snapshot** matched the Gate 1 record.
- **Black level as found:**
  - caps **2**, meaning offset settable, auto switch **not** settable;
  - mode **1**, auto **on** (default 1);
  - offset 0, default 0, range 0–255 step 1.

  Gain boost 0.
- **Raw modes:** RAW16, RAW12 and RAW10 were **rejected**, and **SENSOR_RAW8 was accepted** and read back. So the UI-146xLE delivers **8-bit raw only** through this API. **Verified.**
- Master gain 0 was set and read back as 0.
- **Stopped at "manual settings":** `is_Blacklevel` SET_MODE (auto off) returned **155, "not supported"**. This matches caps 2. No frames were taken.
- **Restore verified:** after exit and a fresh init, the state equalled the snapshot (BGR8, 199.7 ms, gains 0/5/0/3, black-level auto, offset 0).

**Revision 2 (implemented, tested, not yet run):**
- The auto-black-level switch is called only if the caps include `IS_BLACKLEVEL_CAP_SET_AUTO_BLACKLEVEL` (1).
- If the caps lack `…CAP_SET_OFFSET` (2), the run stops before any setter.
- On this camera the auto black level stays on, and the offset B (64 = 25 % of 0–255) is set on top of it. That auto correction plus a positive offset lifts dark frames off 0 is **Probable**: it is the uEye manual's model, and it is tested by this run.
- The read-back expects mode unchanged (1) and offset B.
- Raw data are 8-bit (RAW8), so the dark statistics are uint8 with `data_bits` 8.
- Everything else is unchanged.
- Tests: 12, including a fake that behaves like run 1 (RAW8 only, caps 2, SET_MODE → 155).

## 6b. Revision 2 run, 2026-11-15 15:01 UTC: **PASS** (operator-approved; LEDs off, app closed)

Record: `camera-gate2-colour-20261115T150149-complete.json` plus three `.npy` files (3 × 1536×2048 uint8 each).

**Settings, all read back:**
- SENSOR_RAW8;
- master gain 0;
- black level auto (unchanged, not settable) with offset **64**;
- exposure 1 / 10 / 50 ms read back as **0.915** / 9.959 / 49.9995 ms. The 1 ms value is the nearest step at 24 MHz: 9 × 0.1016 ms, within the one-step tolerance.

**Grabs:** 0 skipped. **Restore verified:** after exit and re-init, the state equalled the snapshot.

**Dark raw 8-bit frames:**

| | 1 ms | 10 ms | 50 ms |
|---|---|---|---|
| mean (DN) | 3.70 | 3.71 | 3.73 |
| median / 0.1 % / 99.9 % | 4 / 3 / 4 | 4 / 3 / 4 | 4 / 3 / 4 |
| temporal noise (DN) | 0.44 | 0.44 | 0.43 |
| spatial std | 0.46 | 0.45 | 0.44 |
| column / row mean std | 0.07 / 0.08 | 0.07 / 0.08 | 0.06 / 0.07 |
| min / max | 2 / 5 | 2 / 5 | 3 / 8 |
| hot (> mean + 10 σ) / saturated | 0 / 0 | 0 / 0 | 0 / 0 |

- Bayer planes (p00 / p01 / p10 / p11) at 10 ms: 3.71 / 3.64 / 3.70 / 3.79. The planes are consistent with each other; the small p11 lift is **Probable** from the R/B channel gains (5/3) applied in the camera.
- At 50 ms, frame 1's histogram is 3: 849,194; 4: 2,296,494; 5: 39; 8: 1.

**Interpretation:**
1. **Offset 64 lifts the auto-corrected dark level to about 3.7 DN.** Dark frames are unclipped (0.1 % percentile 3), so the revision 2 premise is **Verified**. Roughly 0.06 DN per offset unit (Probable; one point).
2. **Read noise is at the 8-bit quantization floor** (about 0.44 DN; the floor for a uniform step is 0.29). The sensor's analogue noise sits below 1 DN at gain 0, so this camera's dark-frame statistics carry little information beyond "clean". **Dark current up to 50 ms is not measurable** (+0.03 DN). That is expected for a colour camera meant for brightfield and phase imaging.
3. Column and row structure are negligible, with no hot pixels at 50 ms (one pixel at 8).

**For the app backend:**
- Raw is RAW8 only.
- Offset 64 is a reasonable black pedestal.
- Demosaicing is needed for display, and the Bayer order is still to be confirmed with light.

## 7. Open points (to settle in review or before implementation)

1. **Black-level constants:**
   - The `is_Blacklevel` command codes (GET/SET_MODE, GET/SET_OFFSET, GET_OFFSET_RANGE, auto on/off) are not yet confirmed.
   - The same goes for `IS_INVALID_COLOR_FORMAT` and the gain-boost GET constant.
   - They come from IDS's `pyueye/ueye.py`, the same source as the Gate 1 constants, and are recorded in the adapter before any run.
   - **Offset B:** the default is read at step 2. B = the default offset + 25 % of the offset range, rounded and clamped. If the range read fails, the run stops and no setter is called.
2. **Bayer order:** `nUpperLeftBayerPixel` read 0. Its meaning (R, G or B at 0,0) is confirmed from a lit frame later, not from dark frames. The dark-frame statistics are reported per plane regardless.
3. **A light check:** Gate 1's frame was all zeros, so whether images come through is still only Probable. One optional extra exposure with the room light on (a separate phrase, run after the dark series) would confirm it and show the Bayer pattern. The proposal is to leave it out of Gate 2 and do it as the first approved app session with the colour backend, as for the mono camera.
5. **Settled at implementation (2026-11-15).** Constants are verbatim from IDS's `pyueye/ueye.py`:
   - black-level commands: GET_CAPS 1, GET_MODE_DEFAULT 2, GET_MODE 3, SET_MODE 4, GET_OFFSET_DEFAULT 5, GET_OFFSET_RANGE 6, GET_OFFSET 7, SET_OFFSET 8; auto off = 0;
   - `IS_INVALID_COLOR_FORMAT` 174, `IS_TRANSFER_ERROR` 178 (skipped like a timeout), `IS_GET_GAINBOOST` 0x8008.
   - Parameters are passed as C `int`, and the range as 3 ints (Probable).
   - The offset B is computed from the range and default read at step 2. If either can't be read, the run stops before any setter.
   - `is_SetColorMode` / `is_SetHardwareGain` return data for their GET queries but a status code for a set; the adapter checks the set status explicitly.
   - Refusals from the guard carry no SDK code, so a refused call can never be mistaken for "format unsupported".
   - **Code:** `backends/ueye/sdk.py` `UeyeSettingsBackend` and `offset_target`; `maintenance/camera_gate2_colour.py`.
   - **Tests:** `tests/test_maintenance_camera_gate2_colour.py` (10, stateful fake: raw-mode fallback, timeout skips, refusal of a state mismatch, the app-running refusal, setter guards).
   - **Dry run:** `camera-gate2-colour-20261115T145443-dry_run.json`.
4. **R/G/B gains (5/0/3)** are left as they are. In uEye they are applied in the camera, which affects raw data. For dark frames this matters only through noise scaling, and it is recorded.
