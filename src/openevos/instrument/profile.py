"""Instrument profile: dual cameras, 4 cube slots + transmitted/phase paths, 5 objective slots.

Manual basis (MAN0007986): dual camera (monochrome CCD for fluorescence + brightfield, colour
CMOS for brightfield/phase only) p.9, p.56; up to four LED light cubes plus transmitted light
p.8, p.59; five-position turret p.56; objective profile per turret slot set on the Service tab
p.43, p.52. Real cube identities and objectives on the project unit come from evidence (LED
board cube detection, recovered `lico/tpco` objective records); until then they are unverified.
"""

from __future__ import annotations

import json
from enum import Enum
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provenance(str, Enum):
    SIMULATED = "simulated"  # simulator configuration
    DOCUMENTED = "documented"  # from the manual / product documentation
    RECOVERED = "recovered"  # read from vendor files or instrument memory (candidate)
    OPERATOR = "operator"  # entered/confirmed by an operator
    VERIFIED = "verified"  # established on this unit with evidence IDs
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class CameraRole(str, Enum):
    MONO = "mono"
    COLOR = "color"


class ChannelKind(str, Enum):
    FLUORESCENCE = "fluorescence"
    TRANSMITTED = "transmitted"  # brightfield via condenser
    PHASE = "phase"  # transmitted with phase annulus


class CameraProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: CameraRole
    device_id: str
    display_name: str
    sensor_hint: str = ""  # e.g. "interline CCD" (manual) — not a model claim
    pixel_size_um: float | None = None  # None => physical scale unknown
    width: int | None = None
    height: int | None = None
    native_bit_depth: int | None = None
    provenance: Provenance = Provenance.UNKNOWN

    @property
    def supports_fluorescence(self) -> bool:
        return self.role is CameraRole.MONO  # manual p.9: colour camera is brightfield/phase only

    def supports(self, channel: ChannelSlot) -> bool:
        return channel.installed and (channel.kind is not ChannelKind.FLUORESCENCE or self.supports_fluorescence)


class ChannelSlot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: str  # "cube1".."cube4", "trans", "phase"
    kind: ChannelKind
    label: str  # e.g. "GFP"; empty slot => label "" and installed False
    installed: bool = True
    pseudocolor: str = "#FFFFFF"  # default display colour; user override lives in settings
    excitation_nm: int | None = None
    emission_nm: int | None = None
    provenance: Provenance = Provenance.UNKNOWN

    @property
    def display(self) -> str:
        return self.label if self.installed else "Empty"


class ObjectiveSlot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: int = Field(ge=1, le=5)
    installed: bool = True
    label: str = ""  # e.g. "10x Plan Fluor"
    magnification: float | None = None
    numerical_aperture: float | None = None
    immersion: str | None = None  # "air" | "oil" | "water"
    correction: str | None = None  # "LWD" | "CS" (cover-slip corrected) — affects calibration slide orientation
    phase_ring: str | None = None
    calibration_valid: bool = False
    provenance: Provenance = Provenance.UNKNOWN

    @property
    def short(self) -> str:
        if not self.installed:
            return "—"
        return f"{self.magnification:g}x" if self.magnification else f"#{self.slot}"


class InstrumentProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    model: str = "AMAFD1000"
    backend: str  # "simulation" | "physical"
    cameras: tuple[CameraProfile, ...]
    channels: tuple[ChannelSlot, ...]
    objectives: tuple[ObjectiveSlot, ...]
    stage_device: str
    focus_device: str
    turret_device: str | None = None
    illumination_device: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _shape(self) -> InstrumentProfile:
        cubes = [c for c in self.channels if c.slot.startswith("cube")]
        if len(cubes) > 4:
            raise ValueError("the FL Auto accepts at most four light cubes")
        if sorted(o.slot for o in self.objectives) != list(range(1, len(self.objectives) + 1)) or len(self.objectives) > 5:
            raise ValueError("objective slots must be 1..n with n <= 5")
        if len({c.role for c in self.cameras}) != len(self.cameras):
            raise ValueError("at most one camera per role")
        return self

    def camera(self, role: CameraRole) -> CameraProfile | None:
        return next((c for c in self.cameras if c.role is role), None)

    def channel(self, slot_or_label: str) -> ChannelSlot | None:
        return next((c for c in self.channels if slot_or_label in (c.slot, c.label)), None)

    def objective(self, slot: int) -> ObjectiveSlot | None:
        return next((o for o in self.objectives if o.slot == slot), None)

    def channels_for(self, role: CameraRole) -> list[ChannelSlot]:
        cam = self.camera(role)
        return [c for c in self.channels if cam is not None and cam.supports(c)]


def load_profile(path: Path | None = None) -> InstrumentProfile:
    if path is None:
        text = resources.files("openevos.instrument.data").joinpath("sim_instrument_profile.json").read_text("utf-8")
    else:
        text = Path(path).read_text("utf-8")
    return InstrumentProfile.model_validate(json.loads(text))
