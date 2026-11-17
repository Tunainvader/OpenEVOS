"""Runtime (operational) device state vs evidence stage (ADR-0004, revised by ADR-0007).

* **Evidence stage** (Detected → Identified → Connected → Responsive → Functionally validated):
  how much we *know* about a physical device. Monotonic; advances only with evidence.
  -> `openevos.inspector.evidence.EvidenceStage`
* **Runtime state**: what the device is doing *now*. ADR-0007 models it as three independent axes
  (Connection, Health, Activity) in `openevos.core.models`, because a single enum cannot express
  "connected but faulted" or "connected and busy".

`activity_is_admissible()` links the two for physical devices: a device cannot be reported as
moving or acquiring under OpenEVOS control unless a safe read has succeeded (stage >= RESPONSIVE).
Simulated devices carry no evidence stage and are exempt.

Transitional: the single-enum `RuntimeState` of ADR-0004 remains only as the HAL devices' own
`get_status()` vocabulary (`hal/types.py`, the simulator). `hal/adapter.py` translates it into the
three core axes; validation never reads it (ADR-0010). Do not use it in new code.
"""

from __future__ import annotations

from enum import Enum

from openevos.core.models import Activity, BackendKind, Connection, Health
from openevos.inspector.evidence import EvidenceStage

__all__ = [
    "MIN_STAGE_FOR_ACTIVITY",
    "MIN_STAGE_FOR_STATE",
    "Activity",
    "Connection",
    "Health",
    "RuntimeState",
    "activity_is_admissible",
    "state_is_admissible",
]

# --------------------------------------------------------------------------------------------
# Three-axis model (ADR-0007): use this.
# --------------------------------------------------------------------------------------------
MIN_STAGE_FOR_ACTIVITY: dict[Activity, EvidenceStage] = {
    Activity.UNKNOWN: EvidenceStage.DETECTED,
    Activity.INITIALIZING: EvidenceStage.CONNECTED,
    Activity.IDLE: EvidenceStage.RESPONSIVE,
    Activity.MOVING: EvidenceStage.RESPONSIVE,
    Activity.ACQUIRING: EvidenceStage.RESPONSIVE,
    Activity.CALIBRATING: EvidenceStage.RESPONSIVE,
}


def activity_is_admissible(activity: Activity, stage: EvidenceStage | None,
                           backend_kind: BackendKind = BackendKind.PHYSICAL) -> bool:
    if backend_kind is BackendKind.SIMULATED:
        return True
    if stage is None:
        return activity is Activity.UNKNOWN
    return stage >= MIN_STAGE_FOR_ACTIVITY[activity]


# --------------------------------------------------------------------------------------------
# Transitional single-enum model (ADR-0004), for the gateway-era HAL only.
# --------------------------------------------------------------------------------------------
class RuntimeState(str, Enum):
    DISCONNECTED = "disconnected"
    INITIALIZING = "initializing"
    READY = "ready"
    BUSY = "busy"
    FAULTED = "faulted"
    UNKNOWN = "unknown"


#: Minimum evidence stage before a runtime state is meaningful to display.
MIN_STAGE_FOR_STATE: dict[RuntimeState, EvidenceStage] = {
    RuntimeState.DISCONNECTED: EvidenceStage.DETECTED,
    RuntimeState.UNKNOWN: EvidenceStage.DETECTED,
    RuntimeState.INITIALIZING: EvidenceStage.CONNECTED,
    RuntimeState.READY: EvidenceStage.RESPONSIVE,
    RuntimeState.BUSY: EvidenceStage.RESPONSIVE,
    RuntimeState.FAULTED: EvidenceStage.CONNECTED,
}


def state_is_admissible(state: RuntimeState, stage: EvidenceStage | None) -> bool:
    """A device cannot be 'ready' unless a safe read has succeeded (stage >= RESPONSIVE)."""
    if stage is None:
        return state in (RuntimeState.UNKNOWN, RuntimeState.DISCONNECTED)
    return stage >= MIN_STAGE_FOR_STATE[state]
