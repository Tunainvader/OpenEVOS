"""Runtime models: device state axes, immutable snapshots, typed commands, command status.

Design rules (handoff §12, ADR-0007):
* Connection, health and activity are independent axes. A camera can be CONNECTED and FAULT.
* A newly registered device is UNKNOWN on every axis until its backend reports otherwise.
* Snapshots are frozen pydantic models containing only immutable values (tuples, scalars), so a
  snapshot handed to a subscriber can never change underneath it.
* Requested state (what we asked for) and observed state (what the backend reported) are
  separate fields.
* Commands are typed per operation; numbers must be finite and booleans are never numbers.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    model_validator,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------------------------
# State axes
# --------------------------------------------------------------------------------------------
class Connection(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    UNKNOWN = "unknown"


class Health(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    FAULT = "fault"
    UNKNOWN = "unknown"


class Activity(str, Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    MOVING = "moving"
    ACQUIRING = "acquiring"
    CALIBRATING = "calibrating"
    UNKNOWN = "unknown"


class FirmwareMode(str, Enum):
    """Which firmware a device is running (ADR-0009). BOOTLOADER quarantines the device."""

    APPLICATION = "application"
    BOOTLOADER = "bootloader"
    RECOVERY = "recovery"  # any non-application recovery/safe mode: quarantined like BOOTLOADER
    UNKNOWN = "unknown"


class DeviceKind(str, Enum):
    CAMERA = "camera"
    XY_STAGE = "xy_stage"
    FOCUS = "focus"
    TURRET = "turret"
    ILLUMINATION = "illumination"
    INCUBATOR = "incubator"


class BackendKind(str, Enum):
    SIMULATED = "simulated"
    PHYSICAL = "physical"


# --------------------------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------------------------
def _finite_number(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        # ValueError, not TypeError: pydantic only converts ValueError into a ValidationError
        raise ValueError(f"expected a finite number, got {type(v).__name__}")  # noqa: TRY004
    f = float(v)
    if not math.isfinite(f):
        raise ValueError("value must be finite (NaN and infinity are rejected)")
    return f


FiniteFloat = Annotated[float, BeforeValidator(_finite_number)]


class Position(BaseModel):
    """Immutable axis positions, e.g. axes=('x','y'), values=(10.0, 20.0), unit='um'."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    axes: tuple[str, ...]
    values: tuple[FiniteFloat, ...]
    unit: str
    frame: str

    @model_validator(mode="after")
    def _shape(self) -> Position:
        if len(self.axes) != len(self.values) or not self.axes:
            raise ValueError("axes and values must be non-empty and the same length")
        if len(set(self.axes)) != len(self.axes):
            raise ValueError("duplicate axis names")
        return self

    def get(self, axis: str) -> float:
        return self.values[self.axes.index(axis)]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.axes, self.values))


# --------------------------------------------------------------------------------------------
# Device snapshot
# --------------------------------------------------------------------------------------------
_ATTR_SCALARS = (str, int, float, bool, type(None))


def _immutable_attr(value: Any) -> bool:
    if isinstance(value, tuple):
        return all(_immutable_attr(v) for v in value)
    return isinstance(value, _ATTR_SCALARS)


class DeviceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    kind: DeviceKind
    backend_kind: BackendKind
    connection: Connection = Connection.UNKNOWN
    health: Health = Health.UNKNOWN
    activity: Activity = Activity.UNKNOWN
    firmware_mode: FirmwareMode = FirmwareMode.UNKNOWN  # observed; BOOTLOADER => quarantine (ADR-0009)
    recovery_required: bool = False  # set after timeout / readback mismatch; cleared only by recovery
    observed_position: Position | None = None
    requested_position: Position | None = None
    observed_at: datetime | None = None
    observed_source: str | None = None
    version: int = 0
    note: str | None = None
    #: Other observed facts interlocks rely on, e.g. ("homed", True), ("carriage_position", 2),
    #: ("live", False). Reported by the backend like every other observation; immutable values only.
    attributes: tuple[tuple[str, Any], ...] = ()

    @model_validator(mode="after")
    def _attributes_immutable(self) -> DeviceSnapshot:
        names = [k for k, _ in self.attributes]
        if len(set(names)) != len(names):
            raise ValueError("duplicate attribute names")
        for k, v in self.attributes:
            if not _immutable_attr(v):
                raise ValueError(f"attribute {k!r} must be an immutable scalar or tuple")
        return self

    def attr(self, name: str, default: Any = None) -> Any:
        return next((v for k, v in self.attributes if k == name), default)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------
class CommandOrigin(str, Enum):
    UI = "ui"
    ACQUISITION = "acquisition"
    SCRIPT = "script"
    SYSTEM = "system"  # start-up / shut-down sequences
    TEST = "test"
    AI = "ai"  # always rejected by the SafetyManager: AI is advisory (ADR-0003)


class CommandBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(default_factory=new_id)
    device_id: str = Field(min_length=1)
    origin: CommandOrigin
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    timeout_s: FiniteFloat = Field(default=30.0, gt=0, le=3600)
    #: Holder of a long-lived reservation (e.g. "acquisition:<run>") this command runs under.
    owner: str | None = None
    #: Explicit operator confirmation, required for the kinds in CONFIRMATION_REQUIRED.
    confirmed: StrictBool = False


class MoveStageAbsolute(CommandBase):
    kind: Literal["stage.move_absolute"] = "stage.move_absolute"
    x: FiniteFloat
    y: FiniteFloat
    unit: str
    frame: str

    def target(self) -> Position:
        return Position(axes=("x", "y"), values=(self.x, self.y), unit=self.unit, frame=self.frame)


class MoveFocusAbsolute(CommandBase):
    kind: Literal["focus.move_absolute"] = "focus.move_absolute"
    z: FiniteFloat
    unit: str
    frame: str

    def target(self) -> Position:
        return Position(axes=("z",), values=(self.z,), unit=self.unit, frame=self.frame)


class SetTurretPosition(CommandBase):
    kind: Literal["turret.set_position"] = "turret.set_position"
    position: StrictInt = Field(ge=1)


class SetIllumination(CommandBase):
    kind: Literal["illumination.set"] = "illumination.set"
    channel: str = Field(min_length=1)
    intensity_percent: FiniteFloat = Field(ge=0, le=100)
    enabled: StrictBool


class SetCameraExposure(CommandBase):
    kind: Literal["camera.set_exposure"] = "camera.set_exposure"
    exposure_ms: FiniteFloat = Field(gt=0, le=60_000)


class HomeStage(CommandBase):
    kind: Literal["stage.home"] = "stage.home"


class HomeFocus(CommandBase):
    kind: Literal["focus.home"] = "focus.home"


class SelectCarriagePosition(CommandBase):
    """Move the epi carriage (cube slots + mirror) to a numbered position."""

    kind: Literal["illumination.select_carriage"] = "illumination.select_carriage"
    position: StrictInt = Field(ge=1)


class AllIlluminationOff(CommandBase):
    kind: Literal["illumination.all_off"] = "illumination.all_off"


class SetCameraGain(CommandBase):
    kind: Literal["camera.set_gain"] = "camera.set_gain"
    gain_db: FiniteFloat


class SetCameraLive(CommandBase):
    kind: Literal["camera.set_live"] = "camera.set_live"
    live: StrictBool


class SnapImage(CommandBase):
    kind: Literal["camera.snap"] = "camera.snap"


class SetIncubatorSetpoint(CommandBase):
    kind: Literal["incubator.set_setpoint"] = "incubator.set_setpoint"
    control: str = Field(min_length=1)
    value: FiniteFloat


class SetIncubatorControlEnabled(CommandBase):
    kind: Literal["incubator.set_enabled"] = "incubator.set_enabled"
    control: str = Field(min_length=1)
    enabled: StrictBool


Command = Annotated[
    MoveStageAbsolute | MoveFocusAbsolute | SetTurretPosition | SetIllumination | SetCameraExposure
    | HomeStage | HomeFocus | SelectCarriagePosition | AllIlluminationOff | SetCameraGain | SetCameraLive
    | SnapImage | SetIncubatorSetpoint | SetIncubatorControlEnabled,
    Field(discriminator="kind"),
]
COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)

MOTION_COMMANDS = (MoveStageAbsolute, MoveFocusAbsolute)

#: Which device kind each command targets.
COMMAND_DEVICE_KIND: dict[str, DeviceKind] = {
    "stage.move_absolute": DeviceKind.XY_STAGE,
    "stage.home": DeviceKind.XY_STAGE,
    "focus.move_absolute": DeviceKind.FOCUS,
    "focus.home": DeviceKind.FOCUS,
    "turret.set_position": DeviceKind.TURRET,
    "illumination.set": DeviceKind.ILLUMINATION,
    "illumination.select_carriage": DeviceKind.ILLUMINATION,
    "illumination.all_off": DeviceKind.ILLUMINATION,
    "camera.set_exposure": DeviceKind.CAMERA,
    "camera.set_gain": DeviceKind.CAMERA,
    "camera.set_live": DeviceKind.CAMERA,
    "camera.snap": DeviceKind.CAMERA,
    "incubator.set_setpoint": DeviceKind.INCUBATOR,
    "incubator.set_enabled": DeviceKind.INCUBATOR,
}

#: Kinds that need `confirmed=True` (homing drives to the reference switches; incubator changes
#: affect living samples).
CONFIRMATION_REQUIRED = frozenset({"stage.home", "focus.home", "incubator.set_setpoint", "incubator.set_enabled"})

#: Activities (besides IDLE) in which a kind is still admissible. Exposure and gain may change
#: while the camera streams, and live view may be switched off at any time.
EXTRA_ADMISSIBLE_ACTIVITY: dict[str, frozenset[Activity]] = {
    "camera.set_exposure": frozenset({Activity.ACQUIRING}),
    "camera.set_gain": frozenset({Activity.ACQUIRING}),
    "camera.set_live": frozenset({Activity.ACQUIRING}),
}


def is_safe_direction(command: Command) -> bool:
    """Commands that only make the instrument safer: LEDs off, live view off.

    They are admitted while STOP is latched, under any long reservation, and regardless of
    activity, health or a pending recovery (switching light off is always safe). They are never
    exempt from identity, connection or firmware-mode checks.
    """
    return isinstance(command, AllIlluminationOff) or (
        isinstance(command, SetCameraLive) and not command.live) or (
        isinstance(command, SetIllumination) and not command.enabled)


class FirmwareOperationRefused(ValueError):
    """A banned or maintenance-only firmware operation was requested (ADR-0009)."""


def parse_command(data: dict) -> Command:
    """Parse untrusted input (e.g. from a UI or script) into a typed command; unknown kinds fail.

    Firmware operations are refused before parsing, with an explicit reason, even though no such
    command type exists: a future command type can never quietly make flashing possible.
    """
    from openevos.safety.firmware import operation_refusal

    kind = data.get("kind") if isinstance(data, dict) else None
    if isinstance(kind, str):
        refusal = operation_refusal(kind)
        if refusal:
            raise FirmwareOperationRefused(refusal)
    return COMMAND_ADAPTER.validate_python(data)


def _assert_no_firmware_command_types() -> None:
    from openevos.safety.firmware import operation_refusal

    for kind in COMMAND_DEVICE_KIND:
        if operation_refusal(kind):
            raise RuntimeError(f"command type {kind!r} is a firmware operation; banned by ADR-0009")


_assert_no_firmware_command_types()


# --------------------------------------------------------------------------------------------
# Command lifecycle
# --------------------------------------------------------------------------------------------
class CommandStatus(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {CommandStatus.SUCCEEDED, CommandStatus.REJECTED, CommandStatus.FAILED,
     CommandStatus.CANCELLED, CommandStatus.TIMED_OUT}
)

S = CommandStatus
ALLOWED_TRANSITIONS: dict[CommandStatus, frozenset[CommandStatus]] = {
    S.QUEUED: frozenset({S.VALIDATING, S.CANCELLED}),
    S.VALIDATING: frozenset({S.ACCEPTED, S.REJECTED}),
    S.ACCEPTED: frozenset({S.RUNNING, S.CANCELLING, S.CANCELLED, S.FAILED, S.TIMED_OUT}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLING, S.TIMED_OUT}),
    S.CANCELLING: frozenset({S.CANCELLED, S.SUCCEEDED, S.FAILED, S.TIMED_OUT}),
}
for _t in TERMINAL_STATUSES:
    ALLOWED_TRANSITIONS[_t] = frozenset()


class CommandRecord(BaseModel):
    """Immutable view of a command's current lifecycle state."""

    model_config = ConfigDict(frozen=True)

    command: Command
    status: CommandStatus
    message: str | None = None
    result: Position | None = None
    #: For REJECTED: which check refused it ("safety", "resource", "reserved", "stop_latch").
    check: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
