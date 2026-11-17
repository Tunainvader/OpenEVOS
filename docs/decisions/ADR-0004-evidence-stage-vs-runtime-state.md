# ADR-0004: Evidence stage and runtime state are separate axes

- **Status:** Accepted; the `RuntimeState` enum is superseded by ADR-0007 (three axes). The separation of evidence stage from runtime state still stands.
- **Date:** 2026-11-11

## Context

Plan v0.1 lists device states in three places that do not agree:

- §6.1: `detected, identified, connected, initialized, ready, busy, faulted, unknown`
- §6.2 (`DeviceStatus` comment): `detected / connected / ready / busy / faulted / unknown`
- §8.1: the five evidence stages `Detected → Identified → Connected → Responsive → Functionally validated`

These mix two different kinds of state. One is how much we *know* about a device, which only increases with evidence. The other is what the device is *doing now*, which changes all the time.

## Decision

- `EvidenceStage` (in `openevos.inspector.evidence`) holds the five ordered stages from §8.1. It advances only through a `Verified` finding, and each context has a cap (the Inspector is capped at `IDENTIFIED`).
- `RuntimeState` (in `openevos.hal.status_model`) holds `disconnected, initializing, ready, busy, faulted, unknown`.
- `state_is_admissible()` links the two. For example, `READY` or `BUSY` needs stage ≥ `RESPONSIVE`.

## Consequences

A device can be `FUNCTIONALLY_VALIDATED` and `FAULTED` at the same time, which is correct. The UI shows both values. `DeviceStatus.state` in the plan's HAL sketch should use `RuntimeState` when the HAL is implemented in Phase 2.
