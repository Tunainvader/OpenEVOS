# ADR-0036: Camera Gate 2 (mono), RAM-only settings writes with verified restore

- **Status:** Accepted. Revision 2 passed on 2026-11-13 18:37 UTC (operator-approved):
  - all settings read back, the restore was verified, 0 incomplete frames;
  - dark level 1,379 / 65,535 (86 in 12-bit units), unclipped;
  - read noise ≈ 5.7 ADU (12-bit) at 0 dB, at 1, 10 and 50 ms;
  - hot pair (88, 1187–1188) confirmed.

  Two earlier runs stopped: one on a byte-order bug of ours (16-bit data is big-endian), one on USB error 41. Revision 2 fixed both: packet size at 75 % of the maximum, and up to 3 incomplete frames skipped. Spec §6a–6c.
- **Spec:** [`camera_gate2_mono_settings_spec.md`](../evidence/camera_gate2_mono_settings_spec.md)

## Context

- Camera Gate 1 (ADR-0033) read the Chameleon CMLN-13S2M. At power-up it runs 1280×960 8-bit with auto shutter and gain and a black level of 0, so dark frames clip at zero.
- M9 dark-frame work and the app's scientific capture both need 16-bit full-sensor frames at manual exposure and gain. Those are camera writes, so under ADR-0009 they need their own reviewed gate.

## Decision

1. **Camera Gate 2 (mono)** does the following:
   - snapshot the state and compare it with the Gate 1 record;
   - set Format7 mode 0 at 1296×964 MONO16, auto exposure off, manual gain 0 dB, brightness 2 %;
   - take 3 dark frames each at shutter 1, 10 and 50 ms;
   - always restore the snapshot, then verify the restore by re-reading.
2. **Volatile registers only.** The allowed setters are `fc2ValidateFormat7Settings`, `fc2SetFormat7Configuration`, `fc2SetProperty` (auto exposure, shutter, gain, brightness, frame rate) and `fc2SetVideoModeAndFrameRate` (restore only). Memory-channel save/restore, register writes, configuration, LUT, strobe, trigger, bus reset and firmware calls are not bound.
3. **Envelope as Gate 1:**
   - dry run by default;
   - phrase `SET CAMERA MONO 1E10:2005 RAM ONLY AND RESTORE WITH LED OFF`;
   - constant values that the command line can't override;
   - one attempt, with any failure leading to restore and stop;
   - evidence in git-ignored `openevos-scans/`;
   - the user approves each run.
4. If the restore can't be verified, the operator power-cycles the instrument and Gate 1 is re-run.

## Consequences

- The first unclipped bias and read-noise numbers for M9, and a first check of the 12-bit left-alignment.
- The same settings path, once proven, becomes the app's `CameraDevice` backend for the mono role: manual exposure and gain, MONO16 Format7, and the SIMULATED badge off for real frames.
- Full M9 (photon transfer, linearity, defect map, 1,000-frame run) stays a later gate.
