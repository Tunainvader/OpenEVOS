# ADR-0056: Load the calibrated X/Y limits at app launch (amends ADR-0022)

- **Status:** Accepted (2026-11-18). Implemented and tested with a recorder in place of the drives. Gate 7 itself ran
  on the instrument the same day (`motor-gate7-20261118T001942-pass.json`).
- **Asked for by:** the operator, 2026-11-18. After a power-up "stage not moving and objectives". The stage was held
  in the drives' power-up box, so it could not be centred and the turret refused every change. They chose "load at
  every launch" over "keep it manual".
- **Amends:** ADR-0022 (Motor Gate 7 as an operator-run, reviewed tool).
- **Code:** `src/openevos/maintenance/motor_autolimits.py`; `src/openevos/ui/app.py` (runs it before the session
  opens the motor port; `--no-limits-autoload` turns it off).
- **Tests:** `tests/test_motor_autolimits.py`.

## Decision
- **When it runs:** only with `--motor-control` on a real port (the "OpenEVOS (real stage)" shortcut). The launch
  path calls Gate 7's `run()` unchanged, once as a dry run and then as the send.
- **What it can send:** only Gate 7's four frames (LPOSMIN/LPOSMAX on X and Y) with the values validated from the
  git-ignored parsed calibration. Each value is read back, there is no motion, and the Z/F limits are checked
  untouched. The writes go to RAM only; a power cycle restores 0..50,000.
- **When it refuses:** Gate 7's own checks all still apply: exactly one FTDI port, normal driver state, line sync,
  gateway axis 1, every axis homed and idle, X and Y at home, and current limits either power-up or already the
  calibration.
- **Repeat launches:** after the first launch since power-on it finds `already_loaded` and writes nothing.
- **What changes:** the launch path supplies the operator's confirmations (instrument on, stage clear, phrase). Choosing
  this at launch is the operator's standing approval.
- **Output:** each attempt that opened the port writes Gate 7's evidence record. The app prints one `Stage limits:`
  line, and the start-up precheck reports power-up limits if they are still in place.

## Amendment (2026-11-18, same day): reload after homing, inside the running app
- **What happened:** the operator power-cycled to clear a drift lock while the app ran (the lock's own recovery). X
  and Y came back on the power-up limits, and the launch-time load cannot help a running app. A relaunch right after
  the power cycle then met `not_idle` (X still homing, ESTATEV 1). The stage stayed in 0–22 mm, and the operator saw
  it as "locked".
- **The fix:** the stage service (the owner of COM3) now runs the same four frames itself, once per homing. It does
  so only when X and Y are homed, idle, at home (|APOS|, |CPOS| ≤ 5) and on exactly the power-up limits, and no job
  is queued.
  - The frames come from `protocol.limit_write`, which a test checks byte for byte against Gate 7's builder.
  - The values come from the same validation as Gate 7 (`calibrated_limits_counts`).
  - The port accepts these frames only while the service registers them for this one operation
    (`transport.check_write(extra=...)`), and only with motor control.
  - Each value is read back. A mismatch stops the writes and reports a stage fault.
  - Evidence goes to `stage-monitor/limits-reload-<time>-pass|fail.json`.
- **Single instance:** a second copy of the app on real hardware now shows "already running" and exits before
  touching anything. Two copies started 6 s apart that day.

## Not changed
- No firmware, EEPROM, setup or speed write. The PC-side travel box and the drift monitor (ADR-0040) are unchanged.
- The import guard allows exactly this module and `led_autostart` from `ui/app.py`, and nothing else from the
  application.
