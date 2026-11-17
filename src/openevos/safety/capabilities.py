"""Capability and operation-limit records (handoff §15.1).

Limits never live in code. Each device has a `DeviceCapabilities` record stating units,
coordinate frame, limits, calibration status and *where the record came from*:

* SIMULATED — simulator values. Only valid for simulated backends (enforced at construction).
* CANDIDATE — from documentation or vendor software (e.g. a recovered PearlScope.ini). Useful
  for planning and display; never sufficient to command physical hardware.
* VERIFIED  — established on our unit with Inspector/test evidence IDs. Required for any physical
  command.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevos.core.models import BackendKind, FiniteFloat


class CapabilitySource(str, Enum):
    SIMULATED = "simulated"
    CANDIDATE = "candidate"
    VERIFIED = "verified"


class AxisLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum: FiniteFloat
    maximum: FiniteFloat
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def _order(self) -> AxisLimits:
        if not self.minimum < self.maximum:
            raise ValueError("minimum must be < maximum")
        return self

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


class AxisCapability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    limits: AxisLimits | None = None  # None = unknown => motion rejected
    coordinate_frame: str | None = None
    calibration_valid: bool = False
    readback_tolerance: FiniteFloat | None = Field(default=None, ge=0)  # None => cannot verify => fail


class ParameterRange(BaseModel):
    """A permitted range for a scalar parameter, e.g. name="exposure_ms" or "setpoint:temperature"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    limits: AxisLimits


class DeviceCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    backend_kind: BackendKind
    source: CapabilitySource
    supported_commands: frozenset[str] = frozenset()
    axes: tuple[AxisCapability, ...] = ()
    turret_positions: tuple[int, ...] = ()
    illumination_channels: tuple[str, ...] = ()
    carriage_positions: tuple[int, ...] = ()
    incubator_controls: tuple[str, ...] = ()
    #: Scalar ranges: "exposure_ms", "gain_db", "intensity:<channel>", "setpoint:<control>".
    #: A command whose parameter has no range here is rejected (unknown limit => fail closed).
    ranges: tuple[ParameterRange, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _source_rules(self) -> DeviceCapabilities:
        if self.backend_kind is BackendKind.PHYSICAL and self.source is CapabilitySource.SIMULATED:
            raise ValueError("simulator capabilities cannot be attached to a physical backend")
        if self.backend_kind is BackendKind.SIMULATED and self.source is not CapabilitySource.SIMULATED:
            raise ValueError("a simulated backend must use simulated capabilities")
        if self.source is CapabilitySource.VERIFIED and not self.evidence_ids:
            raise ValueError("verified capabilities must cite evidence IDs")
        names = [a.name for a in self.axes]
        if len(set(names)) != len(names):
            raise ValueError("duplicate axis names")
        return self

    def axis(self, name: str) -> AxisCapability | None:
        return next((a for a in self.axes if a.name == name), None)

    def range(self, name: str) -> AxisLimits | None:
        return next((r.limits for r in self.ranges if r.name == name), None)
