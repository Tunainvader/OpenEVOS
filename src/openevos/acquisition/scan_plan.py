"""Scan routines (manual p.22-27): vessel, objective/camera/channels, area by rectangle or two
corner beacons, Find Sample / Auto Fine options, coverage, save-separately option.

Coverage options shown in the Scan Wizard (manual p.24): Full coverage, Four points, Outer
region only, Center region only (with Percentage), Random (N images) and Grid (rows x columns).
Tiles are planned in vessel millimetres and converted to stage units by `VesselStageMap`.
"""

from __future__ import annotations

import math
import random
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevos.instrument.profile import CameraRole, InstrumentProfile
from openevos.instrument.vessels import Region, Vessel, VesselStageMap, well_label_sort_key


class Coverage(str, Enum):
    FULL = "full"
    FOUR_POINTS = "four_points"
    OUTER = "outer_region"
    CENTER = "center_region"
    RANDOM = "random"
    GRID = "grid"

    @property
    def label(self) -> str:
        return {"full": "Full coverage", "four_points": "Four points", "outer_region": "Outer region only",
                "center_region": "Center region only", "random": "Random", "grid": "Grid"}[self.value]


class AreaMethod(str, Enum):
    RECTANGLE = "rectangle"
    BEACONS = "beacons"


class Rect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: float  # vessel mm, top-left
    y: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)


class Beacon(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    x_mm: float
    y_mm: float
    z_um: float | None = None


class ScanRoutine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    vessel_id: str
    objective_slot: int = Field(ge=1, le=5)
    camera: CameraRole = CameraRole.MONO
    channels: list[str] = Field(min_length=1)
    save_channels_separately: bool = False
    area_method: AreaMethod = AreaMethod.RECTANGLE
    regions: list[str] = Field(default_factory=list)  # selected wells/slides; empty => all
    rect: Rect | None = None  # within each region, relative to the region's top-left (mm); None => whole region
    beacons: list[Beacon] = Field(default_factory=list)  # exactly 2 for beacon method
    find_sample: bool = False
    auto_fine: bool = True
    coverage: Coverage = Coverage.FULL
    percentage: float = Field(default=50.0, gt=0, le=100)
    random_images: int = Field(default=5, ge=1, le=5000)
    grid_rows: int = Field(default=3, ge=1, le=200)
    grid_cols: int = Field(default=3, ge=1, le=200)
    overlap_percent: float = Field(default=10.0, ge=0, le=50)
    output_folder: str = ""
    random_seed: int = 0

    @model_validator(mode="after")
    def _beacons(self) -> ScanRoutine:
        if self.area_method is AreaMethod.BEACONS and len(self.beacons) != 2:
            raise ValueError("a scan uses exactly two beacons: top-left and bottom-right (manual p.25)")
        return self


class Tile(BaseModel):
    model_config = ConfigDict(frozen=True)
    index: int
    region_id: str
    row: int
    col: int
    x_mm: float
    y_mm: float
    stage_x: float
    stage_y: float


class ScanPlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    routine: ScanRoutine
    fov_mm: tuple[float, float]
    step_mm: tuple[float, float]
    tiles: tuple[Tile, ...]
    out_of_limits: int
    warnings: tuple[str, ...]
    est_seconds: float
    est_bytes: int


def field_of_view_mm(profile: InstrumentProfile, role: CameraRole, slot: int) -> tuple[float, float]:
    cam = profile.camera(role)
    obj = profile.objective(slot)
    if not cam or not cam.pixel_size_um or not cam.width or not cam.height:
        raise ValueError("camera geometry unknown; cannot plan tiles")
    if not obj or not obj.installed or not obj.magnification:
        raise ValueError(f"objective slot {slot} has no magnification")
    return (cam.width * cam.pixel_size_um / obj.magnification / 1000.0,
            cam.height * cam.pixel_size_um / obj.magnification / 1000.0)


def _areas(r: ScanRoutine, vessel: Vessel) -> list[tuple[str, tuple[float, float, float, float], Region | None]]:
    """(label, (x0,y0,x1,y1) mm, region-for-masking)."""
    if r.area_method is AreaMethod.BEACONS:
        a, b = r.beacons
        x0, x1 = sorted((a.x_mm, b.x_mm))
        y0, y1 = sorted((a.y_mm, b.y_mm))
        return [("Beacons", (x0, y0, max(x1, x0 + 1e-3), max(y1, y0 + 1e-3)), None)]
    ids = r.regions or [g.region_id for g in vessel.regions]
    out = []
    for rid in sorted(ids, key=well_label_sort_key):
        reg = vessel.region(rid)
        if reg is None:
            raise ValueError(f"vessel {vessel.vessel_id} has no region {rid}")
        bx0, by0, bx1, by1 = reg.bounds
        if r.rect is not None:
            bx0, by0 = bx0 + r.rect.x, by0 + r.rect.y
            bx1, by1 = min(bx1, bx0 + r.rect.w), min(by1, by0 + r.rect.h)
            if bx1 <= bx0 or by1 <= by0:
                raise ValueError(f"area rectangle lies outside region {rid}")
        out.append((rid, (bx0, by0, bx1, by1), reg))
    return out


def _grid(x0: float, y0: float, x1: float, y1: float, sx: float, sy: float, fov: tuple[float, float]):
    nx = max(1, math.ceil(((x1 - x0) - fov[0]) / sx) + 1) if (x1 - x0) > fov[0] else 1
    ny = max(1, math.ceil(((y1 - y0) - fov[1]) / sy) + 1) if (y1 - y0) > fov[1] else 1
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ox, oy = cx - (nx - 1) * sx / 2, cy - (ny - 1) * sy / 2
    return [(row, col, ox + col * sx, oy + row * sy) for row in range(ny) for col in range(nx)], nx, ny


def plan_scan(r: ScanRoutine, vessel: Vessel, profile: InstrumentProfile, stage_map: VesselStageMap,
              stage_limits: tuple[tuple[float, float], tuple[float, float]] | None = None,
              seconds_per_tile: float = 1.2) -> ScanPlan:
    if r.vessel_id != vessel.vessel_id:
        raise ValueError("routine vessel does not match")
    for ch in r.channels:
        c = profile.channel(ch)
        cam = profile.camera(r.camera)
        if not c or not cam or not cam.supports(c):
            raise ValueError(f"channel {ch} is not available on the {r.camera.value} camera")
    fov = field_of_view_mm(profile, r.camera, r.objective_slot)
    k = 1 - r.overlap_percent / 100.0
    step = (fov[0] * k, fov[1] * k)
    rng = random.Random(r.random_seed)
    tiles: list[Tile] = []
    warnings: list[str] = []
    for label, (x0, y0, x1, y1), reg in _areas(r, vessel):
        cells, _nx, _ny = _grid(x0, y0, x1, y1, step[0], step[1], fov)
        if reg is not None:
            margin = -min(fov) / 2  # keep tiles that overlap the region
            cells = [c for c in cells if reg.contains(c[2], c[3], margin)]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
        chosen = cells
        if r.coverage is Coverage.FOUR_POINTS:
            pts = [(cx - hw / 2, cy - hh / 2), (cx + hw / 2, cy - hh / 2), (cx - hw / 2, cy + hh / 2),
                   (cx + hw / 2, cy + hh / 2)]
            chosen = [(i // 2, i % 2, x, y) for i, (x, y) in enumerate(pts)]
        elif r.coverage is Coverage.CENTER:  # centred sub-rectangle holding `percentage` of the area
            s = math.sqrt(r.percentage / 100.0)
            chosen = [c for c in cells if abs(c[2] - cx) <= hw * s and abs(c[3] - cy) <= hh * s]
        elif r.coverage is Coverage.OUTER:  # outer ring holding `percentage` of the area
            s = math.sqrt(1 - r.percentage / 100.0)
            chosen = [c for c in cells if abs(c[2] - cx) > hw * s or abs(c[3] - cy) > hh * s]
        elif r.coverage is Coverage.RANDOM:
            n = min(r.random_images, len(cells))
            if n < r.random_images:
                warnings.append(f"{label}: only {len(cells)} distinct fields fit; using {n}")
            chosen = sorted(rng.sample(cells, n), key=lambda c: (c[0], c[1]))
        elif r.coverage is Coverage.GRID:
            gx = [x0 + (i + 0.5) * (x1 - x0) / r.grid_cols for i in range(r.grid_cols)]
            gy = [y0 + (j + 0.5) * (y1 - y0) / r.grid_rows for j in range(r.grid_rows)]
            chosen = [(j, i, x, y) for j, y in enumerate(gy) for i, x in enumerate(gx)]
        if not chosen:
            warnings.append(f"{label}: coverage selects no fields")
        # serpentine order for the stage
        chosen = sorted(chosen, key=lambda c: (c[0], c[1] if c[0] % 2 == 0 else -c[1]))
        for row, col, x, y in chosen:
            sx, sy = stage_map.to_stage(x, y)
            tiles.append(Tile(index=len(tiles), region_id=label, row=row, col=col, x_mm=x, y_mm=y,
                              stage_x=sx, stage_y=sy))
    out = 0
    if stage_limits:
        (xl, xh), (yl, yh) = stage_limits
        out = sum(1 for t in tiles if not (xl <= t.stage_x <= xh and yl <= t.stage_y <= yh))
        if out:
            warnings.append(f"{out} tile(s) outside the stage limits will be rejected")
    if not stage_map.verified:
        warnings.append("vessel alignment not verified: positions are nominal")
    if not vessel.geometry_verified:
        warnings.append("vessel geometry is nominal (not measured on this unit)")
    cam = profile.camera(r.camera)
    bpp = 3 if r.camera is CameraRole.COLOR else 2
    est_bytes = len(tiles) * len(r.channels) * (cam.width or 0) * (cam.height or 0) * bpp
    per_tile = seconds_per_tile + 0.4 * len(r.channels) + (2.5 if r.auto_fine else 0) + (0 if not r.find_sample else 0.3)  # noqa: E501
    est = len(tiles) * per_tile + (12.0 if r.find_sample else 0)
    return ScanPlan(routine=r, fov_mm=fov, step_mm=step, tiles=tuple(tiles), out_of_limits=out,
                    warnings=tuple(warnings), est_seconds=est, est_bytes=est_bytes)
