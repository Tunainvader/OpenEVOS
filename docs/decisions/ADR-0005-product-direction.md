# ADR-0005: Product direction: FL Auto 2-class capabilities on the FL Auto 1 hardware we own

- **Status:** Proposed
- **Date:** 2026-11-12
- **Related:** ADR-0001 (standalone UI), ADR-0002 (backends), ADR-0003 (AI evidence boundary), `docs/evidence/vendor_software_analysis.md`

## Goal

> "The end goal is not to turn FL Auto 1 into FL Auto 2. It is to build a modern, independent application that brings the best software capabilities of the newer generation to the hardware we actually own." (project owner, 2026-11-12)

## Context

Static analysis of both vendor packages (hashes checked against the analysis on 2026-11-12) shows that FL Auto 2's code cannot drive our AMAFD1000:
- its cameras are USB3, driven through Spinnaker;
- its motion and LED control go through a different board ("M4L2");
- it has no Technosoft or uEye support.

The incubator is the exception: both generations use the same CN control-board component and the same USB ID (`0DAD:0058`).

FL Auto 2's documented workflows define the bar we want to reach: live imaging, scan maps, time-lapse, Z-stacks, autofocus, calibration and analysis.

## Decision

1. **OpenEVOS targets the AMAFD1000 and AMC1000 as they are.** No hardware swap, and no emulation of FL Auto 2.
2. **FL Auto 2 is a reference, used in three ways only:**
   - *Features and workflows*, from public documentation and observed behaviour, re-implemented independently.
   - *Facts* (objective NA and magnification, vessel geometry), re-derived into our own data files. Thermo's files are not redistributed.
   - *Interface semantics* of the CN board, as the specification for an independently written `AMC1000Backend`.
3. **Never:**
   - copy, link or redistribute vendor code or binaries;
   - install FL Auto 2 software on the instrument PC;
   - run vendor firmware-update paths.
4. **Order of work:**
   - Capabilities with no hardware gate are built now, on the simulator.
   - Each hardware path waits behind the gate in its row below, and is promoted only when that gate passes (plan §8.1 stages, §13 modes).

## Capability map

Labels are for *our unit* and follow ADR-0003: nothing below is Verified on the instrument yet.

| FL Auto 2 capability | FL Auto 1 hardware path (label) | OpenEVOS piece | Gate before real hardware | Start now? |
|---|---|---|---|---|
| Live view, exposure, gain | Mono: Point Grey Chameleon USB 2.0, FlyCapture2 (Strongly supported). Colour: IDS uEye UI-146xLE-C (Probable). Both via Micro-Manager adapters, which ship in the Windows build (Verified, ADR-0002). | `Camera` HAL + MMCore backend | Microscope diff shows `1E10:2004/2005` and `1409:*`; vendor driver enumerates the camera in its own tool | UI and simulator: yes. MMCore DemoCamera: works now |
| Channels, LEDs, light cubes | LED board `0DAD:0051` (Strongly supported) + epi carriage axis F (Technosoft) | `Illumination` + carriage HAL, native backends | Protocol undocumented: passive observation of the original software first. Read the board's HW_DATA before any write. | Simulator: yes |
| Objective turret, phase turret, XY, Z | Technosoft drives, TML over a COM port, 6 axes; the host downloads `.sw` setups at start-up (Strongly supported) | Motion HAL + TML backend (Technosoft's own library preferred over clean-room) | `.sw` files and per-unit `PearlScope.ini` preserved; units, limits, homing and stop behaviour validated | Simulator: yes |
| Autofocus | Image-based, over Z + camera | Autofocus service | Z and camera validated | Algorithm on simulator: yes |
| Scan maps, multi-field, time-lapse, Z-stacks, stitching | Acquisition over validated XY/Z/camera | Acquisition engine (plan Phase 6) | Motion and camera validated | Planner + simulated runs: yes |
| Calibration: stage, objective profiles, camera rotation, parfocality, field alignment | Seed from per-unit `PearlScope.ini` and LED-board HW_DATA (read-only) | Calibration manager (OpenEVOS records kept separate from the originals, plan §5.2F) | Recovered HDD; HW_DATA read with a read-only command only | Data model: yes |
| Incubator: temperature, CO₂, O₂, gas, alarms | CN board, USB HID `0DAD:0058`, same component as FL Auto 2 (Strongly supported) | `AMC1000Backend` | AMC1000 diff; telemetry read-only first; any write gets a safety review (see below) | Specification from FL Auto 2 semantics: yes |
| Image analysis: histograms, cell count, confluence, measurements | None (software only) | `analysis` module | None | Yes |
| Experiment templates, metadata, OME-TIFF | None | `experiments`, `storage` | None | Yes |
| Diagnostics and AI assistant | Inspector evidence | `inspector`, `ai` | Provider adapter + redaction preview | Yes |

## Consequences surfaced by the vendor analysis

- **The original application is not passive.** At start-up it pushes per-axis drive setups (`.sw`) and incubator tuning (PID gains, heater limits), and it can reflash the CN and LED boards.
  - First establish, read-only, what state the boards hold *without* those pushes.
  - Replaying a vendor setup is a maintenance-mode action with operator confirmation (plan §13.1), never part of routine start-up.
  - OpenEVOS never flashes firmware.
- **Calibration lives in two places**, on the PC (`PearlScope.ini`) and on the LED board (HW_DATA). Read and preserve both before anything writes to either.
- **Deployment prerequisites on the replacement PC:**
  - Microsoft VC++ 2015–2022 x64 runtime: installed 2026-11-12. Every Micro-Manager adapter needs it.
  - Micro-Manager 2.0.3 nightly or later: it ships the `PointGrey` and `IDS_uEye` adapters.
  - FlyCapture2, a release that provides `FlyCapture2_v140.dll`, with the `pgrusbcam` driver. This is *newer* than the `FlyCapture2_v100.dll` the original PearlScope used.
  - IDS uEye driver ≥ 4.30, providing `ueye_api_64.dll`.

  Windows 11 support for the last two is Unknown.

## In progress (2026-11-12)

- Desktop application on the simulator (plan Phase 2), in a parallel session.
- Camera path proof: MMCore + DemoCamera works on this PC (snap, XY/Z moves, objective presets, sequence). The real camera adapters now wait only on the vendor SDKs and the connected microscope.
- Hardware discovery, waiting on the instrument:
  - microscope-connected scan + `diff`;
  - AMC1000-only scan + `diff`;
  - recovered-HDD scan with `--artifact-root`.

## Open questions

1. ~~Does the current Windows Micro-Manager build ship `mmgr_dal_PointGrey.dll` and `mmgr_dal_IDS_uEye.dll`?~~ Yes: nightly 2.0.3 20261106 ships both (Verified 2026-11-12, see ADR-0002).
2. Do FlyCapture2 and the IDS uEye driver install and enumerate these USB 2.0 cameras on Windows 11?
3. Do the Technosoft drives keep their setup across power cycles without the host's `.sw` push?
4. Which incubator parameters does the CN board store, and what does it do when no host is connected?
