"""HAL device contracts (plan §6.2).

These describe what OpenEVOS needs from each kind of device. They are **not** a claim that the
physical EVOS FL Auto exposes every operation: a real backend implements what its verified
interface supports and raises `NotSupportedError` for the rest.

Rules that every backend follows:

* Units are part of the method name (`_um`, `_ms`, `_db`, `_pct`). No unit-less floats.
* State-changing methods are only ever called by `openevos.hal.adapter.HalDeviceBackend`, after
  the command core has validated and reserved the command (ADR-0010). UI code reaches devices
  through `CoreGateway.read(role)`, which exposes the read-only methods listed in `READ_METHODS`.
* `halt()` brings the device to a safe state at once (stop motion, LEDs off, stop streaming).
  It must not block on anything slow or remote.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from openevos.hal.types import (
    CameraCapabilities,
    ControlSpec,
    DeviceIdentity,
    DeviceStatus,
    Frame,
    IncubatorTelemetry,
    LedChannel,
    LedState,
    ObjectiveInfo,
    Role,
    StatePosition,
)


class NotSupportedError(RuntimeError):
    """The backend has no verified way to perform this operation."""


class DeviceError(RuntimeError):
    """The device reported or caused an error while executing an operation."""


class HardwareDevice(ABC):
    @abstractmethod
    def identify(self) -> DeviceIdentity: ...

    @abstractmethod
    def get_status(self) -> DeviceStatus: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    def halt(self) -> None:  # noqa: B027 - a deliberate no-op default; devices that move override it
        """Bring the device to a safe state immediately. Default (deliberately): nothing to do."""

    def is_busy(self) -> bool:
        return False


# --------------------------------------------------------------------------------- camera
class CameraDevice(HardwareDevice):
    @abstractmethod
    def get_capabilities(self) -> CameraCapabilities: ...

    @abstractmethod
    def get_exposure_ms(self) -> float: ...

    @abstractmethod
    def set_exposure_ms(self, exposure_ms: float) -> None: ...

    @abstractmethod
    def get_gain_db(self) -> float: ...

    @abstractmethod
    def set_gain_db(self, gain_db: float) -> None: ...

    @abstractmethod
    def snap(self) -> Frame: ...

    @abstractmethod
    def start_live(self) -> None: ...

    @abstractmethod
    def stop_live(self) -> None: ...

    @abstractmethod
    def is_live(self) -> bool: ...

    @abstractmethod
    def get_latest_frame(self) -> Frame | None: ...


# --------------------------------------------------------------------------------- motion
class XYStageDevice(HardwareDevice):
    @abstractmethod
    def get_position_um(self) -> tuple[float, float]: ...

    @abstractmethod
    def get_limits_um(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """((x_min, x_max), (y_min, y_max)) in stage coordinates."""

    @abstractmethod
    def move_absolute_um(self, x_um: float, y_um: float) -> None:
        """Start a move. Returns immediately; poll `is_busy()` for completion."""

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_homed(self) -> bool: ...

    @abstractmethod
    def home(self) -> None: ...

    def halt(self) -> None:
        self.stop()


class FocusDevice(HardwareDevice):
    @abstractmethod
    def get_position_um(self) -> float: ...

    @abstractmethod
    def get_limits_um(self) -> tuple[float, float]: ...

    @abstractmethod
    def move_absolute_um(self, z_um: float) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_homed(self) -> bool: ...

    @abstractmethod
    def home(self) -> None: ...

    def halt(self) -> None:
        self.stop()


class StateDevice(HardwareDevice):
    """A device with a fixed set of numbered positions (turrets, carriages)."""

    @abstractmethod
    def get_positions(self) -> list[StatePosition]: ...

    @abstractmethod
    def get_position(self) -> int: ...

    @abstractmethod
    def select_position(self, position: int) -> None: ...


class ObjectiveTurretDevice(StateDevice):
    @abstractmethod
    def get_objectives(self) -> list[ObjectiveInfo]: ...

    def current_objective(self) -> ObjectiveInfo:
        pos = self.get_position()
        for obj in self.get_objectives():
            if obj.position == pos:
                return obj
        raise DeviceError(f"no objective metadata for position {pos}")


class PhaseTurretDevice(StateDevice):
    """Condenser phase-annulus turret. Position labels name the annulus ("Open", "Ph1", ...)."""


# --------------------------------------------------------------------------------- light
class IlluminationDevice(HardwareDevice):
    """LED channels plus the epi carriage that holds the light cubes.

    On the EVOS FL Auto the cube carriage is a motor axis and the LEDs sit on a separate board
    (vendor analysis, unverified); a real backend may compose two transports behind this one
    interface.
    """

    # LEDs
    @abstractmethod
    def get_channels(self) -> list[LedChannel]: ...

    @abstractmethod
    def get_led_states(self) -> list[LedState]: ...

    @abstractmethod
    def set_led(self, channel_id: str, enabled: bool, intensity_pct: float) -> None: ...

    @abstractmethod
    def all_leds_off(self) -> None: ...

    # carriage
    @abstractmethod
    def get_carriage_positions(self) -> list[StatePosition]: ...

    @abstractmethod
    def get_carriage_position(self) -> int: ...

    @abstractmethod
    def select_carriage_position(self, position: int) -> None: ...

    def halt(self) -> None:
        self.all_leds_off()


# --------------------------------------------------------------------------------- incubator
class IncubatorDevice(HardwareDevice):
    """Separate accessory interface (plan §6.2): the AMC1000 has its own controller."""

    @abstractmethod
    def read_telemetry(self) -> IncubatorTelemetry:
        """Only values exposed by the backend; a real backend reports only verified sensors."""

    @abstractmethod
    def get_supported_controls(self) -> dict[str, ControlSpec]: ...

    @abstractmethod
    def get_setpoints(self) -> dict[str, float]: ...

    @abstractmethod
    def set_setpoint(self, control: str, value: float) -> None: ...

    @abstractmethod
    def get_enabled(self) -> dict[str, bool]: ...

    @abstractmethod
    def set_enabled(self, control: str, enabled: bool) -> None: ...

    def halt(self) -> None:
        """Deliberately does nothing: a motion/light stop must not switch off environmental
        control, because that would harm the samples. Incubator control is changed only by an
        explicit, confirmed operator command."""


#: Read-only methods that `CoreGateway.read()` exposes for each interface.
_COMMON_READS = ("identify", "get_status", "is_busy")
READ_METHODS: dict[type, tuple[str, ...]] = {
    CameraDevice: _COMMON_READS + ("get_capabilities", "get_exposure_ms", "get_gain_db",
                                   "is_live", "get_latest_frame"),
    XYStageDevice: _COMMON_READS + ("get_position_um", "get_limits_um", "is_homed"),
    FocusDevice: _COMMON_READS + ("get_position_um", "get_limits_um", "is_homed"),
    ObjectiveTurretDevice: _COMMON_READS + ("get_positions", "get_position", "get_objectives",
                                            "current_objective"),
    PhaseTurretDevice: _COMMON_READS + ("get_positions", "get_position"),
    IlluminationDevice: _COMMON_READS + ("get_channels", "get_led_states", "get_carriage_positions",
                                         "get_carriage_position"),
    IncubatorDevice: _COMMON_READS + ("read_telemetry", "get_supported_controls", "get_setpoints",
                                      "get_enabled"),
}

#: Interface each role must implement.
ROLE_INTERFACE: dict[Role, type] = {  # abstract interfaces: `type`, as READ_METHODS (type[...] wants a concrete class)
    Role.CAMERA_MONO: CameraDevice,
    Role.CAMERA_COLOR: CameraDevice,
    Role.XY_STAGE: XYStageDevice,
    Role.FOCUS: FocusDevice,
    Role.OBJECTIVE_TURRET: ObjectiveTurretDevice,
    Role.PHASE_TURRET: PhaseTurretDevice,
    Role.ILLUMINATION: IlluminationDevice,
    Role.INCUBATOR: IncubatorDevice,
}


def read_methods_for(device: HardwareDevice) -> tuple[str, ...]:
    for iface, names in READ_METHODS.items():
        if isinstance(device, iface):
            return names
    return _COMMON_READS
