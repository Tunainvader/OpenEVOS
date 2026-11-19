# ADR-0029: Widen the app's physical X/Y travel box to the calibrated envelope

- **Status:** Accepted, implemented (2026-11-13). Tested with the simulated drive chain; **not yet used from the app on the instrument.**
- **Amends:** ADR-0024 (0–22 mm power-up box).
- **Evidence:**
  - Gate 7 (calibrated limits loaded and read back, `motor-gate7-20261113T103701-pass`);
  - Gate 8 (X 0 ↔ 115.299 mm and Y 0 ↔ 74.288 mm with 0 counts error, `motor-gate8-20261113T104204-pass`).

## Decision
- **The travel box:** the PC travel box for `motor.stage` becomes, per axis, **0 … (calibrated max − 2 mm)**: exactly the range Gate 8 verified.
  - It is computed at start-up from the git-ignored parsed calibration (`--calibration`, default: the newest `led-application-stepd-*-parsed.json` in `--scans-dir`).
  - It is validated like Gate 7: minimum within (−2 mm, 0], span within ± 0.5 mm of 119.1 / 78.1 mm.
  - Any problem falls back to the 0–22 mm box, and the UI says which box is in force.
- **The drives still decide:** before every move the backend re-reads each drive's own `LPOSMIN`/`LPOSMAX` and refuses, writing nothing, if the target is outside them. After a power-off the drives hold 0…50,000 again, so far moves are refused with "run Gate 7 (calibrated limits) after power-on". Loading limits from the app is **not** part of this decision; Gate 7 remains the only limit writer.
- **Capability record:** it cites the full-range evidence (ADR-0022, ADR-0028 and the Gate 7/8 records) when the calibrated box is in force.
- **Unchanged:** the minimum stays 0 (home), not the calibrated −1.764 mm: the range behind home was not part of Gate 8.

## Consequences
- **What the controls can reach:** the Hardware page's fixed steps and the Capture joystick can reach the whole verified envelope.
- **Vessel-map moves stay disabled** for the physical stage (no vessel ↔ stage calibration). *Superseded by ADR-0060
  (2026-11-20): enabled once a saved plate alignment exists.*
- **A future decision:** loading the limits at app start-up, as PearlScope does.
