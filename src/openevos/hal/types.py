"""Value types shared by every HAL interface and backend (plan §6.2, ADR-0004).

Nothing here talks to hardware. These are plain data containers so that the UI, the core
services and the command policy can exchange device information without knowing which
backend produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from openevos.hal.status_model import RuntimeState, state_is_admissible
from openevos.inspector.evidence import EvidenceStage


class Role(str, Enum):
    """Logical role of a device in the instrument. One device per role."""

    CAMERA_MONO = "camera.mono"
    CAMERA_COLOR = "camera.color"
    XY_STAGE = "xy"
    FOCUS = "focus"
    OBJECTIVE_TURRET = "objective"
    PHASE_TURRET = "phase"
    ILLUMINATION = "illumination"  # LED channels + epi (cube) carriage
    INCUBATOR = "incubator"

    @property
    def display(self) -> str:
        return ROLE_DISPLAY[self]

    @property
    def is_camera(self) -> bool:
        return self in (Role.CAMERA_MONO, Role.CAMERA_COLOR)


ROLE_DISPLAY = {
    Role.CAMERA_MONO: "Mono camera",
    Role.CAMERA_COLOR: "Colour camera",
    Role.XY_STAGE: "XY stage",
    Role.FOCUS: "Focus (Z)",
    Role.OBJECTIVE_TURRET: "Objective turret",
    Role.PHASE_TURRET: "Phase turret",
    Role.ILLUMINATION: "Illumination",
    Role.INCUBATOR: "Incubator",
}


@dataclass(frozen=True)
class DeviceIdentity:
    """Who a device is. `simulated` is mandatory so a simulator can never pass as hardware."""

    device_type: str
    display_name: str
    backend: str
    simulated: bool
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    transport: str | None = None
    evidence_ids: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class DeviceStatus:
    """What a device is doing now (RuntimeState) plus how much we know about it (EvidenceStage).

    The two axes are deliberately separate (ADR-0004). `evidence_stage` is None for simulated
    devices: there is no physical device, so there is nothing to have evidence about.
    """

    state: RuntimeState
    message: str = ""
    simulated: bool = False
    evidence_stage: EvidenceStage | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_state(self) -> RuntimeState:
        """The state the UI may show.

        A real (non-simulated) device whose backend claims READY/BUSY without the evidence stage
        that state needs is shown as UNKNOWN, so "enumerated" can never be displayed as
        "working" (ADR-0004, plan §4.5).
        """
        if self.simulated:
            return self.state
        if state_is_admissible(self.state, self.evidence_stage):
            return self.state
        return RuntimeState.UNKNOWN


@dataclass(frozen=True)
class StatePosition:
    """One position of a discrete state device (turret slot, carriage slot)."""

    index: int  # 1-based, as an operator counts them
    label: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ObjectiveInfo:
    position: int
    label: str
    magnification: float
    numerical_aperture: float
    phase_ring: str | None = None  # phase annulus this objective needs, e.g. "Ph1"
    parfocal_offset_um: float = 0.0  # focus offset relative to the reference objective
    immersion: str = "air"


@dataclass(frozen=True)
class LedChannel:
    """One LED output. `carriage_slot` is the slot whose cube holds this LED (None = transmitted)."""

    channel_id: str
    label: str
    carriage_slot: int | None
    excitation_nm: str | None = None
    emission_nm: str | None = None
    max_intensity_pct: float = 100.0


@dataclass(frozen=True)
class LedState:
    channel_id: str
    enabled: bool
    intensity_pct: float


@dataclass(frozen=True)
class CameraCapabilities:
    width: int
    height: int
    bit_depth: int
    color: bool
    pixel_size_um: float  # physical sensor pixel pitch
    exposure_range_ms: tuple[float, float]
    gain_range_db: tuple[float, float]
    max_fps: float
    pixel_format: str  # "MONO16 (12-bit data)" / "RGB8"
    sensor: str = ""


@dataclass(frozen=True)
class Frame:
    """One image. `pixels` is a numpy array: (H, W) uint16 for mono, (H, W, 3) uint8 for colour."""

    pixels: Any
    index: int
    timestamp: float  # time.time()
    camera_role: str
    exposure_ms: float
    gain_db: float
    bit_depth: int
    simulated: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.pixels.shape)

    @property
    def max_value(self) -> int:
        return (1 << self.bit_depth) - 1


@dataclass(frozen=True)
class Reading:
    """One telemetry value with its unit and where it came from."""

    value: float | bool | None
    unit: str
    source: str  # "simulated" | "sensor" | "setpoint-echo" | ...


@dataclass(frozen=True)
class IncubatorTelemetry:
    timestamp: float
    readings: dict[str, Reading]
    simulated: bool


@dataclass(frozen=True)
class ControlSpec:
    """A controllable incubator variable (setpoint) and its permitted range."""

    name: str
    label: str
    unit: str
    minimum: float
    maximum: float
    default: float
