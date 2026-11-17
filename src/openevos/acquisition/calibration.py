"""Calibration procedures (manual p.43, p.47-50) and the store that applies their results.

Procedures (all run through the command router; Qt-free; each result carries provenance):

* **Pixel size per objective** — image, move the stage by a known distance, image again, measure the
  image shift by phase correlation: µm/px = stage move / pixel shift. (Replaces the AMEP4720 slide
  measurement of the original "Calibrate Objective › field of view"; same quantity.)
* **Parfocality** — Auto Fine on every installed objective at one position; Z offsets relative to the
  reference (lowest magnification) objective.
* **Parcentration** — the same position on every objective; the reference image is cropped/rescaled to
  each objective's scale and registered: XY offsets relative to the reference.
* **Vessel alignment** — in brightfield at low magnification, find the four edges of two wells by
  stepping outwards until the field turns to plastic; the measured centres minus nominal centres give
  the stage offset of the vessel.
* **FL hot pixel correction** — dark frames (light off), median + robust threshold -> defect map;
  captures then replace defect pixels with the median of their neighbours, recorded in metadata.

Results are applied by `Session`: pixel size for scale bars/measurements, parfocal + parcentric
compensation on objective change, vessel offset in the vessel->stage map, defect correction in captures.
On the physical unit these procedures stay disabled until the turret/camera gates are passed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import ndimage

from openevos.acquisition.controller import InstrumentController
from openevos.core.models import utcnow
from openevos.instrument.profile import CameraRole, InstrumentProfile
from openevos.instrument.vessels import Vessel, VesselStageMap


class CalibrationError(RuntimeError):
    pass


@dataclass
class CalibrationStore:
    pixel_size_um: dict[str, dict[int, float]] = field(default_factory=dict)  # role -> slot -> µm/px
    parfocal_um: dict[int, float] = field(default_factory=dict)  # slot -> Z offset vs reference
    parcentric_um: dict[int, tuple[float, float]] = field(default_factory=dict)  # slot -> (dx, dy) vs reference
    vessel_offset_um: dict[str, tuple[float, float]] = field(default_factory=dict)  # vessel_id -> (dx, dy)
    hot_pixels: dict[str, list[tuple[int, int]]] = field(default_factory=dict)  # role -> [(y, x)]
    hot_pixel_correction: bool = True
    provenance: str = "measured on simulator"
    updated: dict[str, str] = field(default_factory=dict)  # procedure -> ISO time
    path: Path | None = None

    def touch(self, what: str) -> None:
        self.updated[what] = utcnow().isoformat()
        self.save()

    def to_json(self) -> dict:
        return {"pixel_size_um": {r: {str(k): v for k, v in d.items()} for r, d in self.pixel_size_um.items()},
                "parfocal_um": {str(k): v for k, v in self.parfocal_um.items()},
                "parcentric_um": {str(k): list(v) for k, v in self.parcentric_um.items()},
                "vessel_offset_um": {k: list(v) for k, v in self.vessel_offset_um.items()},
                "hot_pixels": {k: [list(p) for p in v] for k, v in self.hot_pixels.items()},
                "hot_pixel_correction": self.hot_pixel_correction, "provenance": self.provenance,
                "updated": self.updated}

    def save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.to_json(), indent=2), "utf-8")

    @classmethod
    def load(cls, path: Path | None) -> CalibrationStore:
        if path is None or not path.exists():
            return cls(path=path)
        d = json.loads(path.read_text("utf-8"))
        return cls(
            pixel_size_um={r: {int(k): float(v) for k, v in m.items()} for r, m in d.get("pixel_size_um", {}).items()},
            parfocal_um={int(k): float(v) for k, v in d.get("parfocal_um", {}).items()},
            parcentric_um={int(k): (float(v[0]), float(v[1])) for k, v in d.get("parcentric_um", {}).items()},
            vessel_offset_um={k: (float(v[0]), float(v[1])) for k, v in d.get("vessel_offset_um", {}).items()},
            hot_pixels={k: [(int(p[0]), int(p[1])) for p in v] for k, v in d.get("hot_pixels", {}).items()},
            hot_pixel_correction=bool(d.get("hot_pixel_correction", True)),
            provenance=d.get("provenance", "measured"), updated=d.get("updated", {}), path=path)

    def status(self) -> dict[str, str]:
        def when(k: str) -> str:
            t = self.updated.get(k)
            return datetime.fromisoformat(t).astimezone().strftime("%Y-%m-%d %H:%M") if t else ""
        return {k: when(k) for k in ("pixel_size", "parfocality", "parcentration", "vessel_alignment", "hot_pixels")}


# ---------------------------------------------------------------------------------------------- helpers
def _shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """(dy, dx, error) such that b ≈ a shifted by (dy, dx)."""
    from skimage.registration import phase_cross_correlation

    fa = ndimage.gaussian_filter(a.astype(np.float32), 1.0)
    fb = ndimage.gaussian_filter(b.astype(np.float32), 1.0)
    shift, err, _ = phase_cross_correlation(fa, fb, upsample_factor=20, normalization=None)
    return float(-shift[0]), float(-shift[1]), float(err)


def _frame(ctl: InstrumentController, role: CameraRole) -> np.ndarray:
    f = ctl.acquire(role, 1)[0].data
    return f.mean(axis=2) if f.ndim == 3 else f.astype(np.float32)


@dataclass(frozen=True)
class PixelSizeResult:
    slot: int
    um_per_px: float
    nominal_um_per_px: float | None
    deviation_pct: float | None
    registration_error: float


def measure_pixel_size(ctl: InstrumentController, profile: InstrumentProfile, role: CameraRole, slot: int,
                       move_fraction: float = 0.2) -> PixelSizeResult:
    cam, obj = profile.camera(role), profile.objective(slot)
    if obj is None or not obj.installed:
        raise CalibrationError(f"slot {slot} has no objective")
    if ctl.light.channel is None or not ctl.light.enabled:
        raise CalibrationError("switch the light on (brightfield or a stained channel) first")
    ctl.run(ctl.set_objective(slot))
    nominal = cam.pixel_size_um / obj.magnification if cam.pixel_size_um and obj.magnification else None
    est = nominal or 1.0
    x0, y0 = ctl.stage_xy()
    a = _frame(ctl, role)
    shifts, errs = [], []
    for dx_px, dy_px in ((move_fraction * a.shape[1], 0.0), (0.0, move_fraction * a.shape[0])):
        dx_um, dy_um = dx_px * est, dy_px * est
        ctl.run(ctl.move_stage(x0 + dx_um, y0 + dy_um))
        b = _frame(ctl, role)
        sy, sx, err = _shift(a, b)
        moved_px = abs(sx) if dx_um else abs(sy)
        if moved_px < 2:
            raise CalibrationError("no measurable image shift — is there structure in the field?")
        shifts.append((abs(dx_um) + abs(dy_um)) / moved_px)
        errs.append(err)
    ctl.run(ctl.move_stage(x0, y0))
    um = float(np.mean(shifts))
    dev = (um / nominal - 1) * 100 if nominal else None
    return PixelSizeResult(slot, um, nominal, dev, float(np.mean(errs)))


@dataclass(frozen=True)
class ParfocalResult:
    reference_slot: int
    best_z: dict[int, float]
    offsets_um: dict[int, float]
    failed: dict[int, str]


def measure_parfocality(ctl: InstrumentController, profile: InstrumentProfile, autofocus, role: CameraRole,
                        slots: list[int] | None = None, progress=None) -> ParfocalResult:
    slots = slots or [o.slot for o in profile.objectives if o.installed]
    slots = sorted(slots, key=lambda s: profile.objective(s).magnification or 0)
    best, failed = {}, {}
    for i, s in enumerate(slots):
        if progress:
            progress(i, len(slots), f"objective {profile.objective(s).short}: focusing")
        ctl.run(ctl.set_objective(s))
        r = autofocus.auto_fine(role, 150.0 if i == 0 else 100.0)
        if r.ok and r.best_z is not None:
            best[s] = r.best_z
            fine = autofocus.auto_fine(role, 40.0)  # second, narrower pass
            if fine.ok and fine.best_z is not None:
                best[s] = fine.best_z
        else:
            failed[s] = r.message
    if not best:
        raise CalibrationError("no objective found focus: " + "; ".join(failed.values()))
    ref = slots[0] if slots[0] in best else next(iter(best))
    return ParfocalResult(ref, best, {s: z - best[ref] for s, z in best.items()}, failed)


@dataclass(frozen=True)
class ParcentricResult:
    reference_slot: int
    offsets_um: dict[int, tuple[float, float]]
    errors: dict[int, float]


def measure_parcentration(ctl: InstrumentController, profile: InstrumentProfile, role: CameraRole,
                          pixel_size: dict[int, float], slots: list[int] | None = None, progress=None
                          ) -> ParcentricResult:
    slots = slots or [o.slot for o in profile.objectives if o.installed]
    slots = sorted(slots, key=lambda s: profile.objective(s).magnification or 0)
    ref = slots[0]
    ctl.run(ctl.set_objective(ref))
    a = _frame(ctl, role)
    pa = pixel_size[ref]
    out, errs = {ref: (0.0, 0.0)}, {ref: 0.0}
    for i, s in enumerate(slots[1:], 1):
        if progress:
            progress(i, len(slots), f"objective {profile.objective(s).short}: registering")
        ctl.run(ctl.set_objective(s))
        b = _frame(ctl, role)
        ps = pixel_size[s]
        k = pa / ps  # upscale factor ref -> slot scale
        h, w = b.shape
        ch, cw = h / k, w / k
        cy, cx = a.shape[0] / 2, a.shape[1] / 2
        crop = a[int(cy - ch / 2): int(cy + ch / 2), int(cx - cw / 2): int(cx + cw / 2)]
        up = ndimage.zoom(crop, (h / crop.shape[0], w / crop.shape[1]), order=1)
        sy, sx, err = _shift(up, b)
        # the slot's image appears shifted by (sx, sy) px at slot scale: its optical axis is offset by -shift
        out[s] = (-sx * ps, -sy * ps)
        errs[s] = err
    ctl.run(ctl.set_objective(ref))
    return ParcentricResult(ref, out, errs)


@dataclass(frozen=True)
class VesselAlignmentResult:
    vessel_id: str
    offset_um: tuple[float, float]
    per_region: dict[str, tuple[float, float]]
    residual_um: float


def _is_inside(ctl: InstrumentController, role: CameraRole, plastic_level: float) -> bool:
    f = _frame(ctl, role)
    h, w = f.shape
    core = f[h // 3: 2 * h // 3, w // 3: 2 * w // 3]
    return float(core.mean()) > plastic_level


def find_region_center(ctl: InstrumentController, role: CameraRole, nominal_xy: tuple[float, float],
                       radius_um: float | tuple[float, float], tol_um: float = 15.0,
                       max_out_um: float | None = None) -> tuple[float, float]:
    """Binary-search the four edges of a region around its nominal centre (brightfield).
    `max_out_um` bounds the search so it never reaches a neighbouring well."""
    x0, y0 = nominal_xy
    rx, ry = radius_um if isinstance(radius_um, tuple) else (radius_um, radius_um)
    ctl.run(ctl.move_stage(x0, y0))
    inside = _frame(ctl, role)
    level_in = float(inside.mean())
    ctl.run(ctl.move_stage(x0 + 1.25 * rx, y0 + 1.25 * ry))  # diagonal: between wells
    level_out = float(_frame(ctl, role).mean())
    if level_in - level_out < 0.08 * max(level_in, 1):
        raise CalibrationError("cannot tell well from plastic — use brightfield (TRANS) at low magnification")
    thr = (level_in + level_out) / 2
    edges = {}
    for axis, sign in (("x", 1), ("x", -1), ("y", 1), ("y", -1)):
        r_ax = rx if axis == "x" else ry
        lim = ctl.axis_limits(ctl.profile.stage_device, axis)
        base = x0 if axis == "x" else y0
        reach = ((lim[1] - base) if sign > 0 else (base - lim[0])) if lim else 1.6 * r_ax
        lo, hi = 0.0, min(1.6 * r_ax, max_out_um or 1.6 * r_ax, reach)  # lo inside, hi outside
        while hi - lo > tol_um:
            mid = (lo + hi) / 2
            dx, dy = (sign * mid, 0.0) if axis == "x" else (0.0, sign * mid)
            ctl.run(ctl.move_stage(x0 + dx, y0 + dy))
            if _is_inside(ctl, role, thr):
                lo = mid
            else:
                hi = mid
        edges[(axis, sign)] = (lo + hi) / 2
    cx = x0 + (edges[("x", 1)] - edges[("x", -1)]) / 2
    cy = y0 + (edges[("y", 1)] - edges[("y", -1)]) / 2
    return cx, cy


def alignment_regions(ctl: InstrumentController, vessel: Vessel, nominal: VesselStageMap) -> list[str]:
    """Two regions as far apart as possible whose whole search area is inside the stage limits."""
    lx = ctl.axis_limits(ctl.profile.stage_device, "x")
    ly = ctl.axis_limits(ctl.profile.stage_device, "y")

    def reachable(r) -> bool:
        if not lx or not ly:
            return True
        mx, my = 1.08 * r.w / 2, 1.08 * r.h / 2  # both edges (plus a margin) must be reachable
        x0, y0 = nominal.to_stage(r.cx - mx, r.cy - my)
        x1, y1 = nominal.to_stage(r.cx + mx, r.cy + my)
        return lx[0] <= x0 and x1 <= lx[1] and ly[0] <= y0 and y1 <= ly[1]

    ok = [r for r in vessel.regions if reachable(r)]
    if not ok:
        raise CalibrationError("no region of this vessel lies fully inside the stage limits")
    if len(ok) == 1:
        return [ok[0].region_id]
    a = min(ok, key=lambda r: r.cx + r.cy)
    b = max(ok, key=lambda r: r.cx + r.cy)
    return [a.region_id, b.region_id]


def calibrate_vessel_alignment(ctl: InstrumentController, vessel: Vessel, stage_map: VesselStageMap,
                               role: CameraRole, regions: list[str] | None = None, progress=None
                               ) -> VesselAlignmentResult:
    if not vessel.regions:
        raise CalibrationError("vessel has no regions")
    nominal = VesselStageMap(vessel_w_mm=stage_map.vessel_w_mm, vessel_h_mm=stage_map.vessel_h_mm,
                             stage_unit=stage_map.stage_unit, stage_frame=stage_map.stage_frame)
    if regions is None:
        regions = alignment_regions(ctl, vessel, nominal)
    per: dict[str, tuple[float, float]] = {}
    for i, rid in enumerate(regions):
        if progress:
            progress(i, len(regions), f"finding the edges of {rid}")
        r = vessel.region(rid)
        nx, ny = nominal.to_stage(r.cx, r.cy)
        others = [o for o in vessel.regions if o.region_id != rid]
        d_nn = min((np.hypot(o.cx - r.cx, o.cy - r.cy) for o in others), default=None)
        rad = min(r.w, r.h) / 2
        limit = (d_nn - rad) * 0.95 * 1000 if d_nn else None
        cx, cy = find_region_center(ctl, role, (nx, ny), (r.w / 2 * 1000, r.h / 2 * 1000), max_out_um=limit)
        per[rid] = (cx - nx, cy - ny)
    offs = np.array(list(per.values()))
    mean = offs.mean(axis=0)
    resid = float(np.max(np.hypot(*(offs - mean).T))) if len(offs) > 1 else 0.0
    return VesselAlignmentResult(vessel.vessel_id, (float(mean[0]), float(mean[1])), per, resid)


@dataclass(frozen=True)
class HotPixelResult:
    role: str
    defects: list[tuple[int, int]]
    threshold: float
    frames: int


def find_hot_pixels(ctl: InstrumentController, role: CameraRole, *, frames: int = 6, exposure_ms: float = 500.0,
                    k_mad: float = 10.0) -> HotPixelResult:
    """Dark frames (light switched off by this procedure) → defect map."""
    light = ctl.light
    cam = ctl.camera_backend(role)
    prev_exp, prev_gain = getattr(cam, "exposure_ms", None), getattr(cam, "gain", None)
    if light.channel is not None:
        ctl.run(ctl.set_light(light.channel, light.intensity_percent, False))
    ctl.apply_camera(role, exposure_ms, 1.0)
    stack = np.stack([ctl.acquire(role, 1)[0].data.astype(np.float32) for _ in range(frames)])
    med = np.median(stack, axis=0)
    base = ndimage.median_filter(med, size=5)
    diff = med - base
    mad = float(np.median(np.abs(diff - np.median(diff)))) or 1.0
    thr = k_mad * 1.4826 * mad
    ys, xs = np.nonzero(diff > thr)
    if prev_exp is not None and prev_gain is not None:  # restore the operator's camera settings
        ctl.apply_camera(role, prev_exp, prev_gain)
    if light.channel is not None and light.enabled:
        ctl.run(ctl.set_light(light.channel, light.intensity_percent, True))
    return HotPixelResult(role.value, [(int(y), int(x)) for y, x in zip(ys, xs)], thr, frames)


def correct_hot_pixels(data: np.ndarray, defects: list[tuple[int, int]]) -> np.ndarray:
    """Replace defect pixels with the median of their 3x3 neighbours (excluding other defects)."""
    if not defects or data.ndim != 2:
        return data
    out = data.copy()
    h, w = data.shape
    bad = np.zeros((h, w), bool)
    for y, x in defects:
        if 0 <= y < h and 0 <= x < w:
            bad[y, x] = True
    for y, x in defects:
        if not (0 <= y < h and 0 <= x < w):
            continue
        ys, xs = slice(max(0, y - 1), min(h, y + 2)), slice(max(0, x - 1), min(w, x + 2))
        patch, mask = data[ys, xs], ~bad[ys, xs]
        if mask.any():
            out[y, x] = np.median(patch[mask])
    return out
