# ADR-0034: LED Gate 4, brightfield and epi LED on/off test

- **Status:** Accepted. Run once, 2026-11-13 17:35 UTC: both lights switched on and confirmed off. The observer saw bright white (brightfield), then green (RFP excitation). The board runs in system mode PEARL (spec §6a).
- **Spec:** [`fl1_v1.6_led_app_command_table.md` §6](../evidence/fl1_v1.6_led_app_command_table.md)

## Context

- The operator asked to test the lights: brightfield and the light cubes.
- Gate 3 (ADR-0031) showed system/epi mode 1 (moveable carriage), RFP in the light path, epi intensity setting 1 of 0–31 in DIGIPOT current mode.

## Decision

1. **The first lighting write is the smallest possible one:** on/off only.
   - `0x43` (brightfield PWM enable) and `0x49` (epi enable), values 0/1.
   - Each light on for a short hold (default 3 s, max 10 s), then off, with read-back.
   - Intensity (`0x4C`), PWM width/period (`0x41`/`0x42`) and current mode (`0x44`/`0x4D`) are **not** changed. Changing them needs its own review (M8), not least because the digipot chip's volatility is Unknown.
2. **Classified as reviewed maintenance (RAM/IO write)** in `REVIEWED_MAINTENANCE["led_application.light_test"]`. The firmware trace shows no flash or persistent-memory path for these handlers.
3. **Fail safe:**
   - Never resent.
   - On any failure every light that may be on gets one OFF.
   - An unconfirmed OFF tells the operator to power-cycle.
4. **Only the cube in the light path (RFP) is tested.** Lighting the DAPI or GFP cube needs an F-carriage move, which belongs to the stage session (COM3).
   - Never look into the light path.
   - DAPI is near-UV.

## Amendment (2026-11-16): single-light mode for camera tests

- `--only brightfield|epi` runs one light.
- **Brightfield-only:** may hold up to 120 s. It is white transmitted light, requested by the camera session for colour-camera frames with F at the mirror. Epi stays limited to 10 s.
- **Unchanged:** the same commands (`0x43`/`0x49`, values 0/1), read-back, OFF-on-failure, phrases. Still no intensity control.
- The tool prints the UTC time each light turns on, so a camera session can record during the hold.

## Consequences

- Gives the first physical evidence that OpenEVOS can switch light, and whether the observer sees brightfield and RFP excitation.
- M8 (linearity/stability) still needs a reviewed intensity command and a power meter.
