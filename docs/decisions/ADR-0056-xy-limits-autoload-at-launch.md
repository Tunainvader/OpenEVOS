# ADR-0056: Load the calibrated X/Y limits at app launch (amends ADR-0022)

- **Status:** Accepted (2026-11-18). Implemented and tested with a recorder in place of the drives. Gate 7 itself ran
  on the instrument the same day (`motor-gate7-20261118T001942-pass.json`).
- **Asked for by:** the operator, 2026-11-18. After a power-up "stage not moving and objectives". The stage was held
  in the drives' power-up box, so it could not be centred and the turret refused every change. They chose "load at
  every launch" over "keep it manual".
- **Amends:** ADR-0022 (Motor Gate 7 as an operator-run, reviewed tool).
- **Code:** `src/openevos/maintenance/motor_autolimits.py`; `src/openevos/ui/app.py` (runs it before the session
  opens the motor port; `--no-limits-autoload` turns it off); `src/openevos/ui/instance_guard.py` (single instance).
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

## Amendment (2026-11-23): the single-instance guard sees a copy started from another app's container
- **What happened:** the guard was a lock file next to the settings, in `%LOCALAPPDATA%\OpenEVOS`.
  - A copy started from inside a packaged (MSIX) app, such as a terminal in the Claude desktop app, sees that folder
    through the packaged app's private copy, where new files land.
  - Each copy then found only its own lock file, so both could run.
  - A copy started with another `--settings` missed the lock file too.
- **The fix:** `src/openevos/ui/instance_guard.py`.
  - On Windows a named kernel object (`Local\OpenEVOS-instrument`, the logon session's namespace) is taken first,
    then the lock file as before. The lock file still records the PID and is the only guard off Windows.
  - The named object is a one-byte Qt shared-memory section (`QSharedMemory` with a native key), not a mutex made
    through `ctypes`: the UI package stays free of FFI (`tests/test_phase2_import_guard.py`).
  - Kernel object names are shared across that boundary. This was checked both ways on the instrument PC, then
    with this code: a copy inside the container held the guard, and one started through Explorer with its own
    settings folder was refused.
  - The named section is the same whatever `--settings` a copy uses. The kernel drops it when its holder exits, crashed or
    not.
  - `main()` releases both guards however the app ends, a startup exception included.
- **Not fixed here:** a copy started inside a container still keeps its own settings, logs and MCP token. Start the
  app from its shortcut (`tools/create_shortcuts.ps1`).
- **Tests:** `tests/test_instance_guard.py` and `tests/test_app_launch_branches.py`. Tests use their own section name,
  so they are never refused by the app running on the same PC, and never block it.

## Not changed
- No firmware, EEPROM, setup or speed write. The PC-side travel box and the drift monitor (ADR-0040) are unchanged.
- The import guard allows exactly this module and `led_autostart` from `ui/app.py`, and nothing else from the
  application.
