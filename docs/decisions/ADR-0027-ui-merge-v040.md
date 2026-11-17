# ADR-0027: Adopt the v0.4.0 desktop UI on this repository's core

- **Status:** Accepted, implemented (2026-11-13), phases 1–4 of [`docs/ui/UI_MERGE_ANALYSIS.md`](../ui/UI_MERGE_ANALYSIS.md). Phase 5 (running it on the instrument) is pending the instrument.
- **Supersedes:** the Phase 2 desktop app (ADR-0010's UI pages). The command-core decisions of ADR-0010 still hold for the gateway and HAL path, which the maintenance tools and tests still use.
- **Related:** ADR-0025 (UI manual parity) and ADR-0026 (Craftimon design system), both imported from the v0.4.0 line and renumbered; ADR-0023 and ADR-0024 (motor telemetry and X/Y control).

## Decision
1. **The desktop app is the v0.4.0 line's manual-parity UI** (`openevos.ui`: shell, pages, widgets, Craftimon tokens) with its Qt-free workflow layers (`instrument`, `acquisition`, `imaging`, `analysis`) and its optics simulator (`openevos.backends.rig`). It runs on **this repository's core**: router, stricter SafetyManager, firmware policy, Technosoft backend.
2. **Core reconciliation, all additive:**
   - the router gains `verify_result` (non-motion readback) and `cancel_all()` (workflow cancel; the latching STOP is unchanged);
   - gain stays **dB** in the core; the controller and the simulator camera convert to and from the UI's linear factor;
   - the rig simulator declares its parameter ranges, because the validator fails closed without them.
3. **Physical stage in the new app:**
   - `--motor-telemetry PORT` adds the drives read-only;
   - `--motor-control` makes the controller's `stage_device` the real `motor.stage`. The rig keeps its own simulated stage for images.

   Every stage move passes the router and SafetyManager (0–22 mm box, verified capability, homed, idle, firmware). Relative moves on a physical stage are **refused, not clamped**. Every `motor.*` command lifecycle is appended to `physical_audit.jsonl`. Vessel-map moves are disabled for the physical stage, because its vessel mapping is not calibrated. The badge and footer say "SIM + PHYSICAL X/Y STAGE". The Hardware page offers fixed 0.1/1/5 mm steps and Stop stage.
4. **Readiness is tied to gate evidence:**
   - X/Y: verified-limited;
   - Z, F, LED, lico: read-only (state verified, never commanded);
   - O/P: locked.
5. **The legacy Phase 2 UI is retired.** Its Diagnostics (Inspector scans) page is ported, and its other functions exist in the new app. A full pre-merge copy is in `backups/OpenEVOS-pre-ui-merge-20261113-140944.zip`.
6. **The guards cover the new packages:** no transports, FFI or network in UI, workflow or simulator code; the workflow layers stay Qt-free; neither UI imports a backend; firmware names stay banned everywhere.
7. **Version 0.5.0.dev0.** New extras `imaging` and `ui`; the entry point is `openevos` / `openevos-ui` / `python -m openevos.ui`.

## Consequences
- **Two simulators remain:** the HAL-adapter simulator (`backends.simulation`, used by `build_instrument`, the gateway and its tests) and the rig (the app). Consolidating them is future work.
- **No gateway audit in the new app:** its commands do not pass the gateway's audit or actor policy. The physical audit file covers the hardware-facing commands; full audit of simulated commands is future work.
- **v0.4.0's tests now run here** (198 more tests), alongside this repository's.
