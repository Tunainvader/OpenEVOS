# ADR-0038: Camera Gate 2 (colour), RAM-only settings writes with re-init restore check

- **Status:** Accepted. Revision 2 **passed** on 2026-11-15 at 15:01 UTC (operator-approved):
  - RAW8 with black-level offset 64;
  - exposures 1, 10 and 50 ms read back;
  - unclipped dark level about 3.7 DN, temporal noise about 0.44 DN (8-bit quantization floor);
  - no hot pixels; restore verified.

  Run 1 stopped (restore verified) and found two things: **RAW8 only**, and that the black-level auto switch is **not supported** (caps 2, offset only, SET_MODE → 155). Revision 2 sets only the offset. Spec §6a–6b.
- **Spec:** [`camera_gate2_colour_settings_spec.md`](../evidence/camera_gate2_colour_settings_spec.md)
- **Builds on:** ADR-0033 (Gate 1; colour PASS 2026-11-15) and ADR-0036 (mono Gate 2 pattern).

## Context

- Colour Gate 1 read the uEye UI146xLE-C (Bayer, 2048×1536).
- After init it runs 8-bit BGR at maximum exposure with all autos off. Its black-level-corrected output is 0 in the dark, so dark frames carry no noise information.
- M10 dark-frame work and the app's colour capture need raw Bayer data, manual exposure and a known black level. Those are camera writes, so under ADR-0009 they need their own reviewed gate.

## Decision

1. **Camera Gate 2 (colour)** does the following:
   - snapshot the state and compare it with the Gate 1 record;
   - choose the first accepted raw Bayer mode (RAW16 → RAW12 → RAW10 → RAW8);
   - set master gain 0, black-level auto off and a constant offset;
   - take 3 dark frames each at 1, 10 and 50 ms;
   - always `is_ExitCamera`;
   - verify by re-init and comparison with the snapshot.
2. **Session RAM only.** A subclass of the Gate 1 adapter adds guarded setters:
   - `is_SetColorMode` (raw candidates only);
   - `is_SetHardwareGain` (0, −1, −1, −1);
   - `is_Blacklevel` (mode off, offset B);
   - `is_Exposure` SET (1, 10, 50 ms).

   Not bound: parameter-set save/load, EEPROM writes, configuration, LUT, I/O, trigger, camera ID, pixel-clock, frame-rate and area setters, the starter-upload flag, and anything firmware-related.
3. **The envelope is the same as the mono Gate 2:**
   - dry run by default;
   - phrase `SET CAMERA COLOUR 1409:1465 RAM ONLY AND RESTORE WITH LED OFF`;
   - constant values that the command line can't override;
   - one attempt;
   - up to 3 skipped grabs per exposure;
   - evidence in git-ignored `openevos-scans/`;
   - the user approves each run.
4. If the restore can't be verified, the operator power-cycles the instrument and Gate 1 is re-run.

## Consequences

- The first unclipped bias and read-noise numbers for the colour camera, which raw bit depth it supports, and its bit alignment.
- The proven path becomes the app's colour `CameraDevice` backend (raw Bayer plus demosaic for display), following ADR-0037.
- Whether images come through with light, and the Bayer order, are confirmed in the first approved app session, not in this gate.
