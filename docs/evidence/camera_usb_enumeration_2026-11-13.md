# Camera USB enumeration (M2, camera part), 2026-11-13

- **Method:** read-only. `Get-PnpDevice -PresentOnly` and `Get-PnpDeviceProperty` on the replacement PC (Windows 11 Pro N 26300), microscope on and connected. No device was opened, no driver or SDK installed, nothing downloaded. COM3 (the stage) was not touched.
- **Raw record:** `openevos-scans/camera-usb-enumeration-20261113T172422Z.json` (git-ignored: it contains the serial number).
- **Labels:** ADR-0003. "Reported" = Windows read it from the device (Verified).

## Result

```text
0424:2514 microscope hub (Port_#0004.Hub_#0004)                → its child hub is Hub_#0006
  ├─ Port 1  0DAD:0057  LED board bootloader (HID, OK)          (not a camera; see note)
  ├─ Port 2  1409:1000  no name reported, REV 0000              Status Error, problem code 28 (no driver)
  ├─ Port 3  1E10:2005  "USB 2.0 Digital Imaging Camera", REV 0003   Status Error, problem code 28 (no driver)
  └─ Port 4  0403:6001  FTDI → COM3 (stage; not touched)
```

| Item | Mono camera | Colour camera |
|---|---|---|
| VID:PID, revision | `1E10:2005` rev `0003`, **Verified** (reported) | `1409:1000` rev `0000`, **Verified** (reported) |
| USB class | `FF/00/00` vendor-specific (not UVC, not USB Video), **Verified** | `FF/FF/FF` vendor-specific, **Verified** |
| Product string | "USB 2.0 Digital Imaging Camera", **Verified** | none, **Verified** |
| Serial | present in the PnP instance ID (device-reported iSerial); recorded only in the git-ignored scan | **none**: the instance ID is a port-derived ID (`7&…&2`), so the device reports no iSerial. Serial must come from the uEye SDK. **Verified** (absence) |
| Driver | none bound (code 28) | none bound (code 28) |
| Vendor SDK on this PC | no FlyCapture2 / Spinnaker install, no `pgrusbcam` in the driver store | no `C:\Program Files\IDS`, no `ueye_api_64.dll`, no uEye driver in the driver store |
| Identity | Point Grey **Chameleon** (ICX445, 1296×964 mono): **Strongly supported** (exact VID:PID:REV PearlScope 1.6 hard-codes; usb.ids names the sibling `2004` "ICX445 IIDC [Chameleon]") | IDS **uEye UI-146xLE-C**: **Strongly supported** (IDS vendor ID; PearlScope's only colour camera). PID `1000` is shared by IDS's USB 2 uEye family, so the exact model is **Probable** until the SDK reads it |

## Downloaded by the user (2026-11-13, not installed)

| File | Size | Authenticode | SHA-256 |
|---|---|---|---|
| `FlyCapture_2.13.3.61_x64.exe` | 307 MB | **Valid**, signed by FLIR Integrated Imaging Solutions, Inc. | `240C67ED801ADA165F84CC613C1AD18FC78DE584686512C3E827B3F6637EC2A9` |
| `PyCapture2-2.13.61.win-amd64.zip` | 83 MB | n/a (zip; contains MSIs for Python 2.7 / 3.5 / 3.6 only) | `8458D0195EC704EE66CC75EFF35DC322E65E9AD07CA864D1742A460A2B3EA92C` |

PyCapture2 does not support the project's Python 3.12, so the mono adapter will use ctypes against `FlyCapture2_C.dll`, as planned in the gate spec. Do not install the PyCapture2 MSIs.

## After installing FlyCapture2 2.13.3.61 (2026-11-13, installed by the user through the GUI)

Read-only PnP check, with the camera never opened:
- `1E10:2005` is now **OK**, problem code 0. Windows names it **"PGR Chameleon CMLN-13S2M"** (from the INF match, class `PGRDevices`). The model is therefore **Strongly supported**: the INF maps this VID:PID:REV to the CMLN-13S2M, and the SDK will confirm it in Gate 1.
- Service `PGRUSBCam`, driver version **2.7.3.235** (`oem10.inf`). That is close to the 2.7.x line PearlScope used.
- SDK DLLs are in `C:\Program Files\Point Grey Research\FlyCapture2\bin64\`: `FlyCapture2_C_v100.dll`, plus `vs2013\…_v120` and `vs2015\FlyCapture2_C_v140.dll`, all version 2.13.3.61. The adapter will load `vs2015\FlyCapture2_C_v140.dll`.

## Consequences

1. **Plan M2 finding "the uEye PID" is answered: `1000`.** The Chameleon serial is recorded (scan). The uEye serial is **Unknown** until its driver is installed.
2. **No frame can be grabbed today.** Both cameras are vendor-class devices with no driver. Windows' built-in camera stack (Media Foundation / DirectShow) cannot see them. Any first image needs one of these, each requiring the user's approval to download and install (plan §12):
   - Mono: FLIR **FlyCapture2** 2.x (provides `pgrusbcam` + `FlyCapture2_C.dll`), or a WinUSB binding plus a libusb/IIDC stack (libdc1394 has no maintained Windows build: **Probable** not practical).
   - Colour: **IDS Software Suite 4.9x** (provides `uEye_usb_64.sys` + `ueye_api_64.dll`); 4.97 is the plan's pick.
   Neither SDK may update camera firmware (plan risk table; the vendor package carries Chameleon firmware 1.12.2.0 / 1.17.3.0).
4. **Colour camera bring-up, step 0 (2026-11-15).** Read-only check:
   - `1409:1000` is still present, Status Error, with no driver.
   - No IDS files are on C: (`ueye*` search). The only hit is `C:\Program Files\Micro-Manager-2.0.3_20261106\mmgr_dal_IDS_uEye.dll`, a Micro-Manager adapter that needs the IDS runtime.
   - Proposed software: **IDS Software Suite 4.97, Windows 64-bit**, the final release. IDS's uEye manual says its fixes keep the suite usable on Windows 11. **Verified** only from a secondary copy of the manual ([1stvision "what's new"](https://1stvision.com/cameras/IDS/IDS-manuals/uEye_Manual/hw_einleitung_neues.html)). The suite is legacy, but it supplies the USB 2 uEye driver and `ueye_api_64.dll`, which Gate 1's `colour` role expects.
   - IDS peak is not an alternative: it runs UI-model uEye cameras through the Software Suite's driver.
   - **The download needs an IDS account**, so the user downloads it, as with FlyCapture2. File name and size will be known after download. Before installing, verify the Authenticode signature (IDS Imaging Development Systems GmbH) and record the SHA-256 here.
   - **Install scope:** USB uEye driver plus SDK/runtime only. **Untick the GigE/ETH driver** if offered: it adds a network filter driver, which is a system network change outside this task. Skip IDS peak and the demo tools.
   - **Downloaded instead (by the user, 2026-11-15):** `ids-peak-win-extended-setup-64-26.06.1.exe`, 356,032,504 bytes.
     - Authenticode **Valid**, signed by IDS Imaging Development Systems GmbH, DigiCert timestamp.
     - Product "IDS peak" 26.6.1. SHA-256 `A80DE27838D18B97C832EC11C860BC1CE3591806A1BE5AF83965A21C7D7CD12E`.
     - Per the IDS peak 26.06 readme (1stVision copy), the **extended** setup "also contains the latest uEye camera drivers for UI models", so UI cameras work without the Software Suite.
     - The readme also says it **automatically uninstalls an existing IDS Software Suite**, which has no effect here because none is installed.
     - Tested Windows versions: 11 Pro 25H2 and 10 Pro 22H2.
     - It also installs firmware-update command-line tools for GigE and USB3 Vision cameras. Those tools never run on their own and must not be run.
     - Optional parts: a GigE Vision transport layer with an Ethernet kernel driver. **Untick it in a custom install** if the installer offers that.
     - Whether the bundled uEye driver still covers the USB 2 UI-146xLE and installs the classic `ueye_api_64.dll` is **Probable**, not confirmed. It will be checked read-only after the install (PnP status, driver name, DLL path). If the classic API is missing, Gate 1's colour role will target IDS peak instead.
     - After the install, do not open the camera with IDS Cockpit or Camera Manager: their firmware prompts are outside the gate.
   - **After the install (2026-11-15; installed by the user through the GUI). Read-only PnP and file check, camera never opened:**
     - **The camera re-enumerated.** `1409:1000` is now a non-present device that Windows names **"uEye boot"**. On the same hub port (instance suffix `7&8F87787&0&2`), `1409:1465` is **OK** as **"uEye UI-146xLE Series"**.
     - So PID `1000` is the camera's boot state. The `ueye_usb` driver loads the camera's operating firmware into its RAM, and the camera then reappears with its model PID. That answers the "Unknown" item below: **Verified**. This is a volatile RAM load, repeated at every power-up. It writes no flash and is how the camera ran under PearlScope's uEye driver.
     - The model family is now **Verified** by PnP (UI-146xLE, the colour sensor is not yet confirmed by the SDK). Gate 1's colour role must match `1409:1465`, not `1000`.
     - Driver: service `ueye_usb`, version 4.97.1477.0 (`oem16.inf`, IDS Imaging Development Systems GmbH), file `ueye_usb_64.sys` 4.97.1476.
     - Classic API: `C:\Windows\System32\ueye_api_64.dll` **4.97.4111**, also in `C:\Program Files\IDS\ids_peak\ueye\develop\bin\`. The colour adapter can use the classic uEye API through ctypes, as planned.
     - Also installed: IDS peak comfort/generic SDKs, and the GenTL producers for uEye, U3V and GEV.
     - **Unwanted extra:** the GigE uEye network driver `ueye_eth` (4.97.1543, "uEye ETH … kernel driver") is installed, starts with the system, and is **bound to the Ethernet and WiFi adapters** ("Gigabit Ethernet uEye"). The installer gave no option to leave it out, or it was not unticked.
       - It is not needed for the USB camera.
       - Removing the binding is a network setting change, so it is left to the user.
     - No IDS processes or user-mode services are running.
   - **Firmware safety for the colour gates:**
     - The uEye API can upload "starter firmware" to some cameras, but only when the caller passes `IS_ALLOW_STARTER_FW_UPLOAD` to `is_InitCamera`. Gate 1 must never pass that flag, and must stop on `IS_STARTER_FW_UPLOAD_NEEDED`.
     - The installer must not be asked to update camera firmware.
     - Whether the USB 2 driver itself loads firmware into the camera's RAM on enumeration is **Unknown**. After the driver binds, re-check PnP. A changed PID or REV would show a volatile RAM load (not a flash write); record it before Gate 1.
3. The LED board was in its **bootloader** (`0DAD:0057`) at this scan, i.e. power-cycled since the last StartApp. With the bootloader running the LEDs are **Probable** not driven: consistent with the camera gate's "LED off" precondition, but the gate still asks the operator to confirm it.
