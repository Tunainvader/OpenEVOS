"""Fail-closed command validation (handoff §15).

`SafetyManager.validate()` collects every reason a command must be rejected. A command is
allowed only if *every* required fact is known and acceptable: identity, connection, health,
activity, recovery state, firmware mode, capability, units, coordinate frame, calibration,
verified limits and parameter ranges, operator confirmation, and the cross-device interlocks.
Resource availability and the STOP latch are checked separately, atomically, by the router.

Each reason carries a short check code ("device_state", "limits", "interlock:instrument", …), so
callers can tell *why* without parsing text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from openevos.core.models import (
    COMMAND_DEVICE_KIND,
    CONFIRMATION_REQUIRED,
    EXTRA_ADMISSIBLE_ACTIVITY,
    Activity,
    BackendKind,
    Command,
    CommandOrigin,
    Connection,
    DeviceSnapshot,
    Health,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    SelectCarriagePosition,
    SetCameraExposure,
    SetCameraGain,
    SetIllumination,
    SetIncubatorControlEnabled,
    SetIncubatorSetpoint,
    SetTurretPosition,
    is_safe_direction,
)
from openevos.safety.capabilities import AxisLimits, CapabilitySource, DeviceCapabilities
from openevos.safety.firmware import FirmwareModeRefusal, operation_refusal
from openevos.safety.interlocks import DEFAULT_INTERLOCKS, Interlock
from openevos.safety.restrictions import HealthRestrictionInterlock, RestrictionRegistry


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()  # one code per reason, same order

    @property
    def message(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "allowed"

    @property
    def first_check(self) -> str | None:
        return self.checks[0] if self.checks else None


class _Reasons:
    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.checks: list[str] = []

    def add(self, check: str, reason: str) -> None:
        self.reasons.append(reason)
        self.checks.append(check)

    def extend(self, check: str, reasons: Sequence[str]) -> None:
        for r in reasons:
            self.add(check, r)

    def decision(self) -> Decision:
        return Decision(not self.reasons, tuple(self.reasons), tuple(self.checks))


class SafetyManager:
    def __init__(self, interlocks: Sequence[Interlock] = DEFAULT_INTERLOCKS):
        self.interlocks: list[Interlock] = list(interlocks)
        #: restrictions the operator confirmed from Health (ADR-0070); enforced by an interlock and by run_guard
        self.restrictions = RestrictionRegistry()
        self.interlocks.append(HealthRestrictionInterlock(self.restrictions))

    def add_interlock(self, interlock: Interlock) -> None:
        self.interlocks.append(interlock)

    def validate(self, command: Command, capabilities: DeviceCapabilities | None,
                 snapshot: DeviceSnapshot | None,
                 world: Mapping[str, DeviceSnapshot] | None = None) -> Decision:
        r = _Reasons()
        if command.origin is CommandOrigin.AI:
            r.add("actor", "AI-originated commands are not executable (AI is advisory, ADR-0003)")
        refusal = operation_refusal(command.kind)  # defence in depth; parse_command refuses first
        if refusal:
            r.add("firmware", refusal)
        if capabilities is None or snapshot is None:
            r.add("device", f"device {command.device_id!r} is not registered")
            return r.decision()
        if capabilities.device_id != command.device_id or snapshot.device_id != command.device_id:
            r.add("device", "device identity mismatch")
            return r.decision()

        expected_kind = COMMAND_DEVICE_KIND.get(command.kind)
        if expected_kind is None or snapshot.kind is not expected_kind:
            r.add("kind", f"command {command.kind} does not apply to a {snapshot.kind.value} device")
        if command.kind not in capabilities.supported_commands:
            r.add("capability", f"capability '{command.kind}' is not supported by this device")

        # runtime state — every axis must be known and acceptable. Safe-direction commands
        # (light off, live off) only need the device connected: making it safer is always allowed.
        safe = is_safe_direction(command)
        if snapshot.connection is not Connection.CONNECTED:
            r.add("device_state", f"device is not connected (connection={snapshot.connection.value})")
        if not safe:
            if snapshot.health is not Health.NORMAL:
                why = snapshot.attr("health_reason")  # a backend may say why (e.g. the stage drift lock, ADR-0040)
                r.add("device_state", f"device health is {snapshot.health.value}, not normal"
                      + (f": {why}" if isinstance(why, str) and why else ""))
            admissible = {Activity.IDLE} | EXTRA_ADMISSIBLE_ACTIVITY.get(command.kind, frozenset())
            if snapshot.activity not in admissible:
                r.add("device_state", f"device activity is {snapshot.activity.value}, not idle")
            if snapshot.recovery_required:
                r.add("recovery", "device requires recovery before new commands")
        r.extend("firmware", FirmwareModeRefusal.reasons(snapshot))  # bootloader quarantine (ADR-0009)

        # provenance of the capability record
        if snapshot.backend_kind is not capabilities.backend_kind:
            r.add("provenance", "capability record backend kind does not match the device")
        if snapshot.backend_kind is BackendKind.PHYSICAL:
            if capabilities.source is not CapabilitySource.VERIFIED:
                r.add("provenance", f"physical device capabilities are '{capabilities.source.value}', not verified")
            if not capabilities.evidence_ids:
                r.add("provenance", "physical device capabilities cite no evidence")
        elif capabilities.source is not CapabilitySource.SIMULATED:
            r.add("provenance", "simulated device must use simulated capabilities")

        # operator confirmation
        if command.kind in CONFIRMATION_REQUIRED and not command.confirmed:
            r.add("confirmation", "operator confirmation required")

        # command-specific checks
        r.extend("limits", self._parameters(command, capabilities))

        # cross-device interlocks, against observed state only
        if not safe:
            view = world if world is not None else {snapshot.device_id: snapshot}
            for lock in self.interlocks:
                r.extend(f"interlock:{lock.name}", lock.check(command, view))
        return r.decision()

    # ------------------------------------------------------------------ parameters
    def _parameters(self, command: Command, caps: DeviceCapabilities) -> list[str]:
        if isinstance(command, (MoveStageAbsolute, MoveFocusAbsolute)):
            return self._check_motion(command, caps)
        if isinstance(command, SetTurretPosition):
            if command.position not in caps.turret_positions:
                return [f"turret position {command.position} is not a known position"]
        elif isinstance(command, SelectCarriagePosition):
            if command.position not in caps.carriage_positions:
                return [f"carriage position {command.position} is not a known position"]
        elif isinstance(command, SetIllumination):
            if command.channel not in caps.illumination_channels:
                return [f"illumination channel {command.channel!r} is not a known channel"]
            lim = caps.range(f"intensity:{command.channel}")
            if command.enabled or lim is not None:  # switching off needs no known range
                return _in_range("intensity", command.intensity_percent, lim, "%")
        elif isinstance(command, SetCameraExposure):
            return _in_range("exposure", command.exposure_ms, caps.range("exposure_ms"), "ms")
        elif isinstance(command, SetCameraGain):
            return _in_range("gain", command.gain_db, caps.range("gain_db"), "dB")
        elif isinstance(command, (SetIncubatorSetpoint, SetIncubatorControlEnabled)):
            if command.control not in caps.incubator_controls:
                return [f"incubator control {command.control!r} is not supported"]
            if isinstance(command, SetIncubatorSetpoint):
                return _in_range(f"{command.control} setpoint", command.value,
                                 caps.range(f"setpoint:{command.control}"), None)
        return []

    @staticmethod
    def _check_motion(command: MoveStageAbsolute | MoveFocusAbsolute, caps: DeviceCapabilities) -> list[str]:
        r: list[str] = []
        target = command.target()
        for axis, value in target.as_dict().items():
            ax = caps.axis(axis)
            if ax is None:
                r.append(f"axis {axis} is not described in the capability record")
                continue
            if ax.limits is None:
                r.append(f"axis {axis} has no known limits")
                continue
            if command.unit != ax.limits.unit:
                r.append(f"axis {axis}: unit {command.unit!r} does not match capability unit {ax.limits.unit!r} (no implicit conversion)")  # noqa: E501
            if ax.coordinate_frame is None or command.frame != ax.coordinate_frame:
                r.append(f"axis {axis}: coordinate frame {command.frame!r} does not match {ax.coordinate_frame!r}")
            if not ax.calibration_valid:
                r.append(f"axis {axis} calibration is not valid")
            if ax.readback_tolerance is None:
                r.append(f"axis {axis} has no readback tolerance, so completion cannot be verified")
            if command.unit == ax.limits.unit and not ax.limits.contains(value):
                r.append(f"axis {axis}: target {value} outside [{ax.limits.minimum}, {ax.limits.maximum}] {ax.limits.unit}")  # noqa: E501
        return r


def _in_range(what: str, value: float, lim: AxisLimits | None, unit: str | None) -> list[str]:
    if lim is None:
        return [f"{what} has no known permitted range"]
    if unit is not None and lim.unit != unit:
        return [f"{what}: capability range is in {lim.unit!r}, command in {unit!r} (no implicit conversion)"]
    if not lim.contains(value):
        return [f"{what} {value:g} {lim.unit} is outside the permitted range [{lim.minimum:g}, {lim.maximum:g}] {lim.unit}"]  # noqa: E501
    return []
