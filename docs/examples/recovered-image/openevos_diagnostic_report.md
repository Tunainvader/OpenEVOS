> **SYNTHETIC EXAMPLE** generated from test fixtures. VID/PIDs 1234:5678 and ABCD:0001 are fake.

# OpenEVOS diagnostic report

- **Snapshot label:** `recovered-image`
- **Scan ID:** `recovered-image-20261111T222225Z-ea01aa`
- **Started / finished (UTC):** 2026-11-11 22:22:25 / 2026-11-11 22:22:25
- **Tool / schema:** 0.1.0.dev0 / 0.1.0
- **Host:** Linux 6.18.44-fc-v378 (x86_64); Python 3.13.18; elevated: True; redacted: False
- **Artifact roots:** <mounted-image>/img

> This report was produced by a read-only scan. No device was opened, no serial port or USB endpoint was written to, and nothing was changed on this system. Labels follow the OpenEVOS evidence policy: *Verified* = read directly from Windows or a file; *Probable / Strongly supported* = inference with alternatives; *Unknown / Not tested* = no basis yet.

## 1. Summary

| Item | Count |
|---|---|
| USB devices/interfaces present | 4 |
| USB devices remembered but not attached | 1 |
| Present devices with a Windows problem code | 0 |
| Serial (COM) ports | 1 |
| Third-party driver packages (oem*.inf) | 1 |
| EVOS-related files | 4 |
| Camera SDK / USB-bridge library matches | 2 |
| Micro-Manager installations | 1 |
| Findings — Verified | 11 |
| Findings — Strongly supported | 0 |
| Findings — Probable | 4 |
| Findings — Unknown | 0 |
| Findings — Not tested | 7 |
| Findings — Not applicable | 0 |

## 2. What Windows directly reported (Verified)

### Host system

| Field | Value |
|---|---|
| Computer | LABPC-EVOS01 |
| Manufacturer / model | Dell Inc. / OptiPlex 7090 |
| OS | Microsoft Windows 10 Pro 10.0.19045 (build 19045, 64-bit) |
| BIOS | Dell Inc. 1.20.0 (2023-05-10) |
| Baseboard | Dell Inc. 0XXXXX |
| RAM (GiB) | 16.0 |
| CPU | Intel(R) Core(TM) i5-10505 CPU @ 3.20GHz |
| USB host controllers | Intel(R) USB 3.1 eXtensible Host Controller |
| .NET Framework 4 release | 528372 |

### USB topology (present devices)

```text
- VEN_8086&DEV_A36D&SUBSYS_00000000&REV_10  Intel(R) USB 3.1 eXtensible Host Controller  (USBXHCI)
  - ROOT_HUB30  USB Root Hub (USB 3.0)  (USBHUB3)
    - 0403:6001  USB Serial Converter "FT232R USB UART"  (FTDIBUS)
      - 0403:6001  USB Serial Port (COM3)  (FTSER2K)
    - 046D:C52B  USB Composite Device "USB Receiver"  (usbccgp)
      - 046D:C52B MI_00  USB Input Device  (HidUsb)
```

### Devices with a Windows problem code

_None._

### Serial ports (listed only — never opened)

| Port | Description | VID:PID | Manufacturer | Evidence |
|---|---|---|---|---|
| COM3 | USB Serial Port (COM3) | 0403:6001 | FTDI | `com-6b8aaefda6` |

### USB devices Windows remembers but are not attached now

| VID:PID | Name | Bus-reported description | Last arrival | Instance ID |
|---|---|---|---|---|
| 1D6B:ABCD | Unknown USB Device | — | 2025-03-04T10:11:12.0000000Z | USB\VID_1D6B&PID_ABCD\OLDSERIAL99 |

## 3. What drivers and files identify

### Drivers bound to USB devices

| Device | INF | Provider | Version | Signed | Evidence |
|---|---|---|---|---|---|
| USB Composite Device | usb.inf | Microsoft | 10.0.19041.1 | True | `drv-293f941195` |
| USB Serial Converter | oem3.inf | FTDI | 2.12.36.4 | True | `drv-8086b5c710` |

### Third-party driver packages (oem*.inf)

| INF | Windows volume | Provider | Class | DriverVer | USB IDs | Keyword hits |
|---|---|---|---|---|---|---|
| <mounted-image>/img/Windows/INF/oem12.inf | <mounted-image>/img | Life Technologies | Image | 03/01/2013,1.0.0.0 | 1234:5678, 1234:9999 | evos, life technologies |

_1 package(s) come from a recovered/mounted Windows volume._

### EVOS-related files (4)

| Path | Size | Bitness | Product / version | SHA-256 | Hits |
|---|---|---|---|---|---|
| <mounted-image>/img/Program Files (x86)/EVOS FL Auto/CamBridge.dll | 1024 | 32 | — | d77d8da47a9903b0 | evos |
| <mounted-image>/img/Program Files (x86)/EVOS FL Auto/EvosApp.exe | 1024 | 32 | — | 0926917ea38efc08 | evos |
| <mounted-image>/img/Program Files (x86)/EVOS FL Auto/FlyCapture2.dll | 1024 | 32 | — | d77d8da47a9903b0 | evos |
| <mounted-image>/img/Program Files (x86)/EVOS FL Auto/calibration.xml | 6 | — | — | b961d672cc30c3a8 | evos |

### Camera SDK / USB-bridge libraries (filename heuristics)

| Vendor / SDK | Path | Label | Bitness | Basis |
|---|---|---|---|---|
| FTDI / D2XX | <mounted-image>/img/Windows/System32/ftd2xx.dll | Probable | 64 | filename matches /^ftd2xx(64)?\.dll$/ |
| Point Grey / FLIR / FlyCapture2 | <mounted-image>/img/Program Files (x86)/EVOS FL Auto/FlyCapture2.dll | Probable | 32 | filename matches /^flycapture2(_c)?(_v\d+)?\.dll$/ |

### Micro-Manager

| Path | Adapters |
|---|---|
| <mounted-image>/img/Program Files/Micro-Manager-2.0 | DemoCamera, PointGrey |

## 4. What is inferred (not verified)

| Label | Conf. | Claim | Alternatives | Evidence |
|---|---|---|---|---|
| Probable | 0.6 | Third-party driver package <mounted-image>/img/Windows/INF/oem12.inf (provider: Life Technologies) may belong to the EVOS installation (matches: evos, life technologies). | driver for other equipment from the same vendor; keyword coincidence | `inf-4977f5e31a` |
| Probable | 0.55 | File appears to belong to FTDI D2XX (filename matches /^ftd2xx(64)?\.dll$/). | same-named file from unrelated software; SDK installed for other hardware | `sdk-1d09f419ae` |
| Probable | 0.55 | File appears to belong to Point Grey / FLIR FlyCapture2 (filename matches /^flycapture2(_c)?(_v\d+)?\.dll$/). | same-named file from unrelated software; SDK installed for other hardware | `sdk-8f40660efa` |
| Probable | 0.5 | Installed program 'Synthetic EVOS Test Suite' may relate to the EVOS installation (matches: evos, life technologies). | unrelated product from the same vendor; keyword coincidence | `prog-f6b26a190f` |

## 5. What remains unknown — documented EVOS FL Auto / AMC1000 profile

| Component | Label | Status |
|---|---|---|
| camera_mono_ccd | Not tested | High-sensitivity monochrome interline CCD camera (fluorescence; also transmitted light): not tested in this snapshot (needs a 'microscope' snapshot and diff). |
| camera_color_cmos | Not tested | High-sensitivity color CMOS camera (brightfield / phase contrast): not tested in this snapshot (needs a 'microscope' snapshot and diff). |
| xy_stage | Not tested | Automated XY scanning stage controller: not tested in this snapshot (needs a 'microscope' snapshot and diff). |
| objective_turret | Not tested | Five-position automated objective turret controller: not tested in this snapshot (needs a 'microscope' snapshot and diff). |
| focus | Not tested | Focus / autofocus mechanism (whether separately controllable is unknown): not tested in this snapshot (needs a 'microscope' snapshot and diff). |
| illumination | Not tested | LED illumination and up to four fluorescence light cubes: not tested in this snapshot (needs a 'microscope' snapshot and diff). |
| amc1000_controller | Not tested | Onstage Incubator (AMC1000) control unit, separate USB connection: not tested in this snapshot (needs a 'amc1000' snapshot and diff). |

## 6. Next read-only steps that would reduce uncertainty

- Pass this scan as `diff --recovered <this scan>` when comparing baseline and microscope snapshots, so new VID:PIDs are matched against the original EVOS driver packages.
- A 32-bit SDK was found. Record its exact version; plan a 32-bit helper process before any integration (ADR-0002).
- Photograph internal/rear controller labels where accessible without disassembly; record them as operator evidence.

## 7. Actions not performed

- No device was opened through a driver, SDK or Micro-Manager.
- No serial port was opened; no data was sent to any COM port or USB endpoint.
- No driver was installed, removed, enabled, disabled or updated.
- No registry value, service, file, firmware or configuration was modified.
- No filesystem check or repair (e.g. CHKDSK) was run; no source disk or image was written.
- No file was copied out of an EVOS installation; only metadata and hashes were recorded.
- No data was sent to any AI provider or network service.

## 8. Collector runs

| Collector | Status | Reason | Duration (s) | Evidence items |
|---|---|---|---|---|
| system | ok | — | 0.00 | 1 |
| windows_devices | ok | — | 0.00 | 8 |
| serial_ports | ok | — | 0.00 | 1 |
| drivers | ok | — | 0.00 | 3 |
| driver_packages | ok | — | 0.00 | 1 |
| installed_software | ok | — | 0.00 | 4 |
| evos_artifacts | ok | — | 0.04 | 7 |
| camera_sdks | ok | — | 0.02 | 3 |
| event_logs | ok | — | 0.00 | 2 |

### Notes

- Running elevated is not required for v0.1; prefer a standard user session.

---
Every row above cites evidence IDs that resolve in `evidence_register.json`.
