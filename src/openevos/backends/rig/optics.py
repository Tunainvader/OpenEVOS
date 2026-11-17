"""Optics-aware sample simulator: what the cameras would see at the current stage/focus/objective/light.

A deterministic synthetic culture covers the vessel regions (cells only inside wells/slides).
`SampleWorld.render(view)` produces a frame for the field of view at the current stage position
with:

* magnification-dependent field of view and pixel scale (sensor pixel / magnification);
* defocus blur that grows with |z - focal plane| / depth of field (from NA);
* channel-specific structure (DAPI nuclei, GFP cytoplasm, TxRed puncta, brightfield absorption,
  phase halos), LED intensity x exposure x gain scaling, shot + read noise and saturation;
* a colour camera variant (8-bit RGB) for brightfield/phase;
* darkness when the LED is off or the cube carriage is travelling.

All numbers are synthetic. Nothing here models the real EVOS optics or sensors quantitatively.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from openevos.instrument.vessels import RegionShape, Vessel, VesselStageMap

TILE_UM = 400.0
STRUCTURE = {"cube1": "nucleus", "cube2": "cytoplasm", "cube3": "puncta", "cube4": None}
CHANNEL_GAIN = {"cube1": 1.25, "cube2": 1.0, "cube3": 0.8, "cube4": 0.0}


@dataclass(frozen=True)
class View:
    x_um: float
    y_um: float
    z_um: float
    magnification: float
    numerical_aperture: float
    sensor_pixel_um: float
    width: int
    height: int
    channel: str  # slot: cube1..4 / trans / phase
    led_percent: float
    exposure_ms: float
    gain: float
    light_on: bool
    color: bool = False
    max_value: int = 4095
    parfocal_offset_um: float = 0.0
    time_s: float = 0.0

    @property
    def pixel_um(self) -> float:
        return self.sensor_pixel_um / self.magnification

    @property
    def depth_of_field_um(self) -> float:
        na = max(0.05, self.numerical_aperture)
        return 0.55 / (na * na) + self.pixel_um / na  # wave + geometric term (rough)


class SampleWorld:
    def __init__(self, vessel: Vessel, stage_map: VesselStageMap | None = None, *, seed: int = 7,
                 density_per_mm2: float = 420.0):
        self.seed = seed
        self.density = density_per_mm2
        self._cache: dict[tuple[int, int], np.ndarray] = {}
        self.set_vessel(vessel, stage_map or VesselStageMap())

    def set_vessel(self, vessel: Vessel, stage_map: VesselStageMap | None = None) -> None:
        self.vessel = vessel
        if stage_map is not None:
            self.map = stage_map
        self._cache.clear()

    # ------------------------------------------------------------------ sample geometry
    def focal_plane_um(self, x_um: float, y_um: float) -> float:
        """Sample surface height: vessel focus nominal + gentle tilt and bow (synthetic)."""
        tilt = 6e-4 * x_um - 4e-4 * y_um
        bow = 9.0 * math.cos(x_um / 21000.0) * math.cos(y_um / 17000.0)
        return self.vessel.focus_nominal_um + 18.0 + tilt + bow

    def _tile(self, i: int, j: int) -> np.ndarray:
        key = (i, j)
        if key in self._cache:
            return self._cache[key]
        h = hashlib.blake2b(f"{self.seed}:{self.vessel.vessel_id}:{i}:{j}".encode(), digest_size=8).digest()
        rng = np.random.default_rng(int.from_bytes(h, "little"))
        confluence = 0.35 + 1.1 * (0.5 + 0.5 * math.sin(i * 0.37 + self.seed) * math.cos(j * 0.29 - self.seed))
        n = rng.poisson(self.density * confluence * (TILE_UM / 1000.0) ** 2)
        x = rng.uniform(i * TILE_UM, (i + 1) * TILE_UM, n)
        y = rng.uniform(j * TILE_UM, (j + 1) * TILE_UM, n)
        cells = np.column_stack([
            x, y,
            rng.uniform(3.5, 6.5, n),  # nucleus radius um
            rng.uniform(9.0, 17.0, n),  # cell radius um
            rng.uniform(0.6, 1.4, n),  # GFP level
            rng.uniform(0.7, 1.3, n),  # DAPI level
            (rng.uniform(0, 1, n) < 0.35).astype(float) * rng.uniform(0.6, 1.5, n),  # TxRed level
            rng.uniform(0, 2 * math.pi, n),  # phase of slow motion
            rng.uniform(0.8, 1.6, n),  # elongation
        ])
        if n:
            keep = np.array([self._inside(cx, cy) for cx, cy in cells[:, :2]], dtype=bool)
            cells = cells[keep]
        self._cache[key] = cells
        if len(self._cache) > 4096:
            self._cache.pop(next(iter(self._cache)))
        return cells

    def _inside(self, sx: float, sy: float) -> bool:
        vx, vy = self.map.to_vessel(sx, sy)
        return self.vessel.region_at(vx, vy) is not None

    def cells_in(self, x0: float, y0: float, x1: float, y1: float, t: float = 0.0) -> np.ndarray:
        i0, i1 = math.floor(x0 / TILE_UM), math.floor(x1 / TILE_UM)
        j0, j1 = math.floor(y0 / TILE_UM), math.floor(y1 / TILE_UM)
        parts = [self._tile(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
        cells = np.concatenate(parts) if parts else np.empty((0, 9))
        if len(cells) and t:
            cells = cells.copy()
            cells[:, 0] += 6.0 * np.sin(t / 240.0 + cells[:, 7])  # slow migration for time-lapse demos
            cells[:, 1] += 6.0 * np.cos(t / 300.0 + cells[:, 7] * 1.3)
        return cells

    def region_mask(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Boolean mask (shape of the meshgrid) of pixels lying inside any vessel region."""
        vx = (xs - self.map.offset_x) / 1000.0 + self.map.vessel_w_mm / 2
        vy = (ys - self.map.offset_y) / 1000.0 + self.map.vessel_h_mm / 2
        x0, x1, y0, y1 = vx.min(), vx.max(), vy.min(), vy.max()
        mask = np.zeros(np.broadcast_shapes(vx.shape, vy.shape), dtype=bool)
        for r in self.vessel.regions:
            bx0, by0, bx1, by1 = r.bounds
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            if r.shape is RegionShape.CIRCLE:
                rad = min(r.w, r.h) / 2
                mask |= (vx - r.cx) ** 2 + (vy - r.cy) ** 2 <= rad * rad
            else:
                mask |= (np.abs(vx - r.cx) <= r.w / 2) & (np.abs(vy - r.cy) <= r.h / 2)
        return mask

    # ------------------------------------------------------------------ rendering
    def render(self, v: View, rng: np.random.Generator) -> np.ndarray:
        w, h, px = v.width, v.height, v.pixel_um
        x0, y0 = v.x_um - w * px / 2, v.y_um - h * px / 2
        xs = x0 + (np.arange(w) + 0.5) * px
        ys = y0 + (np.arange(h) + 0.5) * px
        inside = self.region_mask(xs[None, :], ys[:, None])

        kind = "trans" if v.channel == "trans" else "phase" if v.channel == "phase" else "fluo"
        margin = 40.0
        cells = self.cells_in(x0 - margin, y0 - margin, x0 + w * px + margin, y0 + h * px + margin, v.time_s)
        if kind == "fluo":
            structure = STRUCTURE.get(v.channel)
            img = np.zeros((h, w), np.float32)
            if structure is not None:
                self._stamp(img, cells, x0, y0, px, structure)
            signal = img * CHANNEL_GAIN.get(v.channel, 0.0) + 0.02 * inside  # autofluorescence of medium
        else:
            absorb = np.zeros((h, w), np.float32)
            self._stamp(absorb, cells, x0, y0, px, "cytoplasm")
            nuc = np.zeros((h, w), np.float32)
            self._stamp(nuc, cells, x0, y0, px, "nucleus")
            if kind == "trans":
                signal = 1.0 - 0.10 * absorb - 0.06 * nuc
                edge = ndimage.gaussian_laplace(absorb, sigma=max(0.8, 1.2 / px)) if absorb.any() else 0.0
                signal = signal + 0.6 * edge
            else:  # phase: dark bodies, bright halos
                body = absorb + 0.8 * nuc
                halo = ndimage.gaussian_filter(body, sigma=max(1.0, 2.5 / px)) - body
                signal = 0.55 - 0.35 * body + 0.9 * halo
            plastic = 0.62 if kind == "trans" else 0.30
            signal = np.where(inside, signal, plastic).astype(np.float32)
            signal *= self._vignette(w, h)

        dz = abs(v.z_um - (self.focal_plane_um(v.x_um, v.y_um) + v.parfocal_offset_um))
        sigma = min(18.0, 1.4 * dz / v.depth_of_field_um)
        if sigma > 0.3:
            signal = ndimage.gaussian_filter(signal, sigma=sigma, mode="nearest")

        light = (v.led_percent / 100.0) if v.light_on else 0.0
        if kind == "fluo":
            scale = 2600.0 * light * (v.exposure_ms / 40.0)
        else:
            scale = 6600.0 * light * (v.exposure_ms / 5.0)
        photons = np.clip(signal, 0, None) * scale
        noisy = photons + rng.normal(0.0, 1.0, photons.shape).astype(np.float32) * np.sqrt(photons + 9.0)
        counts = (noisy * v.gain + 100.0 + rng.normal(0, 3.0 * v.gain, photons.shape)).astype(np.float32)
        if v.color:
            return self._to_color(counts, v)
        return np.clip(counts, 0, v.max_value).astype(np.uint16)

    @staticmethod
    def _vignette(w: int, h: int) -> np.ndarray:
        yy, xx = np.ogrid[-1:1:complex(0, h), -1:1:complex(0, w)]
        return (1.0 - 0.12 * (xx * xx + yy * yy)).astype(np.float32)

    @staticmethod
    def _to_color(counts: np.ndarray, v: View) -> np.ndarray:
        g = np.clip((counts - 100.0) / 3600.0, 0, 1)  # colour pipeline reference white
        tint = np.array([1.0, 0.96, 0.88], np.float32) if v.channel == "trans" else np.array([0.95, 0.97, 1.0])
        rgb = np.clip(g[..., None] * tint * 255.0, 0, 255)
        return rgb.astype(np.uint8)

    @staticmethod
    def _stamp(img: np.ndarray, cells: np.ndarray, x0: float, y0: float, px: float, structure: str) -> None:
        h, w = img.shape
        for cx, cy, rn, rc, gfp, dapi, txr, ph, elong in cells:
            if structure == "nucleus":
                rad, amp = rn, dapi
            elif structure == "cytoplasm":
                rad, amp = rc, gfp * 0.55
            else:
                if txr <= 0:
                    continue
                rad, amp = rn * 0.5, txr * 1.4
            rp = max(0.8, rad / px)  # radius in pixels
            sx, sy = rp * elong, rp / elong
            u, vv = (cx - x0) / px, (cy - y0) / px
            r = int(1.5 * max(sx, sy)) + 2
            ix0, ix1 = max(0, int(u) - r), min(w, int(u) + r + 1)
            iy0, iy1 = max(0, int(vv) - r), min(h, int(vv) + r + 1)
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            yy, xx = np.ogrid[iy0:iy1, ix0:ix1]
            c, s = math.cos(ph), math.sin(ph)
            dx, dy = xx - u, yy - vv
            a, b = dx * c + dy * s, -dx * s + dy * c
            d = np.sqrt(a * a / (sx * sx) + b * b / (sy * sy))
            edge = max(0.06, 0.9 / rp)  # ~1 px soft edge, as an in-focus optical image
            img[iy0:iy1, ix0:ix1] += amp * (1.0 - 0.25 * d * d).clip(0.5) / (1.0 + np.exp((d - 1.0) / edge))
