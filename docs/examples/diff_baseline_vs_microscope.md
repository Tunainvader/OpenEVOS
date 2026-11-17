> **SYNTHETIC EXAMPLE** generated from test fixtures. VID/PIDs 1234:5678 and ABCD:0001 are fake.

# OpenEVOS snapshot diff

- **Baseline:** `baseline` (baseline-20261111T222225Z-ef57c1, 2026-11-11 22:22)
- **Compared:** `microscope` (microscope-20261111T222225Z-239965, 2026-11-11 22:22)
- **Recovered-image scan:** recovered-image-20261111T222225Z-ea01aa

> Devices listed as *added* are **Detected** only. Their role (camera, stage controller, …) is **Unknown** until further evidence maps them.

## Physical devices that appeared

| Physical device | VID:PID | Interfaces |
|---|---|---|
| USB\VID_05E3&PID_0610\5&1A2B3C4D&0&4 | 05E3:0610 | — |
| USB\VID_1234&PID_5678\6&AAAA&0&1 | 1234:5678 | USB\VID_1234&PID_5678&MI_00\7&BBBB&0&0000, USB\VID_1234&PID_5678&MI_01\7&BBBB&0&0001 |
| USB\VID_ABCD&PID_0001\SN0001TEST | ABCD:0001 | — |

## Added devices and interfaces

| VID:PID | MI | Name | Bus-reported | Class | Driver / service | COM | Seen before | Candidate role (vendor-software evidence) | Recovered INF match |
|---|---|---|---|---|---|---|---|---|---|
| 05E3:0610 | — | Generic USB Hub | — | USB | USBHUB3 | — | no | — | — |
| 1234:5678 | 00 | Synthetic Imaging Interface | — | Image | oem12.inf (Synthetic Vendor) | — | no | — | <mounted-image>/img/Windows/INF/oem12.inf [Life Technologies] |
| 1234:5678 | 01 | USB Device | — | — | — | — | no | — | <mounted-image>/img/Windows/INF/oem12.inf [Life Technologies] |
| 1234:5678 | — | USB Composite Device | Synthetic Test Camera | USB | usbccgp | — | no | — | <mounted-image>/img/Windows/INF/oem12.inf [Life Technologies] |
| ABCD:0001 | — | Unknown device | Synthetic Controller | — | — | COM7 | no | — | — |

## Removed devices

_None._

## Changed devices

_None._

## Serial ports

| Change | Port | Description | VID:PID |
|---|---|---|---|
| added | COM7 | Synthetic USB Serial (COM7) | ABCD:0001 |
