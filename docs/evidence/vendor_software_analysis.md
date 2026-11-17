# Vendor software analysis: EVOS FL Auto 1.6 vs FL Auto 2 (2.0.2094)

- **Date:** 2026-11-12
- **Method:** static, offline unpacking only. Neither package was executed. Each package was copied into its own isolated folder and unpacked with 7-Zip, innoextract and msiextract. The Burn manifest was parsed to restore real file names, and binaries were inspected with string, PE-header and .NET-metadata readers. No vendor code is reproduced in this repository: the inventories (`fl_auto*_inventory.json`) hold only paths, sizes, SHA-256 hashes, PE bitness, version-resource fields and the USB IDs each binary references.
- **Evidence status:** everything below describes what the **vendor software expects**. None of it has been observed on our unit yet. On our unit these findings are at most *Strongly supported*, and they become *Verified* only after an Inspector scan or test on the real instrument confirms them (ADR-0003).

## 1. Packages analysed

| | FL Auto 1 (our hardware generation) | FL Auto 2 (reference) |
|---|---|---|
| File | `PearlUpdate_Rev1.6_31201.amg` (31.4 MB) | `SetupAuto_2_0_2094_0.exe` (226 MB) |
| SHA-256 | `5ac42b5725c6f213eea8c05d9e69f301a904b7fc6350d281b8cc9275b7d54cf0` | `46a11f781d6df490a672b835cc65790005d1111586169255a5a538de7329a3f8` |
| Container chain | ZIP → 7-Zip SFX (`TheUpdate.exe`) → Inno Setup 5.5.0 (`setup.exe`, app id "PearlScope") | WiX Burn bundle → `Auto.msi` (WiX 3.10, 2020-11-04) + `SetupEvosAnalysis.exe` (Inno 5.5.7) + .NET 4.6.1, VC++ 2015, Intel USB 3.0 xHCI driver; FlyCapture 2.7.3.18 MSI chained as an *external* (not included) payload |
| Build | ProductRevision 31201, changeset `4f59a6d2a2fe`, files dated 2014-06-04 | 2.0.2094.0, 2020-11-03 |
| Internal product name | **PearlScope** (`PearlScope64.exe`, native C++/MFC/ATL) | **DiamondScope** (`Thermo.Products.DiamondScope.exe`, .NET WPF) |
| Binaries | 27 PE files: 25 x64, 2 x86 (`PearlScopeSupport.dll`, `FLAutoUnpatch.exe`), no .NET | 290 PE files: 223 managed (.NET), 144 x64 native |

## 2. FL Auto 1 hardware stack, as the software sees it

| Subsystem | What the software uses | Transport | Evidence | Label (for our unit) |
|---|---|---|---|---|
| Mono camera | Point Grey **Chameleon** USB 2.0, "Sony ICX445AL (1/3″ 1296×964 CCD)"; FlyCapture2 (`FlyCapture2_v100.dll`, `PGRCamera264.dll`); driver `pgrusbcam` 2.3.3.59; firmware `chameleon-v1.12.2.0/1.17.3.0.ez2` | USB 2.0, `1E10:2005` hard-coded (rev 0003); INF also binds `1E10:2004` | `PearlScope64.exe` strings; `PearlScope.ini`; INF; usb.ids lists `1E10:2004` as "ICX445 IIDC [Chameleon]" | Strongly supported |
| Color camera | IDS **uEye UI-146xLE-C**; `UCam264.dll` → `ueye_api_64.dll`, `uEye_usb_64.sys` (**not** in the update; must come from the original install) | USB 2.0, VID `1409` (PID unknown) | `PearlScope.ini` camera section and `AllowUEyeColorCamera`; `UCam264.dll` strings | Probable |
| Motion: 6 axes | **Technosoft** drives through `TML_lib.dll` + `tmlcomm.dll`. Axes X, Y (stage), Z (focus), F (epi carriage: cube 1–4 plus mirror), O (objective turret), P (phase turret). The host downloads per-axis `.sw` setup files at initialisation (`TS_DownloadSwFile`) | **Serial COM port** (`\\.\COM%d` scan, "Relay Axis ID", "CANbus error"), so the expected topology is PC ↔ RS-232 ↔ relay drive ↔ CAN ↔ other drives | `MotorController64.dll` strings and imports; `MotorController/axis_firmware/*`; `PearlScope.ini` `[MotorControllerParameters]` | Strongly supported |
| Illumination | **EVOS Gen III LED board** (`EvosGenIII_LEDBoard64.dll`), epi/brightfield/prism channels; the board **also stores stage HW_DATA** (axis offsets, registration, mm/rev) | USB, `0DAD:0051` (bootloader DLL also lists 0050–0057), opened through SetupAPI with ReadFile/WriteFile | DLL strings | Strongly supported |
| Incubator (AMC1000) | **"CN" ("ChuckNorris") control board**, AMG PIC24F **USB HID** class; PID loops for water, glass, chamber, hose and ambient temperature; CO₂/O₂/N₂ and total-gas flow; O₂-sensor calibration; separate sensor slave board | USB HID, `0DAD:0058` | `CNControlBoard64.dll` strings (`AMG_PIC24F_HID…`, `HidBoardMultiManagement`); `Firmware/ChuckNorris*.hex`; `CN_parameter_update.ini` | Strongly supported (role); Probable (that our AMC1000 is this board revision) |
| Other | `AmgNetworkSupport` (WLAN, sharing), `Fiber64`, `MicronSupport64`, `GPUProcessing64`, stitcher; legacy `Evos_Update.ini` refers to older EVOS Gen III/SmartScope (not this instrument) | | | — |

USB vendor `0DAD` is registered to **Westover Scientific** in the public usb.ids database.

### Safety-relevant behaviours of the vendor software
1. **The host pushes incubator tuning parameters to the board.** On start-up, `CN_parameter_update.ini` "Forced Parameter Updates" writes PID gains and heater duty-cycle limits. The board's behaviour therefore depends partly on what the host sends.
2. **The host can reflash firmware.** PearlScope runs a "firmware update sequence" for the CN board, its sensor slave board and the LED board, using the bundled `.hex` files.
3. **Drive setup is downloaded from the PC.** Each Technosoft axis receives its `.sw` setup from the host. The original `.sw` files are calibration-critical artefacts to preserve.
4. **Instrument calibration is also stored on the LED board** (HW_DATA), not only on the PC disk.

## 3. FL Auto 2: what changed

| Subsystem | FL Auto 2 | Same as FL Auto 1? |
|---|---|---|
| Cameras | Point Grey **USB3**: Chameleon3 CM3-U3-31S4M/C, Flea3 FL3-U3-13S2M and FL3-U3-32S2C, through **Spinnaker only** (`Thermo.Automation.Camera.PointGreyResearch.dll` → `SpinnakerNET_v140`); `AutoUpdateFirmware=true` | **No.** Spinnaker does not support USB 2.0 cameras, so it cannot drive the FL1 Chameleon. No uEye support. |
| Motion and illumination | "M4L2" four-motor/two-LED board (`FourMotorTwoLEDBoard64.dll`, `Thermo.Automation.Instrument.DeviceLayer.M4L2Board`, `M4L2_*.hex`), HID-family IDs 0DAD:005A–0072 | **No.** No Technosoft/TML code at all. |
| Incubator | `CNControlBoard64.dll` + `Thermo.Automation.Incubator.CNControlBoardWrapper` (.NET) + `Incubator.CNBoard` plug-in | **Yes:** the same COM component (8/8 GUIDs and 270/270 parameter names shared with FL1), the same `0DAD:0058`. Firmware ships as `OSIApplication.hex`/`OSIChamberBoard.hex` (FL1 ships `ChuckNorris*.hex`), and an update helper decides whether to reflash. |
| Architecture | .NET 4.6.1 WPF; plug-in factories (`ICameraFactory`, `IDeviceFactory`, `IIncubatorFactory`, `IAutoFocusFactory`, `IZStackFactory`, image/movie access); `FakeCamera` + `SimulatedDevices` + simulated incubator; Autofac DI; XML/DataContract config | Different code base; same pattern as the OpenEVOS HAL |
| Reference data | XML for 51 objectives (AMEP-46xx/49xx, AMPFOP), phase rings, 50+ stage inserts, vessels (16 vendors), per-camera/per-channel brightness tables, motor settings per axis | FL1 INI uses the same AMEP catalogue numbers |

## 4. Reuse assessment for OpenEVOS (AMAFD1000)

| Item | Reuse? | How |
|---|---|---|
| FL2 application code | ✗ | Proprietary, and it targets different cameras and a different motion board. |
| FL2 incubator *interface semantics* | ✓✓ | The .NET wrapper's method and enum names describe the CN board command surface: setpoints, enable flags, valves, O₂ calibration, error and fault codes, over-temperature shutdown, firmware-update checks. Use it as the **specification** for an `AMC1000Backend` and implement it independently. Do not copy code. |
| FL2 workflows (acquisition, Z-stack, autofocus, field alignment, stage and camera-rotation calibration) | ✓ (as feature reference) | Re-implement from documentation and behaviour, as the earlier proposal suggested. |
| FL2 reference data (objective NA/magnification, vessel geometry) | ✓ (facts) | Re-derive as our own data files. Facts are reusable, but don't redistribute Thermo's XML files. |
| FL1 camera path | ✓ via existing open tools | Mono: Micro-Manager **Point Grey Research** adapter (FlyCapture2 only) or libdc1394/IIDC. Color: Micro-Manager **IDS_uEye** adapter (BSD, Windows/Linux). |
| FL1 motion | ⚠ native backend | Technosoft TML over serial. A Technosoft library from Technosoft, or a clean-room TML-over-serial driver; never send motion commands before limits, units and homing are validated. |
| FL1 LED board, CN board | ⚠ native backend | USB HID / SetupAPI devices; hidapi is a candidate. The protocol is undocumented, so: passive observation first, then read-only commands. |

## 5. Implications for the plan
- **ADR-0002 decision criteria are now partly answered:** both cameras have existing Micro-Manager adapters, motion is not Micro-Manager-shaped (Technosoft over COM, so a native backend), and the incubator is a separate HID device (`AMC1000Backend`, as planned).
- **The 32-bit risk is low for v1.6:** all hardware DLLs are x64.
- **Inspector update:** `src/openevos/inspector/data/candidate_ids.json` maps these USB IDs to candidate roles. `diff` and the new rule `R-CANDIDATE-ROLE` label matching devices *Strongly supported* or *Probable*, never *Verified*.
- **Preservation priority (adds to plan §10.5):** from the original HDD, keep the installed uEye driver/SDK version, the FlyCapture runtime, the `MotorController/axis_firmware/*.sw` files actually in use, `PearlScope.ini` with its per-unit calibration (objective calibrations, parfocality, XY offsets), the `Users/` and `Routines/` folders, and `Logs/`.
- **Do not install FL Auto 2 software on the PC connected to our instrument.** Its CN-board update helper and camera auto-update logic were written for different hardware, and it could reflash the AMC1000 control board.

## 6. Open questions to test on the unit (read-only)
1. Does the microscope snapshot show `1E10:2005` or `1E10:2004`, a `1409:xxxx` uEye camera, `0DAD:0051`, and a USB-serial COM port?
2. Does the AMC1000-only snapshot show a HID device at `0DAD:0058`?
3. Which uEye driver version and which `.sw` files are on the recovered HDD, and do they match the 1.6 package hashes?
