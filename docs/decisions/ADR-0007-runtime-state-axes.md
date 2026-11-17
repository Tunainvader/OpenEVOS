# ADR-0007: Runtime state as three independent axes (revises ADR-0004)

- **Status:** Accepted
- **Date:** 2026-11-12
- **Supersedes:** the `RuntimeState` enum in ADR-0004. ADR-0004's main point still holds: evidence stage and runtime state are separate.

## Context

ADR-0004 replaced the plan's mixed state lists with a single `RuntimeState` (`disconnected, initializing, ready, busy, faulted, unknown`). The 12 Nov 2026 handoff (§12.1–12.2) showed that a single enum still can't express common real situations: a camera that is *connected but faulted*, or a stage that is *connected, healthy and moving*.

## Decision

Runtime state lives in `openevos.core.models` as three independent axes:

| Axis | Values |
|---|---|
| `Connection` | DISCONNECTED, CONNECTING, CONNECTED, UNKNOWN |
| `Health` | NORMAL, WARNING, FAULT, UNKNOWN |
| `Activity` | IDLE, INITIALIZING, MOVING, ACQUIRING, CALIBRATING, UNKNOWN |

Alongside these:

- `recovery_required` is a control flag. It is set after a timeout or a readback mismatch and cleared only by an explicit recovery.
- `observed_position` comes from the backend and `requested_position` from the router on acceptance. They are never merged.
- A newly registered device is UNKNOWN on every axis. Nothing is assumed to be idle or healthy until its backend says so.
- `DeviceSnapshot` is a frozen model containing only immutable values, with a version that increases by one per change.
- `hal.status_model.activity_is_admissible()` links runtime state to the evidence stage for physical devices. For example, MOVING requires a stage ≥ RESPONSIVE.

The "calibrated" rung in the handoff's Appendix B ladder is **not** a runtime axis. It belongs to the capability record (`AxisCapability.calibration_valid`, `CapabilitySource`; see ADR-0008). "Safe for a particular command" is decided per command by the SafetyManager.

## Consequences

- The UI shows three badges per device (connection, health, activity), plus a recovery banner when the flag is set.
- The SafetyManager requires CONNECTED + NORMAL + IDLE + no recovery flag. Any UNKNOWN fails closed.
