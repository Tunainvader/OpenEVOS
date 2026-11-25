# ADR-0039: Real colour camera backend in the app (uEye UI-146xLE-C via the uEye API)

- **Status:** Accepted (2026-11-21). Proposed 2026-11-15 and tested against a fake DLL (16 tests, including a full session that captures with live view running). **On the instrument:**
  - **The image path is proven,** but only with the cube changer at the mirror (F = 128,000, Motor Gate 12 / ADR-0041). At the cube slots the camera sees nothing; see "On the instrument".
  - **The live view works in the app** (2026-11-15, flashlight).
  - **A brightfield exposure sweep is linear:** 5.82 DN/ms in red at the LED's default level, so 20–30 ms is usable.
  - **The exposure-lag fix is confirmed** (2026-11-16); see "Exposure lag".
  - **Open:**
    - the right ~20–25 % of the field is vignetted (mirror centring; motor scope);
    - the colour order is not yet checked against a coloured target;
    - a possible one-frame lag after an LED change;
    - gain is fixed at 1x.
- **Builds on:** ADR-0033 (Gate 1, colour PASS), ADR-0038 (Gate 2, colour PASS 2026-11-15), ADR-0037 (mono backend pattern).

## Context

- Colour Gate 2 proved four things on the UI146xLE-C:
  - manual exposure (0.10–199.7 ms at the default 24 MHz pixel clock);
  - master gain 0;
  - a black-level offset;
  - that exit plus re-init returns the driver defaults.
- It also showed that the only raw format is RAW8, and that the Bayer order is unknown until a lit frame confirms it.

## Decision

1. **`backends/ueye/camera.py`, `UeyeColourCamera`.** It has the same contract as the simulated camera and the mono backend. Frames are (H, W, 3) RGB uint8 with `synthetic=False`. It reports `firmware_mode=APPLICATION` after identification (ADR-0009).
2. **Colour pipeline: the driver default BGR8_PACKED**, with the IDS library doing the demosaic and colour conversion. The colour mode is **never set**: no Bayer-order guess is needed, and one less camera write. RAW8 capture for scientific use can follow once the Bayer order is confirmed.
3. **Envelope** (a subclass of the Gate 1 adapter, `UeyeAppSdk`, guarded):
   - connect refuses unless the model is UI146xLE and the camera is in its defaults (BGR8, 2048×1536);
   - the only setters are master gain **0** (R/G/B untouched) and exposure **within the range read from the camera**;
   - **gain is fixed at 1x** (the only gain Gate 2 proved): any other gain is refused (`UNSUPPORTED`);
   - pixel clock, frame rate, area, black level and colour mode stay at their defaults;
   - disconnect is `is_ExitCamera`, which drops the session settings (verified in Gate 2).
4. **Frames:** `is_FreezeVideo` per frame into one host buffer. Live runs at the camera's frame rate (about 5 fps by default; set by exposure and transfer over USB 2). Timeouts and transfer errors are skipped, up to 5 in a row. Exposure changes are accepted while live; each waits for the frame in flight. **After every exposure change the next frame is dropped** (see "Exposure lag"), and each frame is stamped with the exposure read under the same lock as its freeze.
5. **Selection:** `openevos-ui --colour-camera {auto,ueye,sim}` (alias `--color-camera`), default `auto`.
   - `auto` runs a read-only probe first: the uEye DLL is present and exactly one `1409:1465` has a working driver. Otherwise the simulator is used.
   - Screenshot mode always uses the simulator.
   - In code: `build_sim_session(cameras={"color": "ueye"})`.

## Consequences and limits

- **A snap after an exposure change takes two frame times** (one dropped frame, about 0.2 s at 5 fps). Live view loses one frame per change.
- **The light is still simulated.** The real camera images whatever the real illumination is doing.
- Gain above 1x needs a gain-factor gate (`is_SetHWGainFactor`, not yet proven).
- Black level stays auto with offset 0, so very dark areas read 0. That is fine for brightfield and phase imaging; the offset path is proven if it is needed later.
- Live frames are full resolution (2048×1536×3, 9.4 MB). Display throughput over USB 2 is **Probable** a few fps.
- First approved session: live view with some light, then a snap. That confirms the image path and colour order (Gate 1 and 2 frames were dark).

## On the instrument (2026-11-15)

- **First app session (approved):** the user saw no image. A headless diagnostic with the same calls (approved) showed the backend works: connected, live at about 4.7 fps, snap OK, exposure read back. Every pixel was 0 at 5 ms.
- **Exposure sweep (approved), dark lid above the incubator lid removed, room light only:**
  - 5 ms (read back 4.98): mean R/G/B 0.00, max 7;
  - 50 ms (read back 50.00): max 0;
  - 199 ms (read back 198.98): max 1.
  - Frames are in `openevos-scans/colour-sweep-20261115T1759*`.
- **So no measurable light reaches the colour sensor.** The software path is not the problem: exposure is set and read back, and the sensor's dark noise is written into the buffer, as in Gate 1.
- **Probable causes:**
  1. The epi carriage F is at slot 1 (F = 0, the RFP cube; Motor Gate 11 spec) and not at the mirror (`LMIRROR1POS` 128,000). The project's own channel model puts brightfield at the mirror. Whether the mirror also selects the colour camera's port is **Unknown**.
  2. No transmitted LED: the LED board is in bootloader mode (`0DAD:0057`), so only room light reaches the sample. With black level auto at offset 0, a very weak signal also reads 0.
- **Flashlight check (approved, 18:02 UTC):** 94 snaps over 30 s at 199 ms while the user shone a phone light onto the stage. The brightest frame: mean R/G/B 0.00, max 9 (`colour-sweep-20261115T180249-record-199ms`). Even direct bright light on the stage gives nothing, so **the colour camera is Probably not in the current light path** (F at the RFP cube). The mirror position, or a port shutter or cover on the colour camera, is now the prime suspect, rather than too little light.
- **Next:** an image with real light: the transmitted LED on, with the carriage at the mirror. Both are other sessions' scope (LED Gate 4; an F-to-mirror motor gate). This ADR stays Proposed until a lit frame confirms the image path and colour order.
- **Done (2026-11-15, `fl1_v1.6_motor_gate12_f_mirror_spec.md`):** with F at 128,000 (Motor Gate 12, ADR-0041), the flashlight saturates the colour camera and the brightfield LED gives a linear exposure series. A usable exposure is about 20–30 ms at the board's default LED level. The colours are warm (R > G > B; white balance untuned). **The image path through the mirror is Strongly supported.** Open: the right ~20–25 % of the field is cut off by an arc (Probable cause: the mirror is off-centre at 128,000; motor scope), and a lit colour target is still needed to confirm the colour order.

## Exposure lag (found 2026-11-15, fixed 2026-11-16)

- **Symptom (instrument, steady transmitted LED, F at the mirror):** one snap straight after each exposure change returned a frame exposed at the *previous* exposure. The 0.5 ms snap was saturated: it was the 199.7 ms power-up exposure. 5 ms gave the 2 ms value, 10 ms gave the 5 ms value, and so on (`openevos-scans/colour-bf-20261115T190233-summary.json`). Discarding one snap per exposure gave a linear series (`colour-bf-20261115T190306-summary.json`).
- **Cause: freerun pipelining, not the host buffer.**
  - The app uses one image buffer (no ring) and `is_FreezeVideo` per frame (no `is_CaptureVideo`), so no old frame can sit in host memory: each freeze overwrites the buffer.
  - After `is_InitCamera` the camera is in freerun (trigger off), so the sensor keeps exposing. `is_FreezeVideo` returns the next frame to *finish*, and that frame can have started exposing before `is_Exposure` was set.
  - The lag is timing-dependent, not fixed: in the lagged sweep the 1 → 2 ms snap was already correct. Every other step lagged by exactly one frame.
- **Fix: discard.** The backend drops the first frame after every exposure change (`STALE_FRAMES_AFTER_EXPOSURE = 1`, counted in `stale_dropped`), for live view and for snaps. A snap goes through `start_stream(n_frames=1)`, so it is always a fresh frame. One frame is enough: the second freeze's frame started exposing after the set. The discard-one series is the instrument evidence for that.
- **Why not a uEye mechanism:**
  - The classic API in this envelope has no "wait until the new exposure" or flush call.
  - Software-trigger mode (`is_SetExternalTrigger(IS_SET_TRIGGER_SOFTWARE)`) would start each exposure at the freeze and should remove the lag. But it is a mode change outside the proven envelope, it changes how the rolling-shutter sensor runs, and it would need its own gate. The IDS manual pages could not be fetched (HTTP 403) to confirm the latency rules. So the allow-list is unchanged: no new SDK call.
- **Metadata race fixed in the same change:** an exposure command landing between a freeze and the frame's delivery used to stamp the frame with the new exposure. The exposure is now read under the SDK lock, together with the freeze.
- **Tests (fake DLL):** `LaggingColour` models the pipelining (each freeze returns the frame at the exposure that was in force at the previous freeze). With the drop disabled it reproduces the instrument's lag, power-up 199.7 ms first included. With the drop on, every snap has its own exposure, and so does its metadata. A third test covers the stamping race.
- **Open:**
  1. ~~Not yet re-run on the instrument with the fixed backend.~~ **Confirmed on the instrument 2026-11-16 (approved):**
     - Setup: brightfield LED (LED Gate 4), F at the mirror. One snap per exposure through the app's capture path, with no extra discard. The order jumped up and down: 5, 30, 2, 20, 10, 40, 5 ms.
     - Mean red values: 28.4 / 178.0 / 10.5 / 117.9 / 58.1 / 229.5 / 28.4.
     - Red fits 5.82 DN/ms − 0.2 with a maximum residual of 3.5 DN.
     - The two 5 ms snaps match exactly (28.4 / 23.1 / 19.3). `stale_dropped` = 8 (connect plus 7 changes).
     - No lagged frame. Record: `openevos-scans/colour-lagcheck-*.json`.
     - **Second check, 2026-11-15 19:25 UTC (approved, separate LED Gate 4 hold of 120 s):** the original rising sweep, now with one snap per exposure (0.5 … 50 ms). Mean red values were 1.70 / 4.57 / 10.48 / 28.47 / 58.20 / 118.03 / 240.42. That is within 0.3 % of the hand-discard reference (`colour-bf-20261115T190306`). The 0.5 ms snap is no longer saturated. `stale_dropped` = 7. Record: `openevos-scans/colour-bf-lagfix-20261115T192536-summary.json`.
  2. **LED-change lag: confirmed on the instrument 2026-11-15 19:29 UTC (approved).**
     - Setup: app closed, F at 128,000, exposure fixed at 20 ms (so no exposure drop). Brightfield was switched by the reviewed LED Gate 4 tool, twice for 3 s; both runs confirmed off by read-back.
     - **App-style snaps** (a fresh 1-frame stream each, like `controller.acquire`):
       - dark 0.0;
       - first snap straight after the ON reply: **72.1** R mean, **part-lit (61 %)**;
       - next two snaps 118.1 and 118.1;
       - snaps straight after OFF: 0.0 (this time).
     - **Timeline** (live stream, a frame about every 0.4 s):
       - the frame delivered 63 ms after the ON reply was still dark, and the next one was fully lit (118.2);
       - after OFF, one frame was part-lit (41.9), then 0.0;
       - no slow ramp: the first fully lit frame equals the steady level.
     - **So a snap taken right after a light change can return a frame exposed partly before the change.** As with exposure, the frame in flight started before the switch. A part-lit (not dark) frame Probably means the rolling shutter: rows are exposed at staggered times, so the switch fell part-way down the frame (no pixels were saved to confirm the row gradient). Whether it happens depends on timing: the ON snap was hit, the OFF snap was not.
     - **Fixed (2026-11-16), ready for the real LED.** The app's light is still simulated, so this guards the future wiring:
       - `UeyeColourCamera.invalidate_frames()` drops the next frame (`STALE_FRAMES_AFTER_LIGHT = 1`, the same counter as exposure).
       - `InstrumentController` calls it on every camera backend that has it when a light command **succeeds and the light the sample gets has changed**. That covers on ↔ off, intensity, and channel while lit. Re-sending the same light, or a change while dark (off, or on at 0 %), drops nothing.
       - The call runs in the command's synchronous lifecycle handler, before the light command's `wait()` returns, so `capture()` (light, then exposure, then snap) never gets the stale frame. In live view, the frame already in progress still reaches the preview.
       - Tests (fake DLL): `LightLagging` reproduces the dark first snap without the drop and shows it fixed with it. A session-level test checks which light changes drop a frame.
       - **Exercised once on the instrument (approved).** Sequence: after Gate 15's power cycle, LED Gate 1 and StartApp, then Gate 12 `--to mirror` (pass, APOS 128,000). Then one 3 s brightfield hold at 20 ms, with `invalidate_frames()` called at the ON reply and after OFF, as the controller does.
         - First snap after ON: **117.7** R mean (steady 117.6). Before the fix it was 72.1 (61 % lit).
         - Snaps after OFF: 0.0.
         - `stale_dropped` = 3 (exposure set, ON, OFF).
         - LED confirmed off by read-back.
         - Record: `openevos-scans/colour-ledlag-fixed-20261116T002211.json`.
         - The controller's own trigger was not exercised on the instrument, because the app's light is still simulated.
       - The mono backend (Chameleon) has the same light-change lag (but no exposure lag) and now the same drop: ADR-0037, "Lag check on the instrument".
     - Record: `openevos-scans/colour-ledlag-20261115T192949.json`; LED evidence `led-application-gate4-light-20261115T192942-complete.json` and `…192947-complete.json`.
