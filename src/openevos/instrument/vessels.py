"""Vessel library and vessel <-> stage coordinate mapping.

Manual basis: Vessel Expert (category + type, a vessel MUST be selected before imaging) p.8;
virtual vessel with draggable crosshair, click a well to centre it p.8; Vessel Manager for user
vessels p.41; Capture Focus Nominal sets the Z point around which Find Sample and the Quick
slider are centred p.21/p.41; Calibrate Vessel Alignment p.50.

Geometry here is *nominal* (ANSI/SLAS footprint 127.76 x 85.48 mm, standard pitches). It is not
the vendor's vessel database and has not been measured on the project unit, so every built-in
vessel carries `geometry_verified=False`. Physical moves derived from vessel coordinates must
wait for a verified vessel-alignment calibration.

Coordinate frames
* vessel frame: millimetres, origin at the top-left corner of the vessel footprint, +x right,
  +y down (as drawn on the virtual vessel).
* stage frame: the stage device's frame (simulator: "sim-stage", micrometres, origin at the
  footprint centre). `VesselStageMap` converts; it holds an alignment offset for calibration.
"""

from __future__ import annotations

import math
import string
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from openevos.instrument.profile import Provenance

SBS_W, SBS_H = 127.76, 85.48


class VesselCategory(str, Enum):
    WELL_PLATE = "Well plate"
    SLIDE = "Slide"
    DISH = "Petri dish"
    FLASK = "Culture flask"
    CHAMBER_SLIDE = "Chamber slide"


class RegionShape(str, Enum):
    CIRCLE = "circle"
    RECT = "rect"


class Region(BaseModel):
    """One imageable region: a well, a slide, a dish or the growth area of a flask (vessel mm)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    region_id: str  # "A1", "Slide 2", "Dish"
    cx: float
    cy: float
    w: float
    h: float
    shape: RegionShape

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        if self.shape is RegionShape.CIRCLE:
            r = min(self.w, self.h) / 2 - margin
            return r > 0 and (x - self.cx) ** 2 + (y - self.cy) ** 2 <= r * r
        return abs(x - self.cx) <= self.w / 2 - margin and abs(y - self.cy) <= self.h / 2 - margin

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.cx - self.w / 2, self.cy - self.h / 2, self.cx + self.w / 2, self.cy + self.h / 2


class Vessel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vessel_id: str
    display_name: str
    category: VesselCategory
    width_mm: float = SBS_W
    height_mm: float = SBS_H
    regions: tuple[Region, ...]
    rows: int = 0  # well plates only
    cols: int = 0
    focus_nominal_um: float = Field(default=3000.0, description="Z around which Find Sample/Quick slider centre")
    focus_range_um: float = Field(default=800.0, gt=0, description="Find Sample search range")
    geometry_verified: bool = False
    provenance: Provenance = Provenance.DOCUMENTED
    notes: str = ""
    user_created: bool = False

    def region(self, region_id: str) -> Region | None:
        return next((r for r in self.regions if r.region_id == region_id), None)

    def region_at(self, x: float, y: float) -> Region | None:
        return next((r for r in self.regions if r.contains(x, y)), None)

    @property
    def is_plate(self) -> bool:
        return self.rows > 0 and self.cols > 0


def _plate(n: int, rows: int, cols: int, pitch: float, a1x: float, a1y: float, well_d: float,
           shape: RegionShape = RegionShape.CIRCLE, focus_nominal: float = 3000.0) -> Vessel:
    regions = []
    for r in range(rows):
        for c in range(cols):
            regions.append(Region(region_id=f"{_row_label(r)}{c + 1}", cx=a1x + c * pitch, cy=a1y + r * pitch,
                                  w=well_d, h=well_d, shape=shape))
    return Vessel(vessel_id=f"plate-{n}", display_name=f"{n}-well plate", category=VesselCategory.WELL_PLATE,
                  regions=tuple(regions), rows=rows, cols=cols, focus_nominal_um=focus_nominal,
                  notes="Nominal SLAS geometry; not measured on this unit.")


def _row_label(r: int) -> str:
    letters = string.ascii_uppercase
    return letters[r] if r < 26 else letters[r // 26 - 1] + letters[r % 26]


def builtin_vessels() -> list[Vessel]:
    v = [
        _plate(6, 2, 3, 39.12, 24.76, 23.16, 34.8),
        _plate(12, 3, 4, 26.01, 24.94, 16.68, 22.1),
        _plate(24, 4, 6, 19.30, 17.53, 13.84, 15.6),
        _plate(48, 6, 8, 13.08, 18.16, 10.08, 11.0),
        _plate(96, 8, 12, 9.00, 14.38, 11.24, 6.4),
        _plate(384, 16, 24, 4.50, 12.13, 8.99, 3.6, shape=RegionShape.RECT),
        Vessel(vessel_id="slide-single", display_name="Single slide (75 x 25 mm)", category=VesselCategory.SLIDE,
               regions=(Region(region_id="Slide", cx=SBS_W / 2, cy=SBS_H / 2, w=75, h=25, shape=RegionShape.RECT),),
               focus_nominal_um=2400.0),
        Vessel(vessel_id="slide-dual", display_name="Dual slide holder", category=VesselCategory.SLIDE,
               regions=(Region(region_id="Slide 1", cx=SBS_W / 2, cy=SBS_H / 2 - 17, w=75, h=25, shape=RegionShape.RECT),  # noqa: E501
                        Region(region_id="Slide 2", cx=SBS_W / 2, cy=SBS_H / 2 + 17, w=75, h=25, shape=RegionShape.RECT)),  # noqa: E501
               focus_nominal_um=2400.0),
        Vessel(vessel_id="dish-35", display_name="35 mm Petri dish", category=VesselCategory.DISH,
               regions=(Region(region_id="Dish", cx=SBS_W / 2, cy=SBS_H / 2, w=33, h=33, shape=RegionShape.CIRCLE),),
               focus_nominal_um=2800.0),
        Vessel(vessel_id="dish-60", display_name="60 mm Petri dish", category=VesselCategory.DISH,
               regions=(Region(region_id="Dish", cx=SBS_W / 2, cy=SBS_H / 2, w=52, h=52, shape=RegionShape.CIRCLE),),
               focus_nominal_um=2800.0),
        Vessel(vessel_id="flask-t25", display_name="T25 flask", category=VesselCategory.FLASK,
               regions=(Region(region_id="Flask", cx=SBS_W / 2 + 6, cy=SBS_H / 2, w=60, h=40, shape=RegionShape.RECT),),
               focus_nominal_um=3600.0, focus_range_um=1200.0),
        Vessel(vessel_id="chamber-8", display_name="8-chamber slide", category=VesselCategory.CHAMBER_SLIDE,
               regions=tuple(Region(region_id=f"Ch {i + 1}", cx=SBS_W / 2 - 16.5 + (i % 4) * 11,
                                    cy=SBS_H / 2 - 5 + (i // 4) * 10, w=9, h=8.5, shape=RegionShape.RECT)
                            for i in range(8)),
               focus_nominal_um=2400.0),
    ]
    return v


class VesselLibrary:
    """Built-in vessels plus user-created ones (Vessel Manager, manual p.41)."""

    def __init__(self, vessels: list[Vessel] | None = None):
        self._v: dict[str, Vessel] = {x.vessel_id: x for x in (vessels or builtin_vessels())}

    def all(self) -> list[Vessel]:
        return list(self._v.values())

    def categories(self) -> list[VesselCategory]:
        return [c for c in VesselCategory if any(v.category is c for v in self._v.values())]

    def by_category(self, cat: VesselCategory) -> list[Vessel]:
        return [v for v in self._v.values() if v.category is cat]

    def get(self, vessel_id: str) -> Vessel:
        return self._v[vessel_id]

    def add_user_vessel(self, base: Vessel, new_id: str, display_name: str, notes: str = "") -> Vessel:
        if new_id in self._v:
            raise ValueError(f"vessel id {new_id!r} already exists")
        v = base.model_copy(update={"vessel_id": new_id, "display_name": display_name, "notes": notes,
                                    "user_created": True, "provenance": Provenance.OPERATOR})
        self._v[new_id] = v
        return v

    def remove_user_vessel(self, vessel_id: str) -> None:
        v = self._v.get(vessel_id)
        if v is None or not v.user_created:
            raise ValueError("only user-created vessels can be removed")
        del self._v[vessel_id]

    def with_focus_nominal(self, vessel_id: str, z_um: float) -> Vessel:
        """Capture Focus Nominal: replace the vessel's focus nominal (session/user value)."""
        v = self._v[vessel_id].model_copy(update={"focus_nominal_um": float(z_um)})
        self._v[vessel_id] = v
        return v


class VesselStageMap(BaseModel):
    """Vessel mm <-> stage units. `offset_*` is the vessel-alignment correction (stage units)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vessel_w_mm: float = SBS_W
    vessel_h_mm: float = SBS_H
    stage_unit: str = "um"
    stage_frame: str = "sim-stage"
    offset_x: float = 0.0
    offset_y: float = 0.0
    verified: bool = False  # True only with a verified alignment calibration

    def to_stage(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        return ((x_mm - self.vessel_w_mm / 2) * 1000.0 + self.offset_x,
                (y_mm - self.vessel_h_mm / 2) * 1000.0 + self.offset_y)

    def to_vessel(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self.offset_x) / 1000.0 + self.vessel_w_mm / 2,
                (sy - self.offset_y) / 1000.0 + self.vessel_h_mm / 2)


def well_label_sort_key(region_id: str) -> tuple:
    head = region_id.rstrip(string.digits)
    tail = region_id[len(head):]
    return (len(head), head, int(tail) if tail else 0)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
