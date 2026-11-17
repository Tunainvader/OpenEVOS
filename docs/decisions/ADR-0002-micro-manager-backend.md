# ADR-0002: Micro-Manager (MMCore) as the preferred backend — conditional on evidence

- **Status:** Proposed — **decision deferred** until Inspector evidence exists (plan Phases 1 and 3)
- **Date:** 2026-11-11

## Context

MMCore offers a mature device model and many adapters. Whether it suits the EVOS FL Auto depends on facts we do not have yet: the camera vendor and model, the SDK's version and **bitness**, and how the stage, turret and illumination are controlled. No EVOS adapter is known to exist. If the original installation turns out to use a 32-bit SDK, that would make an in-process 64-bit Python + `pymmcore` path impossible without a helper process.

## Decision (what is fixed now)

- The HAL is backend-agnostic. `MicroManager*Backend`, `VendorSDK*Backend`, native and simulated backends all implement the same interfaces.
- **Not decided yet:** `pymmcore` vs Pycro-Manager, a custom C++ adapter vs a direct vendor-SDK backend, and the OME-TIFF writer.

## Decision criteria (to be evaluated with Inspector output)

| Question | Evidence source | Leans towards |
|---|---|---|
| Does an existing `mmgr_dal_*` adapter match the camera vendor? | `software_inventory.json` → `micromanager[].adapters`, SDK matches | MMCore |
| Is the camera SDK 32-bit only? | `R-PE-32BIT` findings on SDK DLLs | 32-bit helper process (MM or direct) |
| Are stage/turret/LED controlled through a vendor DLL or through a USB-serial bridge? | INF packages, `usb_bridge` SDK matches, diff of the `microscope` snapshot | Decide per subsystem |
| Is the AMC1000 a separate USB device? | `amc1000` snapshot diff | Separate `AMC1000Backend` (already planned) |

## Evidence update (2026-11-12, vendor software analysis)

From static analysis of the FL Auto 1.6 and FL Auto 2 packages (docs/evidence/vendor_software_analysis.md). These findings come from the vendor software and have not yet been observed on our unit:

| Question | Finding | Leaning |
|---|---|---|
| Camera adapters? | Mono = Point Grey Chameleon USB 2.0 (FlyCapture2); color = IDS uEye UI-146xLE-C. Micro-Manager has a *Point Grey Research* (FlyCapture2) adapter and an *IDS_uEye* adapter. | **MMCore for cameras** (or libdc1394 for the mono camera) |
| 32-bit SDK? | All FL1 1.6 hardware DLLs are x64. | No helper process needed for v1.6 |
| Stage/turret/LED control? | Technosoft drives through TML over a serial COM port (6 axes); LED board = USB HID-class device 0DAD:0051. | **Native backends** for motion and illumination |
| AMC1000? | CN control board, USB HID 0DAD:0058; same interface as FL Auto 2. | Separate `AMC1000Backend` (confirmed direction) |

## Evidence update (2026-11-12, Micro-Manager check on the replacement PC)

Run on the replacement PC (Windows 11, Python 3.12) in an isolated venv; only Micro-Manager's own test adapters were loaded.

| Check | Result | Label |
|---|---|---|
| `pymmcore` 12.5.1.75.0 + `pymmcore-plus` 0.18.1 install and import | OK (device interface 75) | Verified |
| Download of matching test adapters (`Micro-Manager-75.20261104`: DemoCamera, Utilities, SequenceTester) | OK once Python used the Windows certificate verifier (`truststore`). Python's own store failed `CERTIFICATE_VERIFY_FAILED`. | Verified |
| Load `mmgr_dal_DemoCamera.dll` | First attempt failed: the adapters import `msvcp140.dll`, `vcruntime140.dll` and `vcruntime140_1.dll`, and the Microsoft Visual C++ 2015–2022 x64 runtime was not installed. After installing it (14.52.37029.0, with owner approval), the DemoCamera config worked: snapped a 512×512 uint16 frame in 24 ms, moved XY and Z, switched objective presets, ran a 10-frame sequence. | Verified |
| Full Micro-Manager 2.0.3 nightly 20261106 (Windows x64) | Installed with the installer's silent mode. It contains 266 `mmgr_dal_*` adapters, **including `mmgr_dal_PointGrey.dll` and `mmgr_dal_IDS_uEye.dll`**. There is no Technosoft adapter. The installer is not Authenticode-signed; it came from `download.micro-manager.org` over HTTPS. | Verified |
| Point Grey adapter | Imports **`FlyCapture2_v140.dll`** (FlyCapture2 built with VS2015). The original PearlScope used `FlyCapture2_v100.dll`, so we need a *later* FlyCapture2 SDK plus its `pgrusbcam` USB 2.0 driver, not the copy on the old disk. Whether that SDK enumerates the Chameleon on Windows 11 is untested. | Verified (dependency); Unknown (works on Win 11) |
| IDS_uEye adapter | Imports **`ueye_api_64.dll`** (IDS uEye driver / Software Suite). Wiki: BSD, "Requires drivers from IDS (version 4.30 or higher)", first camera only. | Verified (dependency); Unknown (works with UI-146xLE-C on Win 11) |
| Spinnaker / IDS peak as alternatives | Spinnaker's vendor page lists USB3/GigE cameras; IDS peak's adapter page covers "IDS Peak USB cameras" and does not mention uEye. Neither is a fit for these USB 2.0 cameras. | Probable |

Deployment notes from this check:
- **Python's certificate store.** Python's bundled store could not verify the download sites on this PC; `truststore` (Windows' own verifier) fixed it. The `latest/` redirect on download.micro-manager.org still failed from Python with `WRONG_VERSION_NUMBER`, so the installer was fetched with Windows' HTTP stack and matched by size to nightly 20261106.
- **App-container redirection.** Installs made from inside the Claude desktop app under `%LOCALAPPDATA%` are redirected into the app's container (`AppData\Local\Packages\Claude_…\LocalCache\Local\…`). Micro-Manager was therefore reinstalled, elevated with owner approval, to **`C:\Program Files\Micro-Manager-2.0.3_20261106`**. That location is not redirected, and the demo snap works from it. The redirected copy was removed. Point tools at it explicitly (`setDeviceAdapterSearchPaths`, or the `MICROMANAGER_PATH` environment variable for pymmcore-plus).

Next: obtain FlyCapture2 (a release that ships `FlyCapture2_v140.dll`) and the IDS uEye driver from the vendors. Both need vendor accounts. Then, with the microscope connected, check that each vendor tool sees its camera before loading the Micro-Manager adapter.

## Consequences

The ADR is revisited once a baseline + microscope + recovered-image scan set exists. Until then, nothing in `openevos.inspector` imports Micro-Manager.
