# Camera Gate 1: read-only identity, modes and one dark frame (specification, for review)

- **Status:** Revision 1, for review. Tool implemented and tested against a fake camera only. **Not run on the instrument.** It cannot run yet: neither camera has a driver (see [`camera_usb_enumeration_2026-11-13.md`](camera_usb_enumeration_2026-11-13.md)).
- **Date:** 2026-11-13. **ADR:** [ADR-0033](../decisions/ADR-0033-camera-gate1-readonly.md).
- **Tool:** `python -m openevos.maintenance.camera_gate1 --role mono|colour`.
- **Plan items:** M2 (finishes the camera part), M9 step 1, M10 step 1, first input to RE-5.

## 1. Purpose

For one camera role at a time, without changing anything stored in the camera:
1. Is exactly one expected camera present, with a driver bound?
2. What does the SDK report as model, serial, sensor, firmware and driver version?
3. Which video modes / pixel formats / exposure and gain ranges does it support?
4. Can it deliver **one** frame, with the LED off and the camera's power-up settings, and what are its statistics (bias level, noise)?

## 2. What is known

| Fact | Label |
|---|---|
| Mono = `1E10:2005` rev 0003, vendor class, on the microscope hub port 3 | **Verified** (enumeration 2026-11-13) |
| Mono is a Point Grey Chameleon, Sony ICX445 1296×964, FlyCapture2 | Strongly supported (PearlScope strings + usb.ids) |
| Colour = `1409:1000` rev 0000, vendor class, hub port 2, no iSerial | **Verified** |
| Colour is an IDS uEye UI-146xLE-C, uEye API | Strongly supported (model: Probable) |
| No driver or SDK installed for either | **Verified** |
| The Chameleon stores user sets / memory channels and can be firmware-updated by FlyCapture tools | Strongly supported (FlyCapture2 API) |
| uEye cameras have a user EEPROM and "save parameters to EEPROM" calls | Strongly supported (uEye API) |
| Camera power-up defaults are what PearlScope overwrote at start; their values on our units | Unknown |

## 3. Preconditions (any failure: nothing is opened)

1. **Exactly one** present PnP device with the role's VID:PID (`1E10:2005` mono, `1409:1000` colour), and its problem code is 0 (driver bound).
2. **The vendor SDK library loads** (`FlyCapture2_C.dll` mono, `ueye_api_64.dll` colour) and the SDK sees exactly one camera.
3. **LED off.** The operator includes it in the phrase (below). The tool does not command the LED board.
4. No other program holds the camera (the SDK open fails otherwise; the tool reports and stops).
5. **Dry run by default.** It checks 1–2, prints the exact call plan, opens nothing.
6. **Exact phrase:** `READ CAMERA <ROLE> <VID:PID> WITH LED OFF`, e.g. `READ CAMERA MONO 1E10:2005 WITH LED OFF`.

## 4. Allowed calls (the backend interface exposes only these)

| Step | Call | Mono (FlyCapture2 C) | Colour (uEye) |
|---|---|---|---|
| 1 | connect | `fc2Connect` to the one camera | `is_InitCamera` |
| 2 | identity | `fc2GetCameraInfo` | `is_GetCameraInfo`, `is_GetSensorInfo`, `is_GetDLLVersion` |
| 3 | modes | `fc2GetVideoModeAndFrameRateInfo`, `fc2GetFormat7Info`, `fc2GetPropertyInfo` (shutter, gain) | `is_ImageFormat` (list), `is_Exposure` (range get), `is_PixelClock` (range get) |
| 4 | one frame | `fc2StartCapture`, one `fc2RetrieveBuffer` (timeout 5 s), `fc2StopCapture` | `is_AllocImageMem`, `is_SetImageMem`, one `is_FreezeVideo(IS_WAIT)`, `is_FreeImageMem` |
| 5 | close | `fc2Disconnect` | `is_ExitCamera` |

- Step 4 uses the camera's **current** mode, exposure and gain. Nothing is set except the minimum the SDK needs to deliver a buffer (uEye: image memory and colour mode to the read default).
- **Never called** (refused by construction; the backend has no method for them): firmware update, `fc2SaveToMemoryChannel`, `fc2RestoreFromMemoryChannel`, register writes (`fc2WriteRegister*`), `is_SaveParameters`/EEPROM writes, `is_SetCameraID`, `is_Configuration`, any device-reset call, any LED or stage I/O.
- One attempt. A failure at any step stops the run (close is still called) and is not retried.

### 4a. Mono adapter as built (2026-11-13)

`src/openevos/maintenance/flycapture2_gate.py`, ctypes against `FlyCapture2_C_v140.dll` 2.13.3.61. Struct layouts come from the installed `FlyCapture2Defs_C.h`. Every output struct carries an 8 KB trailing pad, so a layout mismatch can't overrun memory.
- **Bound functions (and only these):**
  - contexts: `fc2CreateContext`, `fc2DestroyContext`;
  - finding and connecting: `fc2GetNumOfCameras`, `fc2GetCameraFromIndex`, `fc2Connect`, `fc2Disconnect`;
  - reads: `fc2GetCameraInfo`, `fc2GetLibraryVersion`, `fc2GetVideoModeAndFrameRate`, `fc2GetFormat7Configuration`, `fc2GetFormat7Info`, `fc2GetPropertyInfo`, `fc2GetProperty`;
  - one frame: `fc2StartCapture`, `fc2RetrieveBuffer`, `fc2StopCapture`, `fc2CreateImage`, `fc2DestroyImage`;
  - error text: `fc2ErrorToDescription`.

  Any other name is refused before the DLL is touched.
- **Dry run:** loads the DLL and calls `fc2GetNumOfCameras` on a temporary context, which is a bus enumeration with no connect. It therefore talks to the driver, and it is run only with approval too.
- **Timeout:** `fc2RetrieveBuffer` runs on a worker thread. If no frame comes within 5 s, `fc2StopCapture` aborts it. The grab timeout is not set through `fc2SetConfiguration`, so no Set call is ever made.
- **Pixels:** the frame is copied out using rows, cols and stride. MONO8/RAW8 give uint8 and MONO16/RAW16 give uint16. MONO12 (packed) is refused.
- **Tests:** a fake DLL in `tests/test_maintenance_flycapture2_gate.py` (7 tests).

## 4b. First run, mono, 2026-11-13 18:04 UTC (operator-approved; LEDs off, room light low)

The dry run passed (one camera seen). The gate run completed every step (`connect, identity, modes, grab_one, close`). It was marked **stopped** only by the shape check (§6): the camera powers up in a standard IIDC mode, not Format7, so the frame is 1280×960 rather than the full sensor. The camera behaved normally; the check was too strict. Record: `openevos-scans/camera-gate1-mono-20261113T180418-stopped.json` plus `-frame.npy`.

| Item | Value | Label |
|---|---|---|
| Model / vendor | Chameleon **CMLN-13S2M**, Point Grey Research | **Verified** (SDK) |
| Sensor | Sony ICX445AL, 1/3″, 1296×964 CCD, mono | **Verified** |
| Firmware | **1.17.3.0**, built 12 Jul 2013. This is the newer of the two vendor package images; not changed by us | **Verified** |
| Driver / library | PGRUsbCam.sys 2.7.3.235; FlyCapture 2.13.3.61 | **Verified** |
| Serial | in the git-ignored record (matches the USB iSerial) | **Verified** |
| Power-up mode | video mode 17 = **1280×960 Y8**, 15 fps (not Format7) | **Verified** |
| Format7 modes | 0: 1296×964; 1: 648×482 (2×2 binning, Probable); 2: 1296×482. All MONO8 / MONO16 | **Verified** |
| Shutter | 0.005–66.6 ms (raw 0–1077); at power-up **auto**, 12.0 ms | **Verified** |
| Gain | −5.6 to 24.0 dB (raw 160–842); at power-up **auto**, 0 dB | **Verified** |
| Brightness (black level) | 0–6.24 % (raw 0–1023); at power-up **0** | **Verified** |
| Auto exposure, frame rate | auto on; frame rate 0.33–16.3 fps, auto, 15 fps | **Verified** |
| Temperature register | raw 3164. If in 0.1 K units, ≈ 43 °C | Probable |
| Frame | 960×1280 uint8: min 0, max 3, mean 0.014, 0 saturated | **Verified** dark |

What this means:
- **Pass on substance:** identity, modes and a dark frame were all read. Nothing was written to the camera.
- **No bias level yet:** with brightness 0 and 8-bit output, the dark frame clips at 0, so read noise and bias can't be measured from it. M9 dark-frame work needs MONO16 Format7 mode 0 with a set black level, i.e. a reviewed **Gate 2** with RAM-only settings writes, never memory channels.
- **Auto exposure and gain are on at power-up.** The app backend must switch them to manual for scientific frames (also Gate 2).
- **Tool fix, applied after the run:** the shape check now accepts either the full sensor or the 1280×960 IIDC mode. That change hasn't been re-run on the instrument.

## 4c. Second run, mono, 2026-11-13 18:14 UTC (operator: room made pitch dark, LEDs off)

Run with the fixed shape check; every step completed. Record: `camera-gate1-mono-20261113T181434-complete.json`.
- **Auto exposure ramped to its limits in the dark:** shutter 66.6 ms (raw 1077, the maximum) and gain **24.0 dB** (raw 842, the maximum). In the first run, with low room light, they were 12 ms and 0 dB.
- **Frame:** 960×1280 uint8, mean 3.6, median 2, 99.9 % percentile 22. **2 saturated pixels**, adjacent at (row 86, columns 1179–1180): a hot-pixel pair, **Probable**, at maximum gain and exposure.
- The tool reported "not dark … not a pass" because of the 2 saturated pixels. **Interpretation:** this is a darker scene, amplified by auto exposure, not light leaking in. Under auto settings, Gate 1 cannot separate the two cases. That is why Gate 2 uses manual settings. Hot pixel (86, 1179–1180) is a candidate for the M9 defect map.
- The other power-up flags and values are unchanged from run 1 (mode 17, rate 3, brightness 0, gamma 1024 off). Sensor temperature register: 3231 (≈ 50 °C if in 0.1 K units, Probable). It was 3164 in run 1.

## 4d. Third run, mono, 2026-11-15 11:15 UTC: restore check after an app session (operator-approved)

Purpose: to confirm the camera was back in its power-up state after the first `openevos-ui` sessions with the real backend (ADR-0037: Format7 MONO16, manual shutter and gain, then a live view and a snap, then a restore on exit). All five steps completed. Record: `camera-gate1-mono-20261115T111559-complete.json`.
- **Restored:**
  - video mode 17 (1280×960 Y8), frame rate 3 (15 fps);
  - auto exposure, shutter, gain and frame rate all auto/on;
  - brightness 0, gamma 1024 off.

  This matches runs 1 and 2. Nothing from the app session remained (no Format7, manual or 2 % brightness setting).
- **Same as run 2:**
  - in the dark, auto exposure ramped to its limits (66.6 ms, 24.0 dB);
  - frame 960×1280 uint8, mean 3.2, median 2, 99.9 % percentile 19;
  - **2 saturated pixels**, which the tool reports as "not dark … not a pass". As in §4c, this is the known hot-pixel pair at maximum auto gain, not light.
- Sensor temperature register: 3206.

## 4e. Colour adapter as built (2026-11-15): `backends/ueye/sdk.py`, `UeyeGateBackend`

- **Role:** `colour` = `1409:1465`. PnP shows `1409:1000` as the camera's "uEye boot" state, before the driver loads its RAM image. Phrase: `READ CAMERA COLOUR 1409:1465 WITH LED OFF`.
- **API:** classic uEye C API, `C:\Windows\System32\ueye_api_64.dll` 4.97.4111 (installed by IDS peak 26.06.1 Extended), called through ctypes.
  - IDS peak installs no `uEye.h`. Constants and struct layouts come from IDS's own generated binding `pyueye/ueye.py` (github.com/jkokorian/ids-pyueye): **Probable**, cross-checked only by the fake-DLL tests.
  - Output structs carry an 8 KB trailing pad.
- **Allow-list, with an argument guard per function:**
  - `is_GetDLLVersion`, `is_GetNumberOfCameras`, `is_GetCameraList`, `is_GetCameraInfo`, `is_GetSensorInfo`, `is_GetError`.
  - `is_InitCamera`: only with handle value 0 and HWND NULL. Never with `IS_ALLOW_STARTER_FW_UPLOAD` (0x10000) or `IS_USE_DEVICE_ID`. Error 184 (`IS_STARTER_FW_UPLOAD_NEEDED`) stops the gate, and the camera is not opened.
  - Calls whose names start with "set", accepted only with their GET query constant:
    - `is_SetColorMode` (0x8000);
    - `is_SetHardwareGain` (0x8000–0x8003, others −1);
    - `is_SetAutoParameter` (0x8801/03/05/07);
    - `is_SetFrameRate` (0x8000, passed as a double).
  - Accepted only with GET commands:
    - `is_Exposure`: 2, 6, 7 (SET = 12 is refused);
    - `is_PixelClock`: 3, 4, 5 (SET = 6 is refused);
    - `is_AOI`: GET_AOI only.
  - Host-RAM frame buffer: `is_AllocImageMem`, `is_SetImageMem`, `is_GetImageMemPitch`, `is_FreeImageMem`. `is_FreezeVideo` takes only a timeout in 10 ms units. `is_ExitCamera` is always called.
  - Nothing that saves parameters, writes EEPROM, uploads firmware, sets LUTs or I/O, or triggers is bound.
- **connect:** list the cameras first, and refuse if any reports "in use". Then init in DIB mode (no window).
- **Frame:** taken in whatever colour mode is current after init. 8-bit packed BGR(A) becomes (H, W, 3) RGB uint8. MONO or RAW 8/10/12/16 become (H, W). Any other mode stops before the frame is taken. Shape comes from the SDK (no `expected_shapes`). The UI-1460LE should be 2048×1536 (**Probable**).
- **Tests:** `tests/test_backend_ueye_gate.py` (8, fake DLL).
- **Dry run 2026-11-15 14:24 UTC:** PnP finds one `1409:1465` with problem code 0, the DLL loads, and `is_GetNumberOfCameras` = 1. Nothing was opened. Record: `camera-gate1-colour-20261115T142443-dry_run.json`.

## 4f. First run, colour, 2026-11-15 14:29 UTC (operator-approved; LEDs off)

**PASS**: all five steps completed (connect, identity, modes, grab_one, close). Record: `camera-gate1-colour-20261115T142903-complete.json`, frame in `…-frame.npy`. The serial is kept in the scan only.

**Identity:**
- Model **UI146xLE-C** (CAMINFO type 66, ID "IDS GmbH", version V1.0, date 18.03.2014), sensor ID 45.
- Bayer colour sensor (colour mode 2): 2048×1536, 3.2 µm pixels, rolling shutter.
- Master and R/G/B gains are available. Library 4.97.4111.
- The model is now **Verified** by the SDK, and the colour sensor is confirmed.

**State after `is_InitCamera` (driver defaults, nothing set by the gate):**
- BGR8_PACKED, full sensor area 2048×1536.
- Pixel clock 24 MHz (default 24, range 5–43), 5.0 fps.
- Exposure 199.7 ms, which is the maximum at this pixel clock (range 0.10–199.7 ms in steps of 0.10).
- Master gain 0, R/G/B gain 5/0/3.
- All auto functions off.
- `GET_EXPOSURE_DEFAULT` returned 0.0, which is unexplained and does not affect the result.

**Frame:**
- 1536×2048×3 RGB uint8.
- Mean 9×10⁻⁶; only **52 of 9.4 M samples are non-zero**, the largest being 6. Nothing is saturated.
- The camera sends black-level-corrected 8-bit data, so a dark sensor reads 0. The few sparse non-zero samples look like hot pixels at 200 ms exposure, which is evidence that this is real sensor data rather than an untouched (all-zero) buffer: **Probable**.
- A frame with some light would confirm the image path (Gate 2 or the app backend).

## 5. Evidence

JSON in git-ignored `openevos-scans/` (`camera-gate1-<role>-<UTC>-<outcome>.json`): preconditions, identity, modes, frame shape/dtype/bit depth, min/max/mean/std and 0.1/50/99.9 % percentiles, saturated-pixel count, plus the frame itself as `.npy`. Serial numbers stay in this git-ignored record.

## 6. Pass / stop

- **Pass:** identity read; modes read; one frame of the expected size (mono 1296×964; colour per the SDK), with no pixel saturated and mean within the lower quarter of range (dark frame).
- **Stop:** any precondition failing, any SDK error, a timeout, or a frame of unexpected size. A bright frame (mean > 25 % of range) is recorded as "LED or room light not off" and the result is not a pass.

## 7. Open points before the first run

- The SDK install itself (file, source, size) needs the user's approval. It must be installed **without** its firmware-updater component and with automatic update checks declined.
- The real SDK adapters (ctypes against `FlyCapture2_C.dll` and `ueye_api_64.dll`) are written against the installed headers, then reviewed, before the first run.
