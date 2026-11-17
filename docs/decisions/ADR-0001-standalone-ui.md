# ADR-0001: Standalone PySide6 application; Micro-Manager is a backend, not the UI

- **Status:** Accepted (proposed in plan v0.1 §19; hardware-independent, safe to lock now)
- **Date:** 2026-11-11

## Context

OpenEVOS has to own the workflow, safety policy, diagnostics and AI assistant. Micro-Manager Studio is a capable general-purpose UI, but building on it would tie the OpenEVOS user experience, release cadence and safety controls to a Java/ImageJ application. None of these concerns depends on what hardware is inside the EVOS FL Auto.

## Decision

1. The user-facing application is a standalone **Python + PySide6 (Qt)** desktop app.
2. UI code calls OpenEVOS services only. It never calls MMCore, a vendor SDK or a transport directly.
3. Micro-Manager, if used, is reached only through the OpenEVOS HAL (ADR-0002).
4. Inspector v0.1 ships as a **CLI first**. The PySide6 inventory and report views come in Phase 2 and read the same JSON exports.

## Consequences

- We need our own camera view, acquisition planner and device panels (planned for Phase 2).
- The UI can be developed and tested entirely against the simulation backend.
- PySide6 is LGPL; we must review licensing and dynamic linking before the first binary release.
