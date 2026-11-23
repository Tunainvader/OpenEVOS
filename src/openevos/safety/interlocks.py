"""Resource-based interlocks (handoff §15.3, §16.4).

Each command declares the resources it needs *exclusively* (it changes them) and the resources
it needs to stay *stable* (shared). The scheduler reserves both atomically with validation.
Long-running work such as an acquisition holds shared claims on the optical path, so a stage
move (exclusive on the stage) cannot start while frames that need a fixed position are taken.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from openevos.core.models import (
    Activity,
    Command,
    DeviceSnapshot,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    SelectCarriagePosition,
    SetIllumination,
    SetTurretPosition,
    SnapImage,
)

World = Mapping[str, DeviceSnapshot]


class Interlock(Protocol):
    """A cross-device rule. It reads only store snapshots (observed state), never a device."""

    @property
    def name(self) -> str: ...  # read-only, so a frozen dataclass's field satisfies it

    def check(self, command: Command, world: World) -> list[str]: ...


class HomedBeforeMotion:
    """Absolute motion needs a homed axis. A backend that reports `homed=False` blocks motion;
    one that reports nothing about homing (e.g. a drive without reference switches) is not
    blocked by this rule, the axis capability's `calibration_valid` covers it instead."""

    name = "homed"

    def check(self, command: Command, world: World) -> list[str]:
        if not isinstance(command, (MoveStageAbsolute, MoveFocusAbsolute)):
            return []
        snap = world.get(command.device_id)
        if snap is not None and snap.attr("homed") is False:
            return [f"{command.device_id} is not homed; home it first"]
        return []


class NotLiveForSnap:
    name = "not_live"

    def check(self, command: Command, world: World) -> list[str]:
        if isinstance(command, SnapImage):
            snap = world.get(command.device_id)
            if snap is not None and snap.attr("live") is not False:
                return ["camera is streaming live (or its live state is unknown); stop live view first"]
        return []


@dataclass(frozen=True)
class InstrumentInterlocks:
    """Rules between devices of one instrument. Values are instrument-profile data, not code."""

    name: str = "instrument"
    focus_id: str | None = None
    objective_turret_id: str | None = None
    illumination_id: str | None = None
    turret_safe_z: float | None = None  # in the focus axis unit
    position_tolerance: float = 0.5
    #: LED channel -> carriage slot whose cube carries it (None = transmitted light).
    channel_slots: tuple[tuple[str, int | None], ...] = field(default_factory=tuple)

    def check(self, command: Command, world: World) -> list[str]:
        if isinstance(command, SetTurretPosition) and command.device_id == self.objective_turret_id:
            turret = world.get(self.objective_turret_id)
            here = turret.observed_position if turret is not None else None
            if here is not None and here.values and int(here.values[0]) == command.position:
                return []  # already there: the turret does not turn
            return self._focus_clear(world)
        if isinstance(command, SelectCarriagePosition) and command.device_id == self.illumination_id:
            return self._epi_leds_off(world.get(command.device_id))
        if isinstance(command, SetIllumination) and command.enabled and command.device_id == self.illumination_id:
            return self._cube_in_path(command.channel, world.get(command.device_id))
        return []

    def _focus_clear(self, world: World) -> list[str]:
        focus = world.get(self.focus_id) if self.focus_id else None
        if focus is None or self.turret_safe_z is None:
            return ["no focus device or turret-safe height: cannot confirm the objective is clear of the vessel"]
        if focus.activity is not Activity.IDLE:
            return [f"focus is {focus.activity.value}; wait until it is idle"]
        pos = focus.observed_position
        if pos is None or "z" not in pos.axes:
            return ["focus position is unknown; cannot confirm the objective is clear of the vessel"]
        z = pos.get("z")
        if z > self.turret_safe_z + self.position_tolerance:
            return [(f"focus Z {z:.1f} {pos.unit} is above the turret-safe height {self.turret_safe_z:g} "
                     f"{pos.unit}; lower it first")]
        return []

    def _epi_slots(self) -> dict[str, int]:
        return {c: s for c, s in self.channel_slots if s is not None}

    def _epi_leds_off(self, snap: DeviceSnapshot | None) -> list[str]:
        on = snap.attr("leds_on") if snap is not None else None
        if on is None:
            return ["LED state is unknown; cannot confirm the epi LEDs are off"]
        epi_on = [c for c in on if c in self._epi_slots()]
        if epi_on:
            return [f"epi LED(s) {', '.join(epi_on)} are on; switch them off before moving the carriage"]
        return []

    def _cube_in_path(self, channel: str, snap: DeviceSnapshot | None) -> list[str]:
        slot = self._epi_slots().get(channel)
        if slot is None:
            return []  # transmitted light: no cube needed
        carriage = snap.attr("carriage_position") if snap is not None else None
        if carriage != slot:
            return [(f"{channel} sits in carriage slot {slot} but slot {carriage} is in the light path; "
                     "select that cube first")]
        return []


@dataclass(frozen=True)
class FocusSafeHeight:
    """The objective's safe height (audit APP-03): an upward focus move past it is refused unless the command is
    confirmed by the operator (`confirmed=True`, recorded with the command). Moving down is always allowed.
    `limit_for()` returns (objective name, safe height in the focus unit) for the objective in the light path, or
    None when no height is set."""

    focus_id: str
    limit_for: Callable[[], tuple[str, float] | None]
    tolerance: float = 0.5
    name: str = "focus_safe_height"

    def check(self, command: Command, world: World) -> list[str]:
        if not isinstance(command, MoveFocusAbsolute) or command.device_id != self.focus_id or command.confirmed:
            return []
        lim = self.limit_for()
        if lim is None:
            return []
        objective, safe = lim
        if command.z <= safe + self.tolerance:
            return []
        snap = world.get(self.focus_id)
        pos = snap.observed_position if snap is not None else None
        if pos is not None and "z" in pos.axes and command.z <= pos.get("z") + self.tolerance:
            return []  # not upward: never block a move back toward safety
        return [(f"Z {command.z:,.1f} µm is above the {objective} safe height ({safe:,.1f} µm), where the objective "
                 "nears the vessel; confirm the move to go higher")]


DEFAULT_INTERLOCKS: tuple[Interlock, ...] = (HomedBeforeMotion(), NotLiveForSnap())


@dataclass(frozen=True)
class ResourceClaim:
    exclusive: frozenset[str] = frozenset()
    shared: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        overlap = self.exclusive & self.shared
        if overlap:
            raise ValueError(f"resource both exclusive and shared: {sorted(overlap)}")


def resource(device_id: str) -> str:
    return f"device:{device_id}"


def claim_for_command(command: Command) -> ResourceClaim:
    """Default: a command exclusively owns its target device for its whole lifetime."""
    return ResourceClaim(exclusive=frozenset({resource(command.device_id)}))


def claim_for_acquisition(camera_id: str, stable_device_ids: list[str]) -> ResourceClaim:
    """Acquisition: camera exclusive; stage/focus/turret/illumination must stay stable."""
    return ResourceClaim(
        exclusive=frozenset({resource(camera_id)}),
        shared=frozenset(resource(d) for d in stable_device_ids if d != camera_id),
    )
