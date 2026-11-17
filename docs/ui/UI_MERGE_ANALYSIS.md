# New desktop UI: analysis of the v0.4.0 line and a merge plan

- **Date:** 2026-11-13. **Status:** phases 1–4 **done** (ADR-0027); phase 5 (instrument run) pending the instrument. Phase 1 log: `MERGE_PHASE1_LOG.txt`.
- **Inputs:**
  - `OpenEVOS-v0.4.0-ui.zip`: a complete OpenEVOS repository, `0.4.0.dev0`;
  - `OpenEVOS-UI-BuildKit-v0.4.0.zip` and `BUILD_INSTRUCTIONS.md`: design tokens, QSS, fonts, icons, screenshots, the acceptance checklist and menu specs;
  - the EVOS FL Auto user guide (MAN0007986), used through the kit's page mapping. The PDF could not be text-extracted in this environment.

## 1. What the v0.4.0 line is

A **parallel line of OpenEVOS** that forked from an earlier core than this repository's. It adds a complete, manual-parity desktop application:

| Layer | Packages (new in v0.4.0) | Content |
|---|---|---|
| Qt-free workflows | `instrument/` | profile, vessels, settings, readiness |
| | `acquisition/` | controller, capture, autofocus, scan, time-lapse, calibration, session |
| | `imaging/` | display, composite, save, OME-TIFF, focus metrics |
| | `analysis/` | count, annotations |
| Qt UI | `ui/shell.py`, `ui/pages/*`, `ui/widgets/*`, `tokens`/`theme`/`icons`, `session_model` | Craftimon "Lab" design system; Capture, Scan, Time-lapse, Z-stack, Movies, Save, Review, Analysis, Hardware, Environment, Calibration, Settings, Service, Event log |
| Simulator | `backends/simulation/rig.py`, `devices.py`, a richer `optics.py` | deterministic culture in vessel regions; parfocal and parcentric offsets; hot pixels; LED × exposure × gain |

**Design rules match this repository's:**
- the UI never writes devices;
- every action is a typed command through `CommandRouter.submit()`, or a workflow on a worker thread;
- exposures reserve the camera exclusively and hold stage, focus, turret and illumination shared;
- honest SIMULATED labels;
- raw data is never changed by display.

It **lacks everything this repository built for the real instrument:**
- `safety/firmware.py`;
- the `CoreGateway` (audit trail, command catalog, actor policy: "AI may not command");
- the HAL adapter and `Instrument` assembly;
- every maintenance tool (LED gates, Motor Gates 1–7);
- the Technosoft telemetry/control backend (ADR-0023, ADR-0024);
- ADR-0009 to ADR-0024.

## 2. Compatibility with this repository's core (checked by AST, 2026-11-13)

- **Names:** the v0.4.0 workflow and UI layers import only names that this repository's `core`/`safety` already provide. `core.models` here is a superset; v0.4.0 has no model class this repository lacks. The one missing import is the simulator rig (`attach_optical_simulator`).
- **The controller is profile-driven.** `InstrumentController` addresses devices as `profile.stage_device`, `focus_device`, `turret_device` and so on, and uses only `router.submit`, `reserve` and `release`, all present here. **Pointing `stage_device` at `motor.stage` drives the real X/Y stage through this repository's safety checks**, which already accept it (ADR-0024).
- **Real differences to reconcile:**

  | Item | v0.4.0 | Here | Proposal |
  |---|---|---|---|
  | Camera gain | linear `gain` 1–64 (EVOS shows "gain 1×") | `gain_db` | adopt linear gain, matching the EVOS UI; migrate the simulators and the catalog |
  | Router Stop all | `cancel_all()` (cancel, no latch) | `stop_all()` (latch + halt every device) | keep the latching STOP (it halts the physical stage); add `cancel_all()` for workflow cancel |
  | Non-motion verification | `verify_result(command)` backend hook | not present | add (small, additive) |
  | Validator | older, 110 lines | stricter, 206 lines: provenance, firmware quarantine, confirmation, interlocks | keep ours; test that v0.4.0 workflows pass it in simulation |
  | Audit / actor policy | none in the controller | `CoreGateway` audits every command; AI may not command | route `InstrumentController` through an audit hook (no behaviour change) |
  | ADR numbers | 0005–0008 mean different decisions | 0001–0024 | renumber v0.4.0's "UI manual parity" and "Craftimon design system" to ADR-0025/0026 |

- **Physical readiness** (`instrument/readiness.py`) is hand-written from a bring-up log and **over-claims**: Z, the F carriage and the LED board are "Verified (limited)", but no Z/F motion or light command has been tested. It should be **generated from gate evidence**:
  - X/Y: moves verified (Gates 4–6);
  - Z/F: telemetry only;
  - LED: StartApp and calibration reads only;
  - O/P: locked.
- **Not verified here:** v0.4.0's own test suite. Running code from the zip was blocked by the environment's safety policy. Its tests become runnable once its sources are brought into this repository on purpose.

## 3. Recommended path: port v0.4.0's workflow and UI layers onto this repository's core

Not a rebuild from the kit: the v0.4.0 code *is* the kit's reference implementation (the kit was generated from it by `tools/make_build_kit.py`). Not a switch to the v0.4.0 repository either: that would drop the core and all the hardware work.

| Phase | Work | Done when |
|---|---|---|
| 1. Import | Copy `instrument/`, `acquisition/`, `imaging/`, `analysis/`, the new `ui/` (as `openevos.ui`, with today's UI kept as `openevos.ui_legacy` until parity), the rig simulator, fonts, icons, `docs/ui`, tools and tests. Renumber ADRs | the tree builds; the import and firmware guards pass |
| 2. Core reconcile | Linear gain; `cancel_all()`; `verify_result`; audit hook in the controller; run v0.4.0's tests against this core and fix the breakages | all tests (both lines) pass; ruff clean |
| 3. Physical seam | Profile options: `--motor-telemetry` and `--motor-control` map `stage_device` to `motor.stage`; the joystick, vessel map and jogs drive the real stage inside the safety-checked box. Hardware page: the core table with the read-only motor rows, plus a readiness table **generated from gate evidence** | simulated drive chain end-to-end through the new UI |
| 4. Retire | Fold `ui_legacy`'s Devices/Audit and Diagnostics (Inspector scans) into Hardware/Service/Event log; remove `ui_legacy` and, if unused, the HAL adapter path | one UI; one simulator |
| 5. Instrument | Run the new UI on the unit (telemetry, then 0.1 mm jogs), then Gate 7 (calibrated X/Y limits) | real-stage checks recorded |

## 4. Risks
- **Code from outside this repository** (the zip) is unverified until phase 2 runs its tests.
- **Two simulators** (HAL-adapter based here, rig based there) during phases 1–3; phase 4 removes one.
- **The physical stage in the new UI**: the vessel map and jogs can request large moves. This repository's 0–22 mm box and the drive clamp stay the limits, and every move still passes the SafetyManager.
- **The jog joystick repeats moves every 180 ms.** On the real stage this needs "skip while a move is in flight", which v0.4.0 already does, plus a verified stop on release (to add).
