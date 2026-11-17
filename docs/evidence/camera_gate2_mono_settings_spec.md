# Camera Gate 2 (mono): RAM-only settings, 16-bit full-sensor dark frames, then restore (specification, for review)

- **Status:** Revision 1, **for review**. Not implemented; not run.
- **Date:** 2026-11-13. **ADR:** [ADR-0036](../decisions/ADR-0036-camera-gate2-mono-settings.md). **Builds on:** Camera Gate 1 ([spec](camera_gate1_spec.md) §4b).
- **Tool (planned):** `python -m openevos.maintenance.camera_gate2_mono`.
- **Plan items:** M9 step 2 (dark frames) in a first, minimal form. It also gives the app backend the settings path it needs (manual exposure and gain, 16-bit).

## 1. Purpose

Gate 1 showed that at power-up the camera runs in 1280×960 8-bit, with **auto** shutter and gain and a black level of 0, so dark frames clip at zero. Gate 2 learns, changing only volatile camera registers and putting them back afterwards:
1. Does the camera accept **Format7 mode 0, 1296×964, MONO16**?
2. Do **manual** shutter, gain and black level read back as set?
3. What are the **bias level and temporal noise** of 16-bit dark frames, unclipped, at 3 exposures?
4. Does restoring the power-up state read back identical to Gate 1?

## 2. What is known

| Fact | Label |
|---|---|
| Model CMLN-13S2M, firmware 1.17.3.0, FlyCapture 2.13.3.61 | **Verified** (Gate 1) |
| Power-up state: video mode 17 (1280×960 Y8), frame-rate enum 3 (15 fps); shutter auto 12.0 ms; gain auto 0 dB; brightness 0; auto exposure auto; frame rate auto 15 fps | **Verified** (Gate 1) |
| Format7 mode 0 = 1296×964, MONO8/MONO16 | **Verified** (Gate 1) |
| Ranges: shutter 0.005–66.6 ms, gain −5.6 to 24 dB, brightness 0–6.24 % | **Verified** (Gate 1; the shutter maximum is tied to the current frame rate) |
| `fc2SetProperty` / `fc2SetFormat7Configuration` change volatile IIDC registers only. Settings persist across power cycles only via `fc2SaveToMemoryChannel` | Strongly supported (FlyCapture2 API docs; IIDC 1.31 memory-channel model) |
| At power-up the camera loads its default or memory channel 0 | Strongly supported. The tool **reads** the current channel (`fc2GetMemoryChannel`, `fc2GetMemoryChannelInfo`) and records it |
| The ICX445 ADC is 12-bit; MONO16 is left-aligned (×16 steps) | Probable. Confirmed or refuted by the frame histogram |
| A 2 % black level lifts the dark level clear of 0 | Probable. If clipping remains, the result is "clipped", not a failure |

## 3. Preconditions (any failure: nothing is opened)

1. All Gate 1 preconditions (exactly one `1E10:2005`, driver OK, SDK sees one camera).
2. **A completed Gate 1 record exists** in `openevos-scans/` for this camera serial. Its power-up state is the restore target, and the tool refuses if the live state read at step 2 differs from it.
3. **LEDs off and stage covered or room light low**, stated in the phrase.
4. Dry run by default. Exact phrase: `SET CAMERA MONO 1E10:2005 RAM ONLY AND RESTORE WITH LED OFF`.

## 4. Sequence (fixed, one attempt, any failure → restore → stop)

| # | Step | Calls |
|---|---|---|
| 1 | connect, identity | as Gate 1 |
| 2 | **Snapshot:** the full read of Gate 1 (video mode, frame rate, Format7 config, 7 properties) + memory channel and channel count; compare with the Gate 1 record | read-only |
| 3 | **Format7:** mode 0, offset 0,0, 1296×964, MONO16; validate first | `fc2ValidateFormat7Settings`, then `fc2SetFormat7Configuration` (packet size: the validated recommendation) |
| 4 | **Manual settings**, each read back and compared (tolerance: one raw step) | `fc2SetProperty`: auto exposure **off**; gain manual, **0 dB**; brightness **2.0 %**; shutter manual (step 5) |
| 5 | **Dark frames:** for shutter = **1, 10, 50 ms**: set, read back, start capture, discard the first frame, keep **3**, stop capture | `fc2SetProperty`, `fc2StartCapture`, `fc2RetrieveBuffer` ×4, `fc2StopCapture` |
| 6 | **Restore**, *always* (even after a failure at 3–5): power-up properties (values and auto flags), then video mode 17 / frame rate 3 | `fc2SetProperty` ×n, `fc2SetVideoModeAndFrameRate` |
| 7 | **Verify restore:** re-read everything from step 2 and compare | read-only |
| 8 | close | as Gate 1 |

- **Allowed setters, and only these:** `fc2ValidateFormat7Settings`, `fc2SetFormat7Configuration`, `fc2SetProperty` (types auto exposure, shutter, gain, brightness, frame rate only), and `fc2SetVideoModeAndFrameRate` (restore to the snapshot only).
- **Never:** `fc2SaveToMemoryChannel`, `fc2RestoreFromMemoryChannel`, `fc2WriteRegister*` / `fc2WriteRegisterBlock`, `fc2SetConfiguration`, LUT / strobe / trigger / GPIO calls, `fc2FireBusReset`, and any firmware call. The adapter binds none of them.
- **Values are constants in the tool:** they can't be passed on the command line. Every set value lies inside the range Gate 1 read.
- Grab timeout 5 s per frame (the Gate 1 mechanism). 13 frames in total.

## 5. Evidence

A JSON record in `openevos-scans/` with the snapshot, every set/read-back pair, and the restore comparison. Per exposure:
- mean, median and std of the 3 frames;
- temporal noise = std of the frame difference / √2;
- column/row means;
- histogram step (the 12-bit left-align check);
- count of pixels > mean + 10 σ (hot-pixel candidates, at 50 ms).

Frames are saved as `.npy`.

## 6. Pass / stop

- **Pass:**
  - every read-back matches;
  - frames are 964×1296 uint16;
  - at every exposure, the 0.1 % percentile is above 0 (unclipped) and no pixel is saturated;
  - the restore read-back equals the snapshot.
- **"Clipped" (not a pass, not a fault):** the 0.1 % percentile is 0 at 2 % brightness. The next revision raises the black level after review.
- **Stop:**
  - any SDK error, read-back mismatch, timeout or wrong frame size;
  - a bright frame (mean > 25 % of range), which means light is leaking.

  Restore still runs. **If the restore can't be verified:** report it, and tell the operator to power-cycle the instrument (volatile registers reload; Strongly supported). Then re-run Gate 1 to confirm the power-up state.

## 6a. First run, 2026-11-13 18:23 UTC (operator-approved; room dark, LEDs off)

Record: `openevos-scans/camera-gate2-mono-20261113T182310-stopped.json` plus `-1ms.npy`.
- **Settings all accepted and read back:**
  - Format7 mode 0, 1296×964 MONO16, packet 3,904 B (the recommended size, which is also the maximum);
  - auto exposure off;
  - gain 0.00 dB (raw 160);
  - brightness 2.002 % (raw 328);
  - shutter 1.012 ms (raw 76).

  All **Verified**.
- **Restore verified:** the re-read state equalled the snapshot (mode 17, rate 3, auto flags, brightness 0).
- **Stopped at 1 ms by the "bright frame" check (mean 28,337). Cause: our decoding, not light.** The camera sends Y16 **big-endian**. Read little-endian, the values looked like 4, 5, 6, 4100, 4101… Byte-swapped, the same frame is smooth:

  | Measure | Value |
  |---|---|
  | mean | 1,359 (≈ 85 in 12-bit units) |
  | spatial std | 84 |
  | 0.1 / 50 / 99.9 % percentiles | 1,104 / 1,360 / 1,632 |
  | value steps | multiples of 8, mostly 16 |

  So the 2 % black level **lifts the dark frame clear of 0 (unclipped)**. Byte order is **Verified**. That 16-bit values are 12-bit data left-aligned is Probable: the step-8 values need a closer look.
- **Fix, after the run:** `flycapture2_gate.image_to_array` decodes 16-bit data as big-endian. Tests were updated, plus a byte-order test. The 10 and 50 ms exposures were not reached, so a second run is needed for the full result.

## 6b. Second run, 2026-11-13 18:29 UTC, and revision 2

- **Run** (operator-approved; dark, LEDs off), record `camera-gate2-mono-20261113T182945-stopped.json`:
  - All settings were applied and read back as in run 1.
  - At 1 ms, `fc2RetrieveBuffer` returned **41, `FC2_ERROR_IMAGE_CONSISTENCY_ERROR`** (an incomplete frame), and the run stopped with no frames kept.
  - The **restore was verified**.
  - The same settings delivered frames in run 1, so the fault is intermittent.
  - Cause: **Probable** USB 2.0 isochronous data loss at the maximum packet size (3,904 B, used because the SDK recommended it), with the camera sharing the microscope's internal hub.
- **Revision 2 (implemented, tested, not yet run):**
  1. Packet size **75 % of the maximum**, rounded down to the SDK's unit step: 2,928 B for max 3,904 / unit 4. Lower bandwidth means a slightly lower maximum frame rate, which is irrelevant for single dark frames.
  2. An **incomplete frame (error 41) is skipped**, up to **3 per exposure**, and counted in the record (`incomplete_frames_skipped`). Retrieving the next frame is a host-side read, so nothing extra is sent to the camera. A 4th, or any other error, stops the run, followed by a restore.
  3. Everything else is unchanged.

## 6c. Revision 2 run, 2026-11-13 18:37 UTC: **PASS** (operator-approved; dark, stage covered, LEDs off)

Record: `openevos-scans/camera-gate2-mono-20261113T183712-complete.json` plus three `.npy` files (3 × 964×1296 uint16 each).
- **Packet size** 2,928 B = 6 × the SDK unit (**488 B**, not 4 as the fake assumed), with max and recommended both 3,904. **0 incomplete frames** at every exposure.
- **Memory channel** at power-up: **0** of 2 (channel 0 = factory defaults, Strongly supported). It is unchanged, since no save call is bound.
- **Restore verified:** the camera is back in mode 17 with auto shutter and gain.

All values are **Verified** (measured). The 12-bit columns are the 16-bit values ÷ 16.

| Exposure | Mean (16-bit) | Mean (12-bit) | Temporal noise (16-bit / 12-bit) | Spatial std (16-bit) | Column-mean std | Row-mean std | Max | Hot (> mean + 10σ) |
|---|---|---|---|---|---|---|---|---|
| 1 ms | 1,378.9 | 86.2 | 90.7 / **5.67** | 92.3 | 17.1 | 3.4 | 1,904 | 0 |
| 10 ms | 1,379.4 | 86.2 | 90.5 / **5.66** | 92.2 | 16.8 | 3.6 | 3,008 | 2 |
| 50 ms | 1,383.0 | 86.4 | 90.8 / **5.68** | 96.2 | 16.6 | 3.7 | 11,072 | 5 (single frame); 32 on the 3-frame mean |

What this means:
- **Unclipped:** the 0.1 % percentile is 1,104 (69 in 12-bit units) at all exposures. A 2 % black level is enough.
- **Read noise ≈ 5.7 ADU (12-bit) at 0 dB.** It doesn't depend on exposure, so dark-current shot noise is negligible up to 50 ms. Converting to electrons needs the gain in e⁻/ADU, which comes from the photon transfer curve in full M9.
- **Fixed-pattern noise is small:** about 3.8 ADU on the 3-frame mean. There is some column structure (column-mean std 17 in 16-bit, ≈ 1 ADU in 12-bit); row structure is negligible.
- **Dark current:** the mean rises only 4 counts (16-bit), 1 → 50 ms. That's below 0.3 ADU (12-bit) in 49 ms.
- **Data format:** every value is a multiple of 16 at 10 and 50 ms, consistent with **12-bit data left-aligned in 16 bits** (now Strongly supported). The step of 8 seen at 1 ms comes from a few values and is unexplained; it's a minor Unknown.
- **Hot pixels at 50 ms:** 32 candidates on the 3-frame mean. The strongest is the pair **(row 88, columns 1187–1188)**, at 8,842 and 10,874. In 1280×960 mode that is exactly (86, 1179–1180), the pair that saturated in Gate 1 run 2 (offset 2 rows, 8 columns = the centred crop). Two independent observations agree → a defect, **Strongly supported**. All 32 coordinates are in the record, as input to the M9 defect map.

**Hypotheses offered by the user for RE-5, not checked:**
- PearlScope's `PGRCamera264.dll` may use a fixed packet size below 100 % (for example 50 %, 1,952 B) because both cameras share the hub. → check by Ghidra in RE-5.
- The big-endian default comes from the IIDC `IMAGE_DATA_FORMAT` register (0x1048). → consistent with what we observed, but the register was not read.

## 7. Risks

| Risk | Mitigation |
|---|---|
| A setting persists across a power cycle | No memory-channel or register call is bound. Gate 1 re-run after the next power-up confirms it |
| A USB 2.0 bandwidth / packet-size error in Format7 | Validate first, using the SDK's recommended packet size. On error: restore, stop |
| The app or another program opens the camera at the same time | The SDK connect fails; stop |
