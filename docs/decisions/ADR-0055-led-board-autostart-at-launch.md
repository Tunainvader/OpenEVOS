# ADR-0055: Start the LED board application at app launch (amends ADR-0012)

- **Status:** Accepted (2026-11-18). Implemented, tested against a fake HID board, and run from the app on the
  instrument (2026-11-18 00:01 UTC, after a power-up: application in 1.0 s; the light then connected in the app).
- **Asked for by:** the operator, 2026-11-18: "every time I power-cycle or restart the app I find the light
  disconnected most of the time". They chose "auto-start at launch" over "one click in the precheck" and "keep it
  manual".
- **Amends:** ADR-0012 (StartApp as an operator-run L4 maintenance tool with a typed checklist).
- **Code:** `src/openevos/maintenance/led_autostart.py`; `src/openevos/ui/app.py` (runs it before the illumination is
  resolved; `--no-led-autostart` turns it off).
- **Tests:** `tests/test_led_autostart.py`.

## Context
- The LED board always powers up in its bootloader (`0DAD:0057`). ADR-0012's review found that the vendor software
  sends StartApp (`00 08`, one report) as the first LED-board traffic of every session.
- Under ADR-0012 OpenEVOS did not. The operator ran `led_bootloader` (Gate 1, the version query) and then
  `led_startapp` from a terminal after every power cycle. Until they did, the app showed the light as disconnected.
- The sequence has now run on this instrument three times (2026-11-17 and 2026-11-18). Each time the application
  appeared after about 1 s, with no other effect.

## Decision
1. At launch, before the illumination backend is chosen, the app runs the two reviewed steps **unchanged**:
   - the Gate 1 version query (`0x0A`);
   - then StartApp (`0x08`), followed by enumeration only, for up to 30 s.
2. All of the tools' preconditions still hold:
   - exactly one bootloader interface, and no application interface present;
   - VID/PID and 65/65-byte reports confirmed by the driver;
   - a valid version reply that evidences an application image, with its record under 30 minutes old.
3. **What changes:** the launch path now supplies the checklist and confirmation phrases. Choosing auto-start is the
   operator's standing approval, given in place of typing the phrases each session.
4. Each step writes its JSON evidence record to `openevos-scans/`, as the tools do. Nothing is retried, and nothing is
   sent to the application from this path.
5. **When nothing is sent:**
   - the application is already running;
   - no board is present;
   - the version reply does not evidence an application image.
6. The app prints one `LED board: …` line saying what happened. The start-up precheck reports the light's final
   state.
7. `--illumination sim` and `--no-led-autostart` skip it, and screenshot runs never do it.

## Not changed
- No firmware, EEPROM, calibration or HW_DATA write exists in either step. ADR-0009's bootloader quarantine of the
  *command core* stays: the core still sends nothing to a board in its bootloader. This path runs before the core
  exists.
- Gate 2 reads (checksum, calibration) still need their own approval.
- The manual tools still work. `python -m openevos.maintenance.led_autostart` runs the launch step on its own.

## Guards
- `safety/firmware.py` registers `maintenance/led_autostart.py` as a reviewed maintenance module
  (`led_bootloader.start_app_at_launch`). Only the registered modules may name the StartApp operation; the static scan
  in `tests/test_firmware_safety.py` enforces that.
- `tests/test_phase2_import_guard.py` still forbids any application module from importing a maintenance tool, with
  exactly one exception: `ui/app.py` → `openevos.maintenance.led_autostart`, present once.

## Restart without a power cycle (tested 2026-11-18)
After a normal close of the app, the board **stays** in its application (`0DAD:0051` for 20+ s; the app's exit sends
only light-off and level-restore). So the light shows disconnected after a power cycle, not after an app restart. If
the app is killed while the board is mid-command, a power cycle may still be needed; auto-start then handles the
power-up.
