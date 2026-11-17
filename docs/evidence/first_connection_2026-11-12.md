# First connection: AMAFD1000 and AMC1000 on the replacement PC (2026-11-12)

- **Host:** Dell OptiPlex 9020, Windows 11 Pro N build 26300 (the replacement EVOS PC).
- **Scans:** both read-only, with no device opened.
  - Baseline: `baseline-this-pc-v011-20261111T223432Z-5b6f4e`, nothing connected.
  - Connected: `scope-and-incubator-connected-20261112T091506Z-231aee`, microscope and incubator both plugged in by USB at the same time.
- **Diff:** `openevos-scans/diff-baseline-vs-connected/`. The scan folders are git-ignored because they contain serial numbers.
- **Labels:** follow ADR-0003. "Reported" means Windows read the value from the device itself (Verified); "role" means what the device is for in the instrument.

## Devices that appeared

USB tree, as Windows reports it:

```text
Intel EHCI #1 → root hub → Intel hub 8087:8000
  └─ 0424:2514  4-port USB 2.0 hub           ← the microscope's cable (all four ports used)
       ├─ 1E10:2005  "USB 2.0 Digital Imaging Camera", serial in instance ID   no driver (code 28)
       ├─ 1409:1000  (no name reported)                                      no driver (code 28)
       ├─ 0DAD:0057  "EVOS Gen 3 v2 LED Board Bootloader"                    Windows HID driver
       └─ 0403:6001  "FT232R USB UART" (FTDI USB-serial)                     no driver (code 28) → no COM port yet
Intel EHCI #2 → root hub → Intel hub 8087:8008
  └─ 0DAD:0058  "Life Technologies CN Control Board 1.00"                    Windows HID driver   ← the incubator's cable
```

An HP mouse (`03F0:134A`) and a Wi-Fi Direct virtual adapter also appeared; both are unrelated.

| Device | Evidence on our unit | Role and label |
|---|---|---|
| `1E10:2005` | Present behind the microscope hub. Exactly the ID PearlScope 1.6 hard-codes for the Chameleon. | Mono CCD camera (Point Grey Chameleon): **Strongly supported**. Two independent sources: the device on the microscope's own hub, and the vendor software. |
| `1409:1000` | Present behind the microscope hub. Vendor ID 1409 = IDS; no name reported. | Colour camera (IDS uEye): **Strongly supported** (was Probable). It's the only other camera-class candidate on that hub. |
| `0DAD:0057` | Reports **"EVOS Gen 3 v2 LED Board Bootloader"**. | LED / illumination board: **Verified** identity. It is running its **bootloader**, not its application. PearlScope talks to the application at `0DAD:0051`. See *Caution* below. |
| `0403:6001` | FTDI FT232R USB-serial, behind the microscope hub. | Serial link to the Technosoft motion drives: **Probable**. The vendor software scans COM ports for a relay axis; this is the hub's only serial device. Once a driver binds it becomes a COM port. |
| `0424:2514` | 4-port hub carrying all four devices above. | The microscope's internal hub: **Strongly supported**. |
| `0DAD:0058` | Reports **"Life Technologies CN Control Board 1.00"**, on a separate host port. | AMC1000 incubator control board: **Strongly supported** (role), **Verified** (board identity string). |

## Caution: the LED board is in bootloader mode

The board identifies itself as a bootloader (`0DAD:0057`), not as its application (`0DAD:0051`). Possible explanations:

1. It starts in its bootloader at every power-up and waits for the host software to start the application.
2. Its application firmware is missing or damaged; for example, an earlier firmware update was interrupted.

### Static check of the 1.6 package (same day)

The package was unpacked offline with Python's standard library only (the ZIP, 7z and Inno Setup LZMA layers), and strings were read from the binaries. Nothing was executed.

- **`EvosGenIII_LEDBoard_Bootloader64.dll`** (COM interface `IGen3LedBoardBootloader`) offers:
  - `GetFirmwareVersion`, `BootloaderBoardType` and **`GetApplicationFirmwareVersion`**;
  - **`StartApp`**;
  - `DownloadHex` and `VerifyHex`, for flashing.

  It lists IDs `0DAD:0050`–`0057`.
- **`PearlScope64.exe`, lighting controller** (`CPearlScopeLightingController`):
  - references `PID_0057` and `PID_0051` side by side;
  - reads `Firmware\EVOS_Update.ini`;
  - logs `GetUpdateFirmware: Current Versions … Update Versions …`;
  - can flash `Firmware\LED Board App - EVOS Gen III.hex` (`UpdateFirmware: successfully updated led board firmware`);
  - writes `PearlScopeLightingController.log` and `…-UpdateFirmware.log`.

**Reading:** the original software is built to find the board in either its bootloader or its application and to manage that itself. The likely start-up sequence:

1. find the board at `0057`;
2. compare its application firmware version with the bundled one, and flash if they differ;
3. send `StartApp`, after which the board reappears as `0051`.

| Explanation | Label after this check |
|---|---|
| 1. The board waits in its bootloader at every power-up for the host | **Probable**. The code path exists and is routine-looking. |
| 2. Application firmware missing or damaged | **Not ruled out**. Looks identical from the host until the version is read. |
| The original PearlScope flashes the LED board when versions differ | **Strongly supported** (strings) |

**What would decide it, read-only:** `PearlScopeLightingController*.log` from the recovered original HDD. These logs record the current and bundled firmware versions and any update. Querying the bootloader (`GetApplicationFirmwareVersion`) would also decide it, but that is a command sent to the device, so it waits until the HID protocol is documented and reviewed (ADR-0005).

These are hypotheses until confirmed. This board also stores stage calibration (HW_DATA, per the vendor analysis), so:

- **Do not** run any vendor updater, FL Auto 2 software, or the original PearlScope on this PC. PearlScope itself flashes the LED board when the firmware versions differ.
- A power-cycle and rescan cannot tell the two explanations apart, because both leave the board at `0057`.
- **Original HDD: abandoned (owner decision, 2026-11-12: "dead, forget it").** The original OptiPlex HDD clicks and is not detected by the BIOS. No recovery will be attempted. Its logs (`PearlScopeLightingController*.log`), `Firmware\EVOS_Update.ini`, per-unit `PearlScope.ini` and the in-use `.sw` drive files are permanently unavailable. Do not plan work that depends on them.
- **What can still decide the question:**
  - offline analysis of the v1.6 package (see `fl1_v1.6_led_board_static_analysis.md`);
  - later, a reviewed read-only version query. ADR-0009 treats that as a separately approved step.

## Drivers not yet installed (no action taken)

| Device | Needs |
|---|---|
| `1E10:2005` | FlyCapture2 (a release providing `FlyCapture2_v140.dll`) and its `pgrusbcam` driver. Vendor account needed. |
| `1409:1000` | IDS uEye driver ≥ 4.30, providing `ueye_api_64.dll`. Vendor account needed. |
| `0403:6001` | FTDI VCP driver, which creates a COM port. Free from FTDI, often supplied by Windows Update. Even with a COM port, **no data may be sent** to the drives until ADR-0005's motion gate is met. |

The two HID boards use Windows' built-in HID driver and need nothing installed.

### Update (same day): FTDI driver bound, COM3 present

- **The driver is installed:** FTDI CDM 2.12.36.20 (`ftdibus.inf` → `oem6.inf`, `ftdiport.inf` → `oem7.inf`), WHQL-signed by "Microsoft Windows Hardware Compatibility Publisher".
- **Windows installed it on its own** at 14:48–14:49, most likely through Windows Update, before the owner's official download. Running `pnputil /add-driver … /install` on that download (zip SHA-256 `E8EBF401…EAF1`) found the same package already in place and up to date.
- **Result (Verified, read-only queries):** `USB\VID_0403&PID_6001\A702K26K` = "USB Serial Converter", and `FTDIBUS\…\0000` = **"USB Serial Port (COM3)"**. Both are Status OK, problem code 0.
- **COM3 has not been opened.** No byte goes to it until ADR-0005's motion gate is met: `.sw` setup files and per-unit `PearlScope.ini` preserved; units, limits, homing and stop behaviour validated.
- Role of COM3 (link to the Technosoft drives) stays **Probable** until traffic from the original software, or documentation, confirms it.

### Update (same day): Intel chipset drivers from Windows Update

- **What was installed (owner's request, 15:11–15:12):** only the two updates matching `pci\ven_8086&dev_8c3a` and `pci\ven_8086&dev_8c22`, through the Windows Update API. Result code 2 (succeeded) for both. **Windows reports a restart is required**; none has been done yet.
- **Before/after snapshot of all 99 present devices (read-only).** Every difference is on Intel chipset PCI devices:

  | Device | Before | After |
  |---|---|---|
  | Management Engine Interface (`8C3A`) | Error, code 28 | OK; Intel 11.7.0.1032 (`oem8.inf`, service `MEIx64`) |
  | SMBus Controller (`8C22`) | Error, code 28 | OK; INTEL 10.1.1.38 (`oem9.inf`) |
  | LPC Controller (`8C4E`) | OK; Microsoft `machine.inf` | OK; INTEL 10.1.1.38 (`oem9.inf`). Intel's chipset INF also matches this device; it only adds identification. |

- **Instrument devices:** no change to any microscope or incubator device (`0424`, `0403`, `0DAD:0057`, `0DAD:0058`, `1E10`, `1409`): same status, driver and service before and after.
