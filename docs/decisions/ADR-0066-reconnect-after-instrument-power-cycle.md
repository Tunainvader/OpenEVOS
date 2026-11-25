# ADR-0066: Reconnect the cameras and the LED board after the microscope is switched off and on (amends ADR-0055)

- **Status:** Accepted (2026-11-25). Tested against fakes (FlyCapture2, uEye, LED board). Not yet seen on the
  instrument.
- **Asked for by:** the operator, 2026-11-25: "check the app why such problems, specially with camera and z". They
  had switched the microscope off and on at 15:03:45 IST with OpenEVOS running ("Yes, I did").
- **Amends:** ADR-0055 (the LED board's StartApp ran at launch only).
- **Code:**
  - `backends/flycapture2/camera.py` and `backends/ueye/camera.py`: `_start_reconnect`, `_reopen`, `recover`;
  - `backends/ledboard/illumination.py`: `_presence_loop`, `_board_state`, `_try_reconnect`, `restarter`;
  - `ui/app.py` supplies `restarter` (the launch's `led_autostart`);
  - `ui/session_model.py` logs a warning when the motor controller comes back.
- **Tests:** `tests/test_backend_flycapture2_camera.py`, `tests/test_ledboard_illumination.py`.

## What happened (event log, 2026-11-25 09:33:44–09:34:35 UTC)
- At the same instant, COM3 (the motors), the mono camera and the colour camera all left USB. COM3 came back 10 s
  later; Windows logged the colour camera again at 09:33:53.
- The drives powered up again. X and Y homed by themselves, Z read 0 instead of 10.49 µm, and the stage service
  reloaded the calibrated X/Y limits at 09:34:27 (ADR-0056 already covered this).
- The **mono camera** tried one in-place restart while it was still off USB ("Isochronous transfer has not been
  started"), then stayed in fault. Every capture was refused, although the camera was back on USB within seconds. Only
  an app restart cleared it.
- The **LED board** came back in its bootloader (0DAD:0057). The app held a handle to the application interface that
  no longer existed. Every light command failed with "LED OFF could not be confirmed: power-cycle the instrument now",
  which was wrong twice over: the restarted board's lights were off, and another power cycle leaves it in the
  bootloader again.
- Nothing told the operator that the stage and focus had re-homed.

## Decision
1. **Cameras.** When a stream fails, the backend looks for the camera every 2 s for up to 5 min. It uses bus
   enumeration only (a temporary context; nothing is opened). Once the one camera is counted, the stale handle is
   closed and the camera is opened **exactly as at launch**: the same connect, the same operating settings, each read
   back. Live view resumes if it was running. The mono camera keeps the power-up snapshot from its first connect, so the
   exit still restores that state. Service › Hardware › Recover runs the same reopen. Before, Recover reported
   "recovered" on a stale handle.
2. **LED board, presence.** While connected, the board's USB presence is checked every 2 s. This is SetupAPI enumeration
   only: nothing is sent, and it never queues behind a light command. If the interface that was opened is gone, the
   handle is closed, the light is marked off, a command in flight fails, and the device reports FAULT with what
   happened:
   - **restarted** (the board is back, in its bootloader or re-enumerated): its lights are off;
   - **gone** (not on USB): switch the microscope off if it is still on.
3. **LED board, restart.** When the board is back in its bootloader, the backend calls `restarter`. This is the
   launch's `led_autostart`, **unchanged**:
   - the Gate 1 version query, then StartApp;
   - every precondition, evidence record and refusal as in ADR-0055.

   There are at most three tries per loss. Recover resets the count. Then the backend connects as at launch: both
   lights OFF, each confirmed by read-back. The operator's standing approval for auto-start (ADR-0055) covers this
   path; `--no-led-autostart` turns off both. Without a restarter, the backend reconnects only once the application
   is back by itself.
4. **Messages.** "Power-cycle the instrument now" is kept for a board that is still on USB but does not confirm OFF.
5. **Operator notice.** When the stage's motor link comes back after a drop, the Capture page shows a warning: the
   stage and focus re-home by themselves, so focus again; the cameras and the light reconnect on their own.

## Not changed
- ADR-0009's quarantine of the command core: no light or core command is ever sent to a board in its bootloader. Only
  the reviewed `led_autostart` steps reach the bootloader, as at launch. They are now also run from the LED backend's
  worker thread, only after the board it had open left USB.
- No firmware, EEPROM, calibration or HW_DATA write anywhere. The camera reopen uses only the calls the launch's
  connect already makes.
- The light is never switched on by a reconnect. A command that failed is not resent.
- `safety/firmware.py` registers this use as `led_bootloader.start_app_after_power_cycle`, with the same module and
  commands.
