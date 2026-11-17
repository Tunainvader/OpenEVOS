<!-- Imported from the v0.4.0 line (ADR-0007-ui-manual-parity.md) by the UI merge; renumbered ADR-0007 -> ADR-0025. Its references to ADR-0005/0006 mean that line's runtime-state-axes / core-execution-model ADRs = ADR-0007/ADR-0008 here. -->
# ADR-0025 — Desktop UI: manual parity on top of the command router

Status: accepted (v0.3.0.dev0)
Context: [UI Integration Report](../ui/UI_Integration_Report.md)

## Decision

1. **The original EVOS FL Auto user guide (MAN0007986 Rev C.0) is the behaviour reference.** Every
   control on the original Image / Scan / Time Lapse / System tabs gets an OpenEVOS equivalent or a
   documented reason why not. Concept art and earlier AI drafts are layout input only.
2. **The UI never writes device state.** Every action becomes a typed command through
   `InstrumentController` → `CommandRouter.submit()`, or a workflow (capture, autofocus, scan,
   time-lapse) running on a worker thread that does the same. Blocking work never runs on the GUI thread.
3. **A Qt-free workflow layer** (`openevos.instrument`, `acquisition`, `imaging`, `analysis`) holds all
   logic, so it can be tested without Qt and reused by scripts. The Qt layer (`openevos.ui`) only
   presents it. A test enforces that these packages never import Qt.
4. **Exposures are scientific acquisitions.** A capture reserves the camera exclusively and holds stage,
   focus, turret and illumination *shared*: the filter-cube carriage, turret and stage cannot move
   mid-frame (the F-carriage interlock from the bring-up record).
5. **Honesty in the UI.** Simulated devices show "SIMULATED", never "READY". Synthetic frames carry a
   SIMULATED badge, and saved files say SIMULATED in the TIFF description and the JSON sidecar.
   Environment readings with no telemetry show "Unavailable". Vessel geometry and alignment are shown as
   nominal or unverified.
6. **Physical readiness comes from the bring-up record, not the simulator.** The Hardware page shows what
   the physical unit can do today: X/Y/Z/F verified but limited, O/P locked, cameras not integrated,
   AMC1000 unavailable. Nothing in that table enables a physical command: verified capability records
   with evidence IDs are still required (ADR-0006).
7. **Camera settings need an idle camera** (fail-closed safety). Live view is paused for an exposure or
   gain command and resumed afterwards. Slider traffic goes through a per-device coalescer, so the UI
   cannot flood the router or trip its own resource conflicts.

## Consequences

* Some original behaviours are reimplemented, not copied: the single Light-slider mapping, the autofocus
  search and the auto-count thresholds. The documentation says so and the parameters are recorded with
  each result.
* Planned features are visibly marked "planned" or "not available" instead of being faked. These include
  objective calibration, vessel alignment, hot-pixel correction, the Measure tab annotation set, scan
  `.sti` export and Z-stack per time point.
