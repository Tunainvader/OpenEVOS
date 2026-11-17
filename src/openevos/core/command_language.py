"""The named-command language the controller and UI speak, and the reference catalogue.

This module decides nothing about safety. It defines:

* operational modes, actors and risk classes (plan §13.1, §13.3);
* the exceptions the command port raises (`CommandRejected` with a `check`, `CommandFailed`, ...);
* explicit units (`Quantity`) and their conversion to canonical units;
* `CommandSpec` / `ParamSpec`: each named operation's parameters (types, canonical units, static
  bounds such as the largest jog step), its target role, timeout, confirmation flag and the
  human-readable interlock/readback/abort descriptions shown on the Devices page.

Every admission decision (device state, firmware mode, limits from capability records,
confirmation, interlocks on observed state, reservations, STOP latch) is made by the command
core (`core.command_router` → `safety.command_validator`), see ADR-0008 and ADR-0010.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, NamedTuple

from openevos.hal.types import Role


# ----------------------------------------------------------------------------------- modes
class OperationalMode(str, Enum):
    """Plan §13.1."""

    INVENTORY = "inventory"
    SIMULATION = "simulation"
    CONNECTED_READ_ONLY = "connected_read_only"
    VALIDATED_CONTROL = "validated_control"
    MAINTENANCE = "maintenance"

    @property
    def display(self) -> str:
        return self.value.replace("_", " ").capitalize()


#: Modes this build can enter. Validated control and maintenance need validated real backends,
#: which do not exist yet, so they are refused outright.
AVAILABLE_MODES: frozenset[OperationalMode] = frozenset({
    OperationalMode.INVENTORY,
    OperationalMode.SIMULATION,
    OperationalMode.CONNECTED_READ_ONLY,
})


class Actor(str, Enum):
    OPERATOR = "operator"  # a person using the UI
    ACQUISITION = "acquisition"  # the acquisition engine running an operator-approved plan
    SYSTEM = "system"  # start-up / shut-down sequences
    AI = "ai"  # an AI assistant: may read, may never command (plan §13.3)


class Risk(str, Enum):
    CONFIGURATION = "configuration"  # camera exposure/gain
    ACQUISITION = "acquisition"  # snap / live
    MOTION = "motion"
    ILLUMINATION = "illumination"
    ENVIRONMENT = "environment"  # incubator setpoints


# ----------------------------------------------------------------------------------- errors
class PolicyError(RuntimeError):
    pass


class ModeNotAvailable(PolicyError):
    pass


class CommandRejected(PolicyError):
    """The command was refused. Nothing was sent to the device. `check` says which rule."""

    def __init__(self, command: str, reason: str, check: str = ""):
        super().__init__(f"{command}: {reason}")
        self.command = command
        self.reason = reason
        self.check = check


class CommandFailed(RuntimeError):
    """The command was admitted but the device failed, or readback did not match."""

    def __init__(self, command: str, reason: str):
        super().__init__(f"{command}: {reason}")
        self.command = command
        self.reason = reason


class CommandTimeout(CommandFailed):
    pass


class CommandAborted(CommandFailed):
    """A STOP (or cancellation) ended the command before it completed."""


# ----------------------------------------------------------------------------------- units
class Quantity(NamedTuple):
    """A value with an explicit unit, e.g. Quantity(1.5, "mm"). Converted to the canonical unit."""

    value: float
    unit: str


# unit -> (canonical unit, scale, offset): canonical = value * scale + offset
_UNITS: dict[str, tuple[str, float, float]] = {
    "um": ("um", 1.0, 0.0), "µm": ("um", 1.0, 0.0), "micron": ("um", 1.0, 0.0),
    "nm": ("um", 1e-3, 0.0), "mm": ("um", 1e3, 0.0), "m": ("um", 1e6, 0.0),
    "ms": ("ms", 1.0, 0.0), "s": ("ms", 1e3, 0.0), "us": ("ms", 1e-3, 0.0), "µs": ("ms", 1e-3, 0.0),
    "dB": ("dB", 1.0, 0.0), "db": ("dB", 1.0, 0.0),
    "%": ("%", 1.0, 0.0), "pct": ("%", 1.0, 0.0),
    "degC": ("degC", 1.0, 0.0), "°C": ("degC", 1.0, 0.0), "C": ("degC", 1.0, 0.0),
    "K": ("degC", 1.0, -273.15),
}

UNIT_SYMBOL = {"um": "µm", "ms": "ms", "dB": "dB", "%": "%", "degC": "°C"}


def convert_to(value: Quantity, canonical: str) -> float:
    entry = _UNITS.get(value.unit)
    if entry is None:
        raise ValueError(f"unknown unit '{value.unit}'")
    unit, scale, offset = entry
    if unit != canonical:
        raise ValueError(f"unit '{value.unit}' is not convertible to '{canonical}'")
    return float(value.value) * scale + offset


# ----------------------------------------------------------------------------------- specs
@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: type  # float | int | bool | str
    unit: str | None = None  # canonical unit for numeric params
    minimum: float | None = None  # static bounds only (e.g. the largest jog step)
    maximum: float | None = None
    device_range: bool = False  # the device's capability record bounds it (enforced by the core)
    required: bool = True
    default: Any = None
    doc: str = ""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    risk: Risk
    role: Role | None = None  # fixed target role ...
    role_param: str | None = None  # ... or the parameter that names it (cameras)
    params: tuple[ParamSpec, ...] = ()
    timeout_s: float = 5.0
    requires_confirmation: bool = False
    #: names of the core rules that guard it (display only; the core enforces them)
    interlocks: tuple[str, ...] = ()
    #: makes the instrument safer (stop, LEDs off, live off): admitted during STOP and reservations
    safe_stop: bool = False
    readback: str = ""
    abort_behavior: str = "halt the target device"
    core_kind: str = ""  # the typed core command it becomes ("" = handled by the router directly)

    def __post_init__(self) -> None:
        if (self.role is None) == (self.role_param is None):
            raise ValueError(f"{self.name}: exactly one of role / role_param is required")


def coerce_param(p: ParamSpec, value: Any) -> Any:
    """Type-check and unit-convert one parameter. Raises TypeError/ValueError with a reason."""
    if p.kind is bool:
        if not isinstance(value, bool):
            raise TypeError(f"expected true/false, got {type(value).__name__}")
        return value
    if p.kind is str:
        if not isinstance(value, str):
            raise TypeError(f"expected text, got {type(value).__name__}")
        return value
    if isinstance(value, tuple) and not isinstance(value, Quantity) and len(value) == 2:
        value = Quantity(*value)
    if isinstance(value, Quantity):
        if p.unit is None:
            raise ValueError("this parameter has no unit")
        value = convert_to(value, p.unit)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    if p.kind is int:
        if float(value) != int(value):
            raise ValueError(f"expected a whole number, got {value}")
        return int(value)
    return float(value)


# ----------------------------------------------------------------------------------- catalogue
POSITION_TOLERANCE_UM = 0.5

#: Instrument-level limits. SIMULATION defaults, not measured values; a real profile must
#: replace them with validated numbers.
DEFAULT_LIMITS: dict[str, float] = {
    "turret_safe_z_um": 1500.0,  # focus must be at or below this before the objective turret turns
    "max_focus_step_um": 2000.0,  # largest single relative Z step
    "max_xy_step_um": 50000.0,  # largest single relative XY step
}

_MOVE_READBACK = f"position within {POSITION_TOLERANCE_UM} µm of target"
_MOTION_RULES = ("device idle", "homed", "travel limits")


def _setpoint(name: str, param: str, unit: str, summary: str, control: str) -> CommandSpec:
    return CommandSpec(name=name, summary=summary, risk=Risk.ENVIRONMENT, role=Role.INCUBATOR,
                       params=(ParamSpec(param, float, unit, device_range=True),),
                       requires_confirmation=True, timeout_s=5.0, interlocks=(f"{control} setpoint range",),
                       readback="setpoint echo from the controller", abort_behavior="none (setpoint write)",
                       core_kind="incubator.set_setpoint")


def build_catalog() -> dict[str, CommandSpec]:
    xy_step, z_step = DEFAULT_LIMITS["max_xy_step_um"], DEFAULT_LIMITS["max_focus_step_um"]
    specs = [
        CommandSpec("xy.move_absolute", "Move the XY stage to an absolute position", Risk.MOTION, Role.XY_STAGE,
                    params=(ParamSpec("x_um", float, "um", device_range=True),
                            ParamSpec("y_um", float, "um", device_range=True)),
                    timeout_s=30.0, interlocks=_MOTION_RULES, readback=_MOVE_READBACK,
                    core_kind="stage.move_absolute"),
        CommandSpec("xy.move_relative", "Jog the XY stage by a relative step", Risk.MOTION, Role.XY_STAGE,
                    params=(ParamSpec("dx_um", float, "um", -xy_step, xy_step),
                            ParamSpec("dy_um", float, "um", -xy_step, xy_step)),
                    timeout_s=30.0, interlocks=(*_MOTION_RULES, "resolved from observed position"),
                    readback=_MOVE_READBACK, core_kind="stage.move_absolute"),
        CommandSpec("xy.stop", "Stop XY motion", Risk.MOTION, Role.XY_STAGE, safe_stop=True, timeout_s=1.0,
                    abort_behavior="n/a (this is the abort)"),
        CommandSpec("xy.home", "Home the XY stage (finds the reference switches)", Risk.MOTION, Role.XY_STAGE,
                    requires_confirmation=True, timeout_s=60.0, interlocks=("device idle",),
                    readback="homed flag", core_kind="stage.home"),
        CommandSpec("focus.move_absolute", "Move focus (Z) to an absolute position", Risk.MOTION, Role.FOCUS,
                    params=(ParamSpec("z_um", float, "um", device_range=True),), timeout_s=20.0,
                    interlocks=_MOTION_RULES, readback=_MOVE_READBACK, core_kind="focus.move_absolute"),
        CommandSpec("focus.move_relative", "Jog focus (Z) by a relative step", Risk.MOTION, Role.FOCUS,
                    params=(ParamSpec("dz_um", float, "um", -z_step, z_step),), timeout_s=20.0,
                    interlocks=(*_MOTION_RULES, "resolved from observed position"), readback=_MOVE_READBACK,
                    core_kind="focus.move_absolute"),
        CommandSpec("focus.stop", "Stop focus motion", Risk.MOTION, Role.FOCUS, safe_stop=True, timeout_s=1.0,
                    abort_behavior="n/a (this is the abort)"),
        CommandSpec("focus.home", "Home the focus drive", Risk.MOTION, Role.FOCUS, requires_confirmation=True,
                    timeout_s=60.0, interlocks=("device idle",), readback="homed flag", core_kind="focus.home"),
        CommandSpec("objective.select", "Rotate the objective turret to a position", Risk.MOTION,
                    Role.OBJECTIVE_TURRET, params=(ParamSpec("position", int, device_range=True),), timeout_s=10.0,
                    interlocks=("device idle", "focus idle and at or below the turret-safe height"),
                    readback="turret position", core_kind="turret.set_position"),
        CommandSpec("phase.select", "Rotate the phase (condenser) turret to a position", Risk.MOTION,
                    Role.PHASE_TURRET, params=(ParamSpec("position", int, device_range=True),), timeout_s=10.0,
                    interlocks=("device idle",), readback="turret position", core_kind="turret.set_position"),
        CommandSpec("carriage.select", "Move the epi carriage to a cube slot or the mirror", Risk.MOTION,
                    Role.ILLUMINATION, params=(ParamSpec("position", int, device_range=True),), timeout_s=10.0,
                    interlocks=("device idle", "epi LEDs off"), readback="carriage position",
                    core_kind="illumination.select_carriage"),
        CommandSpec("led.set", "Switch an LED channel and set its intensity", Risk.ILLUMINATION, Role.ILLUMINATION,
                    params=(ParamSpec("channel_id", str, device_range=True), ParamSpec("enabled", bool),
                            ParamSpec("intensity_pct", float, "%", 0.0, 100.0, device_range=True)),
                    timeout_s=1.0, interlocks=("known channel", "its cube in the light path"),
                    readback="LED state echo", abort_behavior="all LEDs off", core_kind="illumination.set"),
        CommandSpec("led.all_off", "Switch every LED off", Risk.ILLUMINATION, Role.ILLUMINATION, safe_stop=True,
                    timeout_s=1.0, readback="every LED reads off", abort_behavior="n/a (this is the abort)",
                    core_kind="illumination.all_off"),
        CommandSpec("camera.set_exposure", "Set camera exposure time", Risk.CONFIGURATION, role_param="camera",
                    params=(ParamSpec("exposure_ms", float, "ms", device_range=True),), timeout_s=2.0,
                    readback="exposure echo", core_kind="camera.set_exposure"),
        CommandSpec("camera.set_gain", "Set camera analogue gain", Risk.CONFIGURATION, role_param="camera",
                    params=(ParamSpec("gain_db", float, "dB", device_range=True),), timeout_s=2.0,
                    readback="gain echo", core_kind="camera.set_gain"),
        CommandSpec("camera.snap", "Acquire one frame", Risk.ACQUISITION, role_param="camera", timeout_s=30.0,
                    interlocks=("device idle", "not streaming live"), readback="frame returned",
                    abort_behavior="discard frame", core_kind="camera.snap"),
        CommandSpec("camera.start_live", "Start continuous (live) acquisition", Risk.ACQUISITION,
                    role_param="camera", timeout_s=2.0, readback="live state echo", abort_behavior="stop streaming",
                    core_kind="camera.set_live"),
        CommandSpec("camera.stop_live", "Stop live acquisition", Risk.ACQUISITION, role_param="camera",
                    safe_stop=True, timeout_s=2.0, readback="live state echo",
                    abort_behavior="n/a (this is the abort)", core_kind="camera.set_live"),
        _setpoint("incubator.set_temperature", "temperature_c", "degC", "Set the chamber temperature setpoint",
                  "temperature"),
        _setpoint("incubator.set_co2", "co2_pct", "%", "Set the CO₂ setpoint", "co2"),
        _setpoint("incubator.set_o2", "o2_pct", "%", "Set the O₂ setpoint", "o2"),
        CommandSpec("incubator.set_enabled", "Enable or disable one incubator control loop", Risk.ENVIRONMENT,
                    Role.INCUBATOR, params=(ParamSpec("control", str, device_range=True), ParamSpec("enabled", bool)),
                    requires_confirmation=True, timeout_s=5.0, interlocks=("supported control",),
                    readback="enable flag echo", abort_behavior="none (flag write)",
                    core_kind="incubator.set_enabled"),
    ]
    from openevos.safety.firmware import operation_refusal

    for s in specs:  # ADR-0009: the catalogue can never contain a firmware operation
        if operation_refusal(s.name):
            raise RuntimeError(f"command {s.name!r} is a firmware operation; banned by ADR-0009")
    return {s.name: s for s in specs}
