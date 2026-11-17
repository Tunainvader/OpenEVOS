# OpenEVOS UI Integration Report

**Version:** OpenEVOS 0.3.0.dev0
**Date:** 2026-11-12
**Scope:** This report analyses the original EVOS FL Auto software through its user guide and reviews three
earlier OpenEVOS UI attempts. It then specifies the merged UI and records what is now built in the
repository.

> **Short version.** The original user guide is now the behaviour reference. The new UI (`openevos-ui`)
> reproduces the Capture tab control for control and adds the following workspaces: Save, Review,
> Analysis (Overlay / Measure / Count / Auto Count), Scan, Time-lapse, Z-Stack, Movies, Settings
> (Mono / Color / Basic / Vessel Manager), Hardware, Environment, Calibration, Service and an event log.
> Everything runs through the Phase 2.1 command router against a new optics-aware simulator. Nothing
> pretends to be hardware: simulated devices say SIMULATED, measured environment values say Unavailable,
> and anything the bring-up record marks unsafe is shown as locked or planned.
> Tests: 229 passed, 6 skipped (Windows-only). `ruff` is clean.

---

## 1. Sources analysed

| # | Source | What it is | How it was used |
|---|---|---|---|
| S1 | *EVOS® FL Auto Imaging System User Guide*, MAN0007986 Rev C.0 (86 pp.) | Vendor manual for the original instrument and software | **Behaviour reference.** Full control inventory with page numbers (§2), rules (§3) |
| S2 | Gemini PySide6 reference implementation + blueprint text | An earlier AI attempt: one-file Qt app with a "router" | Layout ideas kept; architecture and invented values rejected (§4.1) |
| S3 | *OpenEVOS Product UI Design Blueprint v3.0* (MD + 16 concept PNGs) | Product/visual blueprint | Visual language, navigation groups, workspace list (§4.2) |
| S4 | *OpenEVOS UI Product Spec Manual Integrated v2.0* (74 pp.) | Spec that maps the manual to OpenEVOS | Data-model and acceptance ideas, mainly §18 (§4.3) |
| S5 | Operator bring-up log of the project unit (previous turn) | Hardware state of *this* EVOS | Physical readiness and gating (§5) |

Page numbers below are the **printed** page numbers of S1. The PDF page number is the printed page + 2.

---

## 2. The original software: complete control inventory (S1)

The original software has four top tabs: **IMAGE, SCAN, TIME LAPSE, SYSTEM**. IMAGE has the sub-tabs
**Capture, Edit (Overlay / Measure / Count / Auto Count), Save, Review, Settings (Mono / Color)**. The
Capture tab is the start-up screen (p.6-7).

### 2.1 Image > Capture (p.7-9)

| Control | Original behaviour | OpenEVOS implementation |
|---|---|---|
| Vessel Expert | Category + Type wizard. **A vessel must be selected before imaging** (p.8) | `VesselExpertDialog`; 12 built-in vessels in 5 categories (nominal SLAS geometry, flagged *not measured on this unit*) |
| Virtual vessel | Drag the crosshair to a location; click a well to move to its centre (p.8) | `VesselMap`: drag = move to drop point; click a well = centre of the well. Also draws the FOV rectangle and the reachable stage envelope |
| Medium Jog | Moves the stage "at an intermediate pace" while viewing (p.8) | Orange joystick knob; deflection sets the step size in fractions of the FOV (Slow/Medium/Fast); each step is a verified relative move, coalesced so motion never queues up |
| Objective buttons | 5 positions, labels from the Service-tab objective profiles (p.8, p.43) | 5 round buttons labelled from the instrument profile; empty slot disabled; tooltip shows label, NA and provenance |
| Power toggle | LED of the selected channel on/off (p.8) | `POWER ON/OFF` → `SetIllumination(enabled)`. A capture always lights the sample, then restores a dark live view |
| Light intensity slider | One combined control (p.7) | `LIGHT` slider. **OpenEVOS mapping** walks LED 2→100 %, then exposure, then gain (documented in `settings.py`; not the vendor algorithm) |
| Light channel buttons + buffer thumbnails | Selected channel shown in blue; captured images appear as thumbnails under the buttons (p.8) | `ChannelButton` (blue when selected) with thumbnail. Disabled with a reason when the active camera cannot image that channel |
| Light and Camera dialog | Mono camera (fluorescence + brightfield) or Color camera (brightfield only); Apply Pseudo Color; **Actual Mode** (LED, gain, exposure set separately); Clear All Channels (p.8-9) | `LightCameraDialog`, all four functions. Colour camera = brightfield/phase only, enforced in the profile (`CameraProfile.supports`) and in `CaptureService.check_capture` |
| Snap to Last | Returns focus to the Z of the last captured image (p.9) | `CaptureService.snap_to_last()`; LAST marker drawn on the Quick and Coarse sliders |
| Quick slider | Small focus range centred on the vessel's **Focus Nominal** (p.9) | ±150 µm around `vessel.focus_nominal_um` |
| Find Sample / Auto Fine | Automatic coarse / fine focus (p.9) | `Autofocus.find_sample` (17-point sweep over the vessel's range, then a 7-point refinement) and `auto_fine` (11 points over ~8 depths of field × the Auto Fine range %). Reports "no sample found" on a flat curve; refuses to run with the light off |
| Coarse / Fine sliders | Manual focus (p.9) | Coarse = full Z range; Fine = ±25 µm around the position at press, re-centres on release |
| Capture | Captures the current channel | `CAPTURE` button (scientific acquisition, §6.4) |
| Hot buttons (4) | User-programmable, separate for Mono and Color (p.7, p.19-21) | Four buttons from settings; per-camera assignments |
| Hot-button actions | Capture, Capture All, Transfection, Save, Z-Stack, Capture Focus Nominal, Quick Save (p.21) | All 7 implemented (`HotButtonAction`) |

### 2.2 Image > Save (p.10)

The Save tab has a folder browser and a file name. **Save Options** are Default, Quantitative Monochrome
or Custom. **Bit Depth** is 8 or 16. **File Type** is JPEG, BMP, TIFF or PNG. **Save As** is Color or
Monochrome. There are also Include Image Annotations and Save Underlying Channels.

The rules (p.10):

- Default = 8-bit colour TIFF with annotations.
- 16-bit needs TIFF with Color deselected.
- JPEG, PNG and anything saved as colour are 8-bit only.

**OpenEVOS:** the Save workspace has all of these, plus a live preview and Quick Save. Combinations that
break the rules are *disabled and explained*, never silently converted (`imaging/save.py: validate()`).
Every file gets a JSON sidecar. Simulated frames are labelled SIMULATED in the sidecar and in the TIFF
description.

### 2.3 Image > Edit (p.11-17)

| Tab | Original | OpenEVOS status |
|---|---|---|
| Overlay (p.11) | Channel ON/OFF; per-channel brightness/contrast; Reset All; Clear Channel | Channel ON/OFF overlay done. Per-channel brightness/contrast sliders: **planned** (auto range is used now) |
| Measure (p.12-13) | Annotation tools: text, arrow, histogram, rectangle, ellipse, line; **up to 12 ROIs**; histogram/intensity plots; units; Export All (CSV) | Two-point distance in µm (pixel size from objective + camera). Full annotation tool set, 12 ROIs and histograms: **planned** |
| Count (p.14-15) | Manual tags with **up to 6 labels**, running tally with percentages, right-click deletes, optional grid | Done: 6 labels, left-click add, right-click remove, counts + % + total. Grid: planned |
| Auto Count (p.16-17) | Count / Count From File; Separate touching cells; Aggressive boundaries; Brightness / Area / Circularity sliders (toward 100 % = more sensitive); Show cells | Done, all controls. Mapping is documented in `analysis/count.py`; results table and CSV export added |

### 2.4 Image > Review (p.18)

The Review tab has a folder system, Rename, Delete, and **Reuse**. Reuse recalls vessel, XY, focus,
objective, light channel and light slider from an image file.

**OpenEVOS:** session buffers plus a saved-file browser with the metadata sidecar, Rename, and **Reuse**
(objective → XY → focus → channel/LED/exposure/gain, each a validated command). Delete is intentionally
not offered yet; users delete files in the OS.

### 2.5 Image > Settings (p.19-21)

**Mono settings:** four hot buttons, custom channel pseudo colours, highlight saturated pixels in red,
block condenser in fluorescence, underlying channel save (8-bit colour / 16-bit mono), Capture All
selection and Quick Save settings.

**Color settings:** four hot buttons; brightness, contrast, saturation and hue with Reset; highlight
saturated pixels; Quick Save.

**OpenEVOS:** all present (Settings workspace, Mono and Color tabs). Block condenser is stored, but its
tooltip says it needs condenser motion (the P axis is locked on the unit) and has no effect in the
simulator.

### 2.6 Scan (p.22-27)

- **Routines tab:** Create New Routine, folders, stored list, Run, Edit, Delete.
- **Wizard:**
  1. Name and vessel.
  2. Objective, camera, channels, and an option to save channels separately.
  3. Area: either a rectangle (select wells or slides, then position and size) or **two beacons**
     (top-left and bottom-right) on the live image.
  4. Find Sample and/or Auto Fine (either, both or neither).
  5. **Coverage**, then focus and lighting per channel, then the save folder, then Finish or Start.
- **Coverage options** shown on p.24: Full coverage, Four points, Outer region only, Center region only
  (with Percentage), Random (N images), Grid (rows × columns).
- **Edit, export and review:** scans are `.sti` files, exported at Low, Medium or High quality.

**OpenEVOS (Scan workspace):**

- Same wizard order on one page, plus live planning: tile overlay on the vessel, field count, FOV,
  estimated time and size, and warnings.
- All 6 coverage options; rectangle or exactly 2 beacons (enforced).
- Routines saved, recalled and deleted as JSON.
- Run, Pause and Stop between steps (never mid-exposure). Pause/Stop are checkpoints between tiles and
  channels.
- Downsampled mosaic per channel; optional TIFF per tile + `manifest.json`.
- `.sti` export: not applicable (open formats instead). OME-TIFF/Zarr: planned.

### 2.7 Time Lapse (p.28-32)

- **Wizard:**
  - Name, vessel and number of scenes.
  - Objective, camera, channels.
  - Auto find sample, Auto fine focus (each with a chosen channel), Z-Stack, Auto lighting.
  - **Beacons: up to 384** (vs 2 for Scan), Add / Update / Delete / Delete All, and **Populate Wells**
    (one beacon per well centre).
  - Options: video per beacon, watermark, save channels separately.
  - **Scenes:** length and interval; **Capture one frame only** (only with a single scene); environment
    per scene (temperature, humidity, atmosphere).
  - Save folder, then Finish or Start.
- **Run controls:** Start; **Pause** to adjust (on resume the adjustments apply to this run; the saved
  routine keeps its values); Show In Progress; Stop.

**OpenEVOS (Time-lapse workspace):**

- All of the above, except Auto lighting (shown disabled with its reason) and Z-stack per time point
  (planned).
- 384-beacon and single-scene rules are enforced by the model.
- Each beacon remembers its own focus during a run.
- The "latest time point" grid shows progress.
- Environment setpoints are recorded only; the routine says so in a warning because the incubator path
  is unavailable.

### 2.8 Movies (p.33-39)

Dual viewers, Time-lapse and Z-stack movie modes, Start/End markers, left/right/both panes, watermark,
frames-per-second slider, Make Stack, playback and save.

**OpenEVOS (Movies workspace):** sources are the last Z-stack or time-lapse (per beacon). Start/End,
watermark, fps, play, and export as a multi-page TIFF or animated GIF. Dual viewers: planned.

### 2.9 System (p.40-52)

**Basic tab:**

- Live expand/pinch gesture; scale bar; Getting Started button; time-lapse retract objective; reset scale
  bar.
- Capture Focus Nominal; Calibrate Vessel Alignment; Vessel Manager; Auto Fine Adjustment.
- Temperature offset; Calibrate Oxygen Sensor; Configure Gas Connections. The last two need the incubator.

**Network tab:** network shares.

**Service tab (p.43):**

- This EVOS Auto (serial, software revision), Update Software, Import Vessels, Error Log.
- **Microscope** (objective per turret position).
- **Calibrate Objective** (field of view, parfocality, parcentration, p.47-49, AMEP4720 slide).
- **FL Hot Pixel Correction** (stage covered).
- Date and Time, Move XY to Filter Cube Access Position (p.51), Move to Shipping Position (p.52).

**OpenEVOS:**

- **Basic** settings live in Settings.
- **Capture Focus Nominal** and **Auto Fine Adjustment** work (Calibration workspace + hot button).
- **Vessel Manager** saves the current vessel under a new name.
- The **Calibration** workspace lists every calibration with its manual page and an honest status. The
  three objective calibrations, vessel alignment and hot-pixel correction are *planned*; oxygen-sensor
  calibration is *unavailable*.
- The **Service** workspace shows every Service-tab function with a risk class (R0 read-only … R4
  irreversible):
  - Vendor software, firmware and EEPROM updates are **blocked by design**.
  - Filter-cube access and shipping moves are **blocked** until Gate 7 verifies travel.
  - The diagnostics export replaces Error Log.
- **Network:** not needed; the OS handles shares.

### 2.10 Onstage incubator, maintenance, specifications

- **Incubator (p.60-70).** Gas ports: Port 1 premix/air/custom, Port 2 N₂, Port 3 CO₂. O₂ sensor
  calibration takes ~4 min. Temperature offset.
  OpenEVOS: the **Environment** workspace stores this configuration and shows setpoints vs measured, with
  measured = "Unavailable" (no telemetry). Nothing is sent to the AMC1000.
- **Maintenance.** Light-cube change uses the filter-cube access position (p.51). Objectives can be
  changed (p.52). Both affect the profile; physical moves are blocked until verified.
- **Specifications.** Stage travel 115 × 78 mm; AMC1000 from ambient to 40 °C. Simulator values stay
  deliberately different (a synthetic ±50 mm envelope) so they can never be mistaken for EVOS limits.

---

## 3. Behaviour rules extracted from the manual, and where OpenEVOS enforces them

| Rule (manual page) | Enforced in | Test |
|---|---|---|
| A vessel must be selected before imaging (p.8) | A session always has a vessel; Vessel Expert changes it | `test_vessel_library_and_mapping` |
| Colour camera = brightfield/phase only (p.9) | `CameraProfile.supports`, `CaptureService.check_capture`, disabled channel buttons, scan/time-lapse validation | `test_color_camera_refuses_fluorescence`, `test_color_camera_disables_fluorescence_channels`, `test_scan_beacons_limits_and_camera_rules` |
| Max 4 hot buttons, 7 actions (p.21) | `MonoSettings/ColorSettings` validators | `test_hot_buttons_and_light_slider` |
| 16-bit only TIFF + monochrome; JPEG/PNG/colour = 8-bit; Default = 8-bit colour TIFF + annotations (p.10) | `imaging/save.py` `validate`, `allowed_bit_depths`, `SavePreset` | `test_save_rule_matrix`, `test_save_page_rules_and_quick_save` |
| Manual count ≤ 6 labels, tally with % (p.14) | `ManualCount` | `test_manual_count_labels`, `test_count_percentages_and_aggressive_boundaries` |
| Auto Count slider toward 100 % = more sensitive (p.17) | `slider_mapping` | `test_auto_count_known_disks_and_sensitivity` |
| Scan uses exactly 2 beacons (p.25) | `ScanRoutine` validator | `test_scan_beacons_limits_and_camera_rules` |
| Time-lapse ≤ 384 beacons; Populate Wells (p.30) | `TimelapseRoutine`, `populate_wells` | `test_timelapse_rules_and_estimate` |
| "Capture one frame only" needs a single scene (p.31) | `TimelapseRoutine` validator | `test_timelapse_rules_and_estimate` |
| Snap to Last = Z of last capture (p.9) | `CaptureService.last_capture_z` | `test_capture_metadata_and_buffers` |
| Clear All Channels clears the buffers (p.9) | `ImageBuffers.clear` | `test_capture_metadata_and_buffers` |
| Reuse recalls XY/focus/objective/channel/light (p.18) | `ReviewPage._reuse` (validated commands) | `test_review_reuse_recalls_position` |

---

## 4. Review of the three UI attempts

### 4.1 Gemini PySide6 reference (S2): keep / fix / reject

**Keep**
- Three-column Capture layout (instrument | image | light & focus).
- The colour-CMOS interlock idea.
- Dark workstation styling.

**Fix**
- Hot buttons were hard-coded → now per-camera, chosen from the manual's 7 actions.
- Channels and the Capture All set were hard-coded → now come from the instrument profile and settings.
- The objective turret was not gated → slots come from the profile, empty slots are disabled, and the
  physical turret is shown as locked.
- Save, Review, Settings and the wizards were missing → all implemented.

**Reject**
- A command router that always returned success, and the UI writing state directly. This bypassed
  validation, reservations and readback, and contradicts ADR-0006.
- Fabricated "measurements" shown as real: stage 53.817, focus 2138, a "60x APO" objective, 26.84 µm,
  a 181/42/22 count table, "245 ROIs", and "37.0 °C, 5.0 % CO₂" incubator status.
  OpenEVOS shows only device-reported or simulator-labelled values; environment values are "Unavailable".

### 4.2 Blueprint v3.0 (S3)

**Adopted**
- Visual language: deep blue-black chrome, slate panels, cyan/teal actions, amber/red only for
  warnings/faults.
- Status pills that always pair colour with text.
- The workspace list and the Acquire / Inspect / Analyze / Instrument / Manage navigation groups.
- The capture-concept layout and the separate Mono/Color camera profiles.

**Corrected**
- The concept shows green "READY" on simulated devices → pills say "SIMULATED · IDLE".
- The concept omits Power, the Coarse/Fine/Quick focus sliders, Medium Jog and the buffer thumbnails →
  all restored from the manual.
- Concept values such as "Holder profile AMEPVH023" are illustrations only and are not used.

### 4.3 Spec v2.0 (S4)

**Adopted**
- The §18 manual-integrated requirements: vessel/channel/objective records with provenance, simulator
  requirements, and acceptance criteria as tests.

**Changed**
- Scan coverage is reduced to the six options actually printed on manual p.24. Percentage applies to
  Outer and Center.
- The original 7-option list mixed in an inferred option.

---

## 5. Hardware evidence that constrains the UI (S5)

`instrument/readiness.py` is displayed on the Hardware page. Its source is `BRINGUP-LOG-2026-11`, not
yet ingested as Inspector evidence.

| Subsystem | State | UI consequence |
|---|---|---|
| X/Y stage | Verified, limited: homes at power-on, closed-loop | Physical moves confined to the 22 mm boot box until Gate 7. Filter-cube access and shipping moves blocked |
| Z focus | Verified, limited: closed-loop, **collision risk** | Per-objective safe-Z envelope required (parfocality data) |
| F filter carriage | Verified, limited: closed-loop; a channel change moves it physically | **Interlock:** no exposure while the carriage moves. Implemented for real in the simulator: `SimIllumination` reports MOVING, and acquisitions hold the illumination resource shared |
| O objective turret | **Locked** (open-loop, unhomed) | Objective buttons disabled on hardware; magnification "unconfirmed" |
| P phase/condenser wheel | **Locked** | Phase channel and "Block condenser in fluorescence" disabled on hardware |
| LED board | Bootloader `0DAD:0057` → StartApp → application `0DAD:0051` | Light control only after application mode |
| Mono / Color cameras | Not integrated | Live and capture use simulator frames only, labelled SIMULATED |
| AMC1000 | Unavailable | Setpoints recorded; measured values "Unavailable" |
| `lico` calibration blob | Located (coca, moca, mcba, moco, tpco; 5 turret selections) | Read-only, displayed as "recovered", never written back |

---

## 6. Merged UI specification (as built)

### 6.1 Architecture

```
openevos-ui (PySide6)                       Qt-free workflow layer                 Phase 2.1 core
┌─────────────────────────────┐   calls   ┌──────────────────────────────┐ typed  ┌───────────────────────┐
│ shell: header/nav/footer    │──────────▶│ InstrumentController         │──────▶ │ CommandRouter         │
│ pages: Capture, Save, Scan, │  worker   │ CaptureService / Autofocus   │ cmds   │  SafetyManager (fail- │
│ Time-lapse, Z-Stack, Movies,│  threads  │ ScanRunner / TimelapseRunner │        │  closed), Resources,  │
│ Review, Analysis, Hardware, │◀──────────│ imaging (composite/save)     │◀────── │  readback verification│
│ Environment, Calibration,   │  queued   │ analysis (count)             │ events │ EventBus → Qt dispatch│
│ Settings, Service, Events   │  signals  │ instrument (profile/vessels/ │        │ FrameTransport        │
└─────────────────────────────┘           │ settings/readiness)          │        └───────────┬───────────┘
                                          └──────────────────────────────┘                    │
                                                                       optics simulator rig ◀─┘
                                     (stage, focus, turret, LED+carriage, mono + colour cameras)
```

**Session plumbing**
- `Session` is built by `build_sim_session()`. It bundles the core, profile, settings, vessel library,
  current vessel, stage map, controller, capture service, autofocus and simulator rig.
- `SessionModel` subscribes to the bus with a queued Qt dispatcher, polls the preview cache at ~15 Hz,
  keeps the event log and owns the `TaskRunner`. The runner allows one instrument workflow at a time;
  results come back to the GUI thread over a queued signal.

### 6.2 Chrome

- **Header:** OPENEVOS, model and profile name, **SIMULATION BACKEND** badge, and status pills for Stage,
  Focus, Turret, Light, Mono cam, Color cam and Environment. Each pill shows connection/health/activity
  in text (e.g. "SIMULATED · IDLE", "SIM · MOVING", "RECOVERY REQUIRED", "UNAVAILABLE").
- **Navigation:**
  - Acquire: Capture, Scan, Time-lapse, Z-Stack, Movies
  - Inspect: Save, Review
  - Analyze: Analysis
  - Instrument: Hardware, Environment, Calibration
  - Manage: Settings, Service, Event log
- **Footer:** session/vessel, command bus (IDLE / n ACTIVE), current task, XY/Z/objective, and the
  raw-data preservation note.

### 6.3 Capture workspace

All controls from §2.1. The image view has Live / Overlay / Fit / 1:1 / Crosshair / Scale bar /
Saturation, a histogram with the saturated fraction, and a frame line (device, frame #, channel,
exposure, gain, LED, preview drops). The badge always states channel, LIVE or CAPTURED, and SIMULATED
or CAMERA.

**Operator lock-out:** while a workflow runs (scan, time-lapse, autofocus, capture), the motion, light
and focus controls are disabled so the operator cannot fight the workflow.

### 6.4 Interlocks and command flow

1. A channel change becomes `SetIllumination`. The carriage travels (simulator latency) and SUCCEEDED
   only follows a readback through `verify_result()`. Turret and camera settings are verified the same
   way: this turn adds a non-motion verification hook to the router.
2. Capture:
   1. verified light;
   2. verified exposure and gain (live view paused for each and resumed afterwards);
   3. `acquire()`: camera exclusive + stage/focus/turret/illumination shared, through a scientific stream
      that faults explicitly and never drops frames silently;
   4. metadata recorded from the device states at exposure time.
3. A capture is refused if the stage is moving ("cannot acquire while devices are busy"), a channel does
   not match the camera, or no objective is in the path.
4. Autofocus refuses to run with the light off or unknown. A flat curve yields "no sample found", and the
   focus is returned to the centre of the search.
5. UI sliders go through a per-device **coalescer**: only the latest value is sent, one command per
   device at a time.

### 6.5 Simulator (what you see on screen)

`backends/simulation/optics.py` + `rig.py` render a deterministic synthetic culture that exists only
inside wells/slides. It reproduces:

- magnification-dependent FOV and pixel size;
- defocus blur from NA-based depth of field around a gently tilted sample surface;
- per-objective parfocal offsets;
- channel structure: DAPI nuclei, GFP cytoplasm, TxRed puncta, brightfield absorption, phase halos;
- LED × exposure × gain scaling with shot/read noise and 12-bit saturation;
- an 8-bit colour camera;
- darkness while the LED is off or the carriage travels;
- slow cell migration for time-lapse.

All of it is synthetic and labelled as such.

---

## 7. Deliberate deviations from the original

| Topic | Original | OpenEVOS | Why |
|---|---|---|---|
| Light slider | Proprietary combined control | Documented mapping: LED, then exposure, then gain | Reproducible and explainable; Actual Mode is unchanged |
| Find Sample / Auto Fine | Proprietary algorithms | Tenengrad sweep + parabolic refinement; prominence check; light check | No undisclosed heuristics; fails honestly |
| Auto Count | Proprietary | Otsu-anchored threshold, watershed separation, half-peak "aggressive" boundaries; parameters reported | Reproducible counts |
| Scan file format | `.sti` + quality export | TIFF tiles + manifest (+ mosaic) | Open formats; OME-TIFF/Zarr planned |
| Delete in Review | Deletes files | Not offered (Rename only) | Avoid irreversible actions in v0.3; the OS handles deletion |
| Environment | Live AMC1000 control | Setpoints recorded, measured "Unavailable" | No telemetry path exists (S5) |

---

## 8. What was built in this turn

**Code (new or changed)**

- **Instrument models** (`instrument/`):
  - `profile.py` + `data/sim_instrument_profile.json` — dual cameras, ≤4 cubes, ≤5 objective slots,
    provenance
  - `vessels.py` — library, Vessel Manager, stage mapping
  - `settings.py` — hot buttons, Mono/Color/Basic, Quick Save, light slider, persistence
  - `readiness.py`
- **Workflows** (`acquisition/`): `controller.py`, `capture.py`, `autofocus.py`, `scan_plan.py`,
  `scan_runner.py`, `timelapse.py`, `session.py`
- **Imaging and analysis:** `imaging/composite.py`, `imaging/save.py`, `imaging/focus_metrics.py`,
  `analysis/count.py`
- **Simulator** (`backends/simulation/`): `optics.py`, `devices.py` (SimTurret, SimIllumination),
  `rig.py`. `camera.py` gains gain control, verification and metadata hooks; `motion.py` gains a
  power-on position.
- **Core:**
  - new `SetCameraGain` command;
  - the router verifies non-motion commands through the optional backend hook `verify_result()`;
    otherwise it fails closed.
- **UI** (`ui/`): `app.py` (`openevos-ui`, `--screenshot-dir`), `shell.py`, `session_model.py`,
  `qt_bridge.py`, `theme.py`, `common.py`, `dialogs.py`, `screenshots.py`; widgets (image view, vessel
  map, joystick, channel button, marked slider, histogram, status pill); pages (capture, save, routines,
  workspaces, instrument)
- **Docs:** this report, ADR-0007, `docs/ui/screenshots/*.png`, `tools/make_ui_screenshots.py`
- **Packaging:** version 0.3.0.dev0. Extras: `imaging` (scipy, scikit-image, tifffile, pillow) and `ui`
  (PySide6 + imaging); `dev` includes `ui`. New console script `openevos-ui`.

**Tests:** 40 new: models, imaging/save/count/planning, simulator workflows on a real `ThreadClock`, and
offscreen UI. The Qt-independence test now runs in a fresh interpreter and also covers the new workflow
packages.

---

## 9. Gaps and roadmap

1. **Merge with your diverged repository.** You have the maintenance stack, ADR-0009–0021 and 787 tests
   that I don't have. This drop numbers its ADR as 0007. Please send the current repo and I will rebase
   these modules onto it and renumber.
2. **Physical backend** stays behind the gates in §5:
   - camera driver (the mono camera candidate in `candidate_ids.json` is a Point Grey Chameleon IIDC
     device);
   - LED-board application-mode protocol;
   - stage within the boot box;
   - Z safe envelope per objective.
3. **Measure tab parity:** 6 annotation tools, 12 ROIs, histogram/intensity plots, CSV export.
4. **Overlay** per-channel brightness/contrast; Count grid; Movie dual viewers.
5. Vessel alignment, objective FOV/parfocality/parcentration and hot-pixel workflows. These need a
   verified turret and camera.
6. OME-TIFF/Zarr writers; scan export quality presets.
7. Z-stack per time point; auto lighting (needs a validated auto-exposure).

## 10. How to run

```bash
pip install -e ".[dev]"          # or ".[ui]" for the UI only
openevos-ui                       # simulation backend
QT_QPA_PLATFORM=offscreen openevos-ui --no-settings --screenshot-dir out/   # headless screenshots
python -m pytest
```
