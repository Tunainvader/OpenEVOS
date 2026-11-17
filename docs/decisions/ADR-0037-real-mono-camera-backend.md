# ADR-0037: Real mono camera backend in the app (Chameleon via FlyCapture2)

- **Status:** Accepted (2026-11-15). Tested against a fake DLL (12 tests, including a full session with live view and a snap taken while live). **On the instrument (2026-11-15):** live view showed "TRANS • LIVE • CAMERA", and a snap showed "TRANS • CAPTURED • CAMERA". The LED was off and the light simulated.
- **Builds on:** ADR-0033 (Gate 1, identity), ADR-0036 (Gate 2, settings path, passed 2026-11-13).

## Context

- The Capture page showed frames from the rig simulator's cameras.
- Gate 2 proved the settings path on the real Chameleon:
  - Format7 1296×964 MONO16 at a 75 % packet size;
  - manual shutter and gain, brightness 2 %, all read back;
  - a verified restore.

  It also showed that 16-bit data arrive big-endian, as 12-bit values left-aligned.

## Decision

1. **`backends/flycapture2/camera.py`, `FlyCapture2MonoCamera`.** It has the same backend contract as the rig's simulated camera: DeviceBackend, `camera.set_exposure` / `camera.set_gain`, and `start_stream` / `stop_stream` into the FrameTransport. `backend_kind = PHYSICAL`, `synthetic=False` on every frame. After the SDK has identified the camera, it reports `firmware_mode=APPLICATION`, which ADR-0009 requires before the router accepts any command.
2. **The ctypes adapter lives in `backends/flycapture2/sdk.py`.** It is shared with the Gate 1 and Gate 2 tools, which import it via `maintenance/flycapture2_gate.py` and `camera_gate2_mono.py`. Under ADR-0011 the app must not import `openevos.maintenance`, so the adapter can't live there.
3. **Camera envelope = Gate 2's:**
   - on connect: snapshot the power-up state, then apply the operating settings, each read back;
   - commands: manual shutter and gain only, within the ranges the camera reports (exposure 0.01–66.6 ms at the power-up frame rate, gain 0–24 dB);
   - commands are accepted while live view runs: FlyCapture2 allows property writes during capture, and the Capture page sets the channel's exposure before `acquire()` pauses and restarts the stream;
   - on disconnect / app exit: restore the snapshot, verify, and report any difference with "power-cycle the instrument";
   - no memory-channel, register, trigger or firmware call is bound.
4. **Frames:**
   - uint16 with the 4 padding bits shifted out (0–4095), matching the profile's 12-bit depth;
   - incomplete frames (error 41) are skipped and counted, at most 5 in a row, after which the stream faults;
   - metadata records gain, model, the bit shift, the skip count, and `light_simulated=True`.
5. **Selection:** `openevos-ui --mono-camera {auto,flycapture2,sim}`, default `auto`.
   - `auto` runs a read-only probe first, without opening any device. It uses the real camera when the FlyCapture2 DLL exists and exactly one `1E10:2005` has a working driver; otherwise it uses the simulator, and the choice is printed to stderr.
   - Screenshot mode always uses the simulator.
   - In code: `build_sim_session(cameras={"mono": "flycapture2"})` → `attach_optical_simulator(camera_factories=…)`. Any other role or backend is refused before any device is touched.
   - Stage, focus, turret, light and the colour camera stay as they are: simulated, or the stage's own opt-in.
6. **The Capture badge** already follows `synthetic`, so real frames show **CAMERA** instead of SIMULATED, with no change to `capture.py`.

## Consequences and limits

- **The light is still simulated.** The real camera images whatever the real LED board is doing: off, unless LED Gate 4 work turns it on. The UI's light controls don't drive the real LEDs.
- Exposures longer than about 66 ms need the frame rate switched to manual. That is not in this envelope. The router rejects such requests, fail closed; for example, the hot-pixel calibration's 500 ms request.
- The Instrument page's "simulate disconnect" button does nothing to the real camera.
- The restore result is reported to the device registry but not printed to stderr. A read-only Gate 1 run after an app session confirms the power-up state: video mode 17, 15 fps, auto shutter and gain.
- When a capture fails, the Capture page shows the error on its status line only, not in the log file (a UI follow-up).
- Next steps:
  - ~~Gate 1 check of the restore after an app session~~: done 2026-11-15, power-up state confirmed (camera_gate1_spec.md §4d);
  - M9 photon transfer, once LED control is in the app;
  - colour camera: IDS SDK and Gates 1/2 for `1409:1000`.

## Lag check on the instrument (2026-11-16, approved)

- **Setup:**
  - Gate 12 `--to slot1` (pass, F at cube 1 / RFP, APOS 0).
  - App closed. One LED Gate 4 brightfield hold, confirmed off by read-back.
  - App-style snaps: a fresh `fc2StartCapture` and one `RetrieveBuffer` each, as `controller.acquire` does.
  - Exposure set between snaps (not capturing), as the controller does with live view paused.
  - Record: `openevos-scans/mono-lagcheck-20261116T002650.json`.
- **Exposure: no lag.**
  - Series 5, 40, 2, 20, 10, 40, 5 ms gave means 1345, sat, 594, sat, 2584, sat, 1345.
  - (mean − dark 82) / ms ≈ 256, 253, 250: linear, and the repeated 5 ms snaps match.
  - Steps out of saturation (40 → 2, 20 → 10, 40 → 5) gave correct, unsaturated values. A lagged snap would have been saturated.
  - So no exposure drop is added. The colour camera's exposure lag (ADR-0039) does not apply here.
- **Light change: lag.**
  - Exposure 20 ms throughout.
  - **First snap after the LED's ON reply: dark** (82.6, the dark level). It was requested at the reply and delivered 204 ms later. The next snap was saturated.
  - **First snap after OFF: part-lit** (mean 2367); the next was dark.
  - So the frame retrieved after a fresh capture start was exposed before the switch.
- **Fix:**
  - `FlyCapture2MonoCamera.invalidate_frames()` drops the next retrieved frame (`STALE_FRAMES_AFTER_LIGHT = 1`, counted in `stale_dropped`).
  - `InstrumentController` already calls it after every real light change (ADR-0039), so a snap after a light change gets a frame exposed after the change.
  - Tests (fake DLL): `LightLaggingChameleon` reproduces the dark first snap without the drop and shows it fixed with it. The session test checks that the controller triggers the drop.
  - **Exercised once on the instrument (2026-11-16, approved):** F at slot 1, one brightfield hold of 1 s at 20 ms, with `invalidate_frames()` called at the ON reply and after OFF, as the controller does.
    - First snap after ON: **saturated (lit)**. Before the fix it was dark.
    - First snap after OFF: **85.0, dark**. Before the fix it was part-lit at 2367.
    - `stale_dropped` = 2. LED confirmed off; camera restore verified with no diffs.
    - Record: `openevos-scans/mono-lagfix-20261116T022530.json`.
    - The lit level saturates at 20 ms, so this shows lit vs dark, not a partial frame hidden above saturation.
    - The controller's own trigger was not exercised on the instrument, because the app's light is still simulated.
- **Not changed:** the frame's `exposure_ms` is read at delivery, and shutter writes are allowed while capturing. A UI exposure change during live view could therefore stamp one frame in flight with the new value. Snaps are unaffected, because the controller pauses live view for camera settings.

