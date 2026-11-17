# ADR-0048: Real LED illumination in the app (on/off only)

- **Status:** Accepted for implementation: built and tested against a fake LED board and in the simulator session (2026-11-16). Not yet run with the real board from the app; the first run needs the operator's approval in chat.
- **Basis:** LED Gate 3 (ADR-0031) and LED Gate 4 (ADR-0034), spec [`fl1_v1.6_led_app_command_table.md`](../evidence/fl1_v1.6_led_app_command_table.md) §4–§6b. Gate 4 was verified on the instrument at all four cube slots.

## Context

- The operator wants the UI's light buttons to switch the real lights. Until now the app had only the simulated illumination.
- The real board was reachable only through the LED Gate 4 maintenance tool.

## Decision

1. **New backend** `openevos.backends.ledboard`, selected with `openevos-ui --illumination {auto,real,sim}`.
   - `auto` (default) uses the real board when exactly one application interface `0DAD:0051` and no bootloader interface are present (SetupAPI enumeration only), else the simulator.
   - Screenshots always use the simulator.
2. **Commands the app may send** (`REVIEWED_APP_WRITES["led_application.app_illumination"]`):

   | Command | Values | Purpose |
   |---|---|---|
   | `0x43` | 0/1 | brightfield enable |
   | `0x49` | 0/1 | epi enable |
   | `0x45`, `0x4A`, `0x47` | — | reads |

   Nothing else: no intensity, PWM or current mode (M8, unreviewed), no StartApp, no persistent or firmware command.
   - The frames are built in `backends/ledboard/protocol.py`.
   - A test pins them byte-identical to the reviewed tools' frames.
   - The app keeps its own protocol and HID transport (`winhid.py`) because the app never imports maintenance tools (ADR-0011).
3. **Channel mapping:**
   - `trans` and `phase` → brightfield.
   - Fluorescence channels → epi, but only if the board's cube sensor (`0x47` byte 2) reports the matching cube type in the light path. Otherwise the command fails (`CUBE_NOT_IN_PATH`) and all lights are left off.
   - The F carriage is not moved by this backend.
   - Switching channel switches the other light off.
4. **Brightness:** the commanded percentage is recorded but not applied; the board's power-on default level is used. The device reports `intensity_controlled: False`.
5. **Fail safe:**
   - Lights OFF, each confirmed by read-back, at connect, on STOP (`halt`, waits ≤2.5 s), on disconnect / app exit, on any I/O failure and on any wrong read-back.
   - An unconfirmed OFF raises a FAULT telling the operator to power-cycle.
   - Epi switches itself off after 300 s (possibly DAPI, near-UV).
6. **Bootloader:**
   - A board in its bootloader is reported as BOOTLOADER. The safety layer quarantines it (ADR-0009).
   - The app never sends StartApp. The operator starts the board with the reviewed tools (`led_bootloader`, then `led_startapp`) before launching the app with real illumination.
7. **Execution:**
   - The device is PHYSICAL with VERIFIED capabilities citing the Gate 4 runs.
   - Every command goes through the router and the command validator.
   - I/O runs on one worker thread, one exchange at a time, never resent.

## Amendment (2026-11-16): brightfield slider

After LED Gate 5 (ADR-0049) verified the brightfield level on the instrument (level 10 visibly brighter than 3):

- The brightness slider for `trans` / `phase` sets the brightfield level: 0–100 % → level 0–20 of 31. Any non-zero % gives at least 1.
- The level is set through `0x5E` with field mask `0x04` only and read back through `0x5D`. A mismatch fails safe.
- Moving the slider while the light is on changes only the level.
- The level found at connect (the power-on default, 3 on this unit) is restored at disconnect / app exit.
- The cap of 20 stays until power-meter data exists (M8).
- **Epi slider** (added after the epi Gate 5 run on RFP, spec §7.2b):
  - 0–100 % → epi level 0–10 of 31 through `0x4C`, read back via `0x4A` byte 4. The cap of 10 matches the firmware's own cube cap and keeps DAPI (near-UV) modest.
  - The power-on epi level (1 on this unit) is restored at exit.
  - A read-back mismatch, e.g. the firmware capping a cube, fails safe.
- Registry: `REVIEWED_APP_WRITES` now also lists `0x4C`, `0x5D` and `0x5E`.
- **Calibrated brightfield slider** (2026-11-17), from the camera sweep (ADR-0050, spec §8.2):
  - `BF_MEASURED_OUTPUT` holds the measured relative output at levels 1–20, interpolated between swept levels.
  - The slider's % picks the level whose measured output is closest to that % of level 20's output, so the light changes in proportion to the slider.
  - Because the curve is nearly linear, this differs from the straight mapping by at most one level, mostly at the ends (level 1 is about 6 % weak, level 20 droops about 7 %).
  - The calibration is relative and was measured through the RFP cube at slot 1. Absolute power still needs a power meter (M8).
  - The epi slider stays linear until an epi curve with a fluorescent sample exists.

## Consequences

- The UI's light buttons switch the real brightfield LED and the epi LED of the cube in the light path.
- Fluorescence channels whose cube is not in the path are refused with a clear message, until F-carriage control is wired into the app.
- Brightness sliders have no physical effect yet. M8 still needs a reviewed intensity command.
