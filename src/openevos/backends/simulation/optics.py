"""Image formation for the simulated cameras.

The simulated optics read the simulated devices' current state (stage XY, focus Z, objective,
phase ring, epi carriage, LEDs) and render what a camera would see:

* transmitted light: brightfield, or phase contrast when the phase annulus matches the objective;
* fluorescence: only from an epi LED whose cube is in the light path, scaled by NA⁴/M²;
* defocus blur that grows with |Z − focal plane| × NA;
* vignetting, shot noise, read noise, offset, gain and saturation.

It is a plausibility model for UI and workflow development, not a physical simulation.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np

from openevos.backends.simulation.profile import MIRROR_LABEL, TRANSMITTED_LED, CameraModel
from openevos.backends.simulation.specimen import CellBatch, Specimen
from openevos.hal.types import ObjectiveInfo

if TYPE_CHECKING:
    from openevos.backends.simulation.illumination import SimulatedIllumination
    from openevos.backends.simulation.motion import (
        SimulatedFocus,
        SimulatedObjectiveTurret,
        SimulatedPhaseTurret,
        SimulatedXYStage,
    )

# electrons per ms per pixel at 100 % LED, for a map value of 1, at the 10×/0.30 reference
TRANSMITTED_E_PER_MS = 1000.0
FLUOR_E_PER_MS = {"dapi": 120.0, "gfp": 100.0, "rfp": 70.0, "cy5": 45.0}
AUTOFLUOR_E_PER_MS = {"dapi": 3.0, "gfp": 2.0, "rfp": 1.5, "cy5": 1.0}
LED_WHITE_RGB = (1.0, 0.97, 0.91)
CUBE_TRANSMITTED_FRACTION = 0.12  # transmitted light leaking through a cube's emission filter
REF_NA4_M2 = 0.30 ** 4 / 10.0 ** 2
WAVELENGTH_UM = 0.55


@dataclass(frozen=True)
class OpticalState:
    x_um: float
    y_um: float
    z_um: float
    objective: ObjectiveInfo | None  # None while the turret is rotating
    phase_label: str | None  # None while the phase turret is rotating
    carriage: int | None  # None while the carriage is moving
    carriage_label: str | None
    leds: dict[str, float]  # enabled LED id -> intensity %


def box_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian approximation by three box-filter passes per axis (O(N), any sigma)."""
    if sigma < 0.4:
        return a
    w = math.sqrt(12.0 * sigma * sigma / 3.0 + 1.0)
    r = max(1, round((w - 1.0) / 2.0))
    out = a
    for axis in (0, 1):
        for _ in range(3):
            out = _box_axis(out, r, axis)
    return out


def _box_axis(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    k = 2 * r + 1
    if axis == 0:
        return (c[k:] - c[:-k]) / np.float32(k)
    return (c[:, k:] - c[:, :-k]) / np.float32(k)


class SimulatedOptics:
    def __init__(self, specimen: Specimen, specimen_z_um: float, stage: SimulatedXYStage,
                 focus: SimulatedFocus, turret: SimulatedObjectiveTurret,
                 phase: SimulatedPhaseTurret, illumination: SimulatedIllumination):
        self.specimen = specimen
        self.specimen_z_um = specimen_z_um
        self.stage, self.focus, self.turret = stage, focus, turret
        self.phase, self.illumination = phase, illumination
        self._lock = threading.Lock()
        self._maps: OrderedDict[tuple, dict[str, np.ndarray]] = OrderedDict()
        self._blurred: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self._vignette: dict[tuple[int, int], np.ndarray] = {}
        self._rates: dict[str, tuple[tuple, np.ndarray, dict[str, Any]]] = {}

    # ------------------------------------------------------------------------------ state
    def state(self) -> OpticalState:
        x, y = self.stage.get_position_um()
        z = self.focus.get_position_um()
        obj = None if self.turret.is_busy() else self.turret.current_objective()
        phase = None
        if not self.phase.is_busy():
            pos = self.phase.get_position()
            phase = next(p.label for p in self.phase.get_positions() if p.index == pos)
        ill = self.illumination
        carriage = None if ill.is_busy() else ill.get_carriage_position()
        clabel = None
        if carriage is not None:
            clabel = next(p.label for p in ill.get_carriage_positions() if p.index == carriage)
        leds = {s.channel_id: s.intensity_pct for s in ill.get_led_states()
                if s.enabled and s.intensity_pct > 0}
        return OpticalState(x, y, z, obj, phase, carriage, clabel, leds)

    def focal_plane_um(self, objective: ObjectiveInfo) -> float:
        return self.specimen_z_um + objective.parfocal_offset_um

    # ------------------------------------------------------------------------------ render
    def render(self, cam: CameraModel, exposure_ms: float, gain_db: float,
               rng: np.random.Generator) -> tuple[np.ndarray, dict[str, Any]]:
        st = self.state()
        meta: dict[str, Any] = {
            "simulated": True, "stage_x_um": round(st.x_um, 3), "stage_y_um": round(st.y_um, 3),
            "focus_z_um": round(st.z_um, 3), "phase": st.phase_label,
            "carriage": st.carriage_label, "leds": dict(st.leds),
        }
        obj = st.objective
        if obj is not None:
            meta.update(objective=obj.label, magnification=obj.magnification,
                        numerical_aperture=obj.numerical_aperture,
                        pixel_size_um=cam.pixel_size_um / obj.magnification)
        key = (cam.role, cam.width, cam.height, round(st.x_um, 2), round(st.y_um, 2),
               round(st.z_um, 2), obj.position if obj else None, st.phase_label, st.carriage,
               tuple(sorted(st.leds.items())))
        with self._lock:
            cached = self._rates.get(cam.role)
        if cached is not None and cached[0] == key:
            rate, light_meta = cached[1], cached[2]
        else:
            rate, light_meta = self._rate(cam, st)
            with self._lock:
                self._rates[cam.role] = (key, rate, light_meta)
        meta.update(light_meta)
        return self._expose(cam, rate, exposure_ms, gain_db, rng), meta

    def _rate(self, cam: CameraModel, st: OpticalState) -> tuple[np.ndarray, dict[str, Any]]:
        """Noise-free photo-electron rate (e⁻/ms) per pixel, computed at reduced resolution.

        Mono: returned at full resolution (upsampled), so noise is added per pixel.
        Colour: returned at internal resolution; `_expose` upsamples the final 8-bit image.
        """
        b = max(1, int(cam.render_binning))
        wi, hi = -(-cam.width // b), -(-cam.height // b)
        icam = replace(cam, width=wi, height=hi, pixel_size_um=cam.pixel_size_um * b)
        nchan = 3 if cam.color else 1
        rate = np.zeros((hi, wi, nchan), dtype=np.float32)
        meta: dict[str, Any] = {}
        obj = st.objective
        if obj is not None and st.carriage is not None:
            rate = self._light(icam, st, obj, rate, meta)
        else:
            meta["light_path"] = "blocked (turret or carriage moving)"
        if not cam.color and b > 1:
            rate = _upsample(rate, b, cam.height, cam.width)
        return rate, meta

    def _light(self, cam: CameraModel, st: OpticalState, obj: ObjectiveInfo, rate: np.ndarray,
               meta: dict[str, Any]) -> np.ndarray:
        px = cam.pixel_size_um / obj.magnification
        dz = st.z_um - self.focal_plane_um(obj)
        sigma_um = 0.5 * abs(dz) * obj.numerical_aperture + 0.61 * WAVELENGTH_UM / obj.numerical_aperture / 2.355
        sigma_px = min(sigma_um / px, 60.0)
        meta["sim_defocus_um"] = round(dz, 3)
        vign = self._vignette_for(cam)
        cube = self.illumination.cube_at(st.carriage) if st.carriage is not None else None
        at_mirror = st.carriage_label == MIRROR_LABEL

        tl = st.leds.get(TRANSMITTED_LED)
        if tl:
            thick = self._blur(cam, st, obj, "thick", sigma_px)
            nuc = self._blur(cam, st, obj, "nuc", sigma_px)
            ring = obj.phase_ring
            if st.phase_label in (None, "Open") or ring is None:
                wide = self._blur(cam, st, obj, "thick", sigma_px + 2.5)
                defocus = 0.35 * max(-1.0, min(1.0, dz / 15.0)) * (wide - thick)
                edge = np.abs(wide - thick)
                t = 1.0 - 0.07 * thick - 0.06 * nuc - 0.25 * edge + defocus
                mode = "brightfield"
            elif st.phase_label == ring:
                halo_px = max(2.0, 2.0 / px)
                wide = self._blur(cam, st, obj, "thick", sigma_px + halo_px)
                t = 0.62 - 0.3 * thick - 0.1 * nuc + 1.4 * np.clip(wide - thick, 0.0, None)
                mode = "phase contrast"
            else:
                t = 0.35 * (1.0 - 0.05 * thick)
                mode = f"phase ring mismatch ({st.phase_label} vs {ring})"
            t = np.clip(t, 0.0, None) * vign
            fraction = 1.0 if at_mirror else CUBE_TRANSMITTED_FRACTION
            e = TRANSMITTED_E_PER_MS * (tl / 100.0) * fraction
            tint = LED_WHITE_RGB if at_mirror or cube is None else tuple(
                a * b for a, b in zip(LED_WHITE_RGB, cube.display_rgb))
            self._add(rate, t * e, tint)
            meta["transmitted_mode"] = mode

        if cube is not None:
            led = st.leds.get(f"CUBE{cube.slot}")
            if led:
                na_factor = (obj.numerical_aperture ** 4 / obj.magnification ** 2) / REF_NA4_M2
                na_factor = float(min(3.0, max(0.03, na_factor)))
                fmap = self._blur(cam, st, obj, cube.fluor, sigma_px)
                e = (FLUOR_E_PER_MS[cube.fluor] * na_factor * fmap * vign
                     + AUTOFLUOR_E_PER_MS[cube.fluor]) * (led / 100.0)
                self._add(rate, e, cube.display_rgb)
                meta["fluorescence"] = cube.name
        return rate

    @staticmethod
    def _add(rate: np.ndarray, plane: np.ndarray, rgb: tuple[float, ...]) -> None:
        if rate.shape[2] == 1:
            rate[:, :, 0] += plane
        else:
            for i in range(3):
                rate[:, :, i] += plane * np.float32(rgb[i])

    @staticmethod
    def _expose(cam: CameraModel, rate: np.ndarray, exposure_ms: float, gain_db: float,
                rng: np.random.Generator) -> np.ndarray:
        electrons = rate * np.float32(exposure_ms)
        noise = rng.standard_normal(electrons.shape, dtype=np.float32)
        electrons += noise * np.sqrt(electrons + np.float32(cam.read_noise_e ** 2))
        gain = 10.0 ** (gain_db / 20.0)
        adu = electrons * np.float32(gain / cam.e_per_adu) + np.float32(cam.offset_adu)
        top = (1 << cam.bit_depth) - 1
        np.clip(adu, 0, top, out=adu)
        if cam.color:
            img = np.rint(adu).astype(np.uint8)
            if img.shape[:2] != (cam.height, cam.width):
                img = _upsample(img, max(1, int(cam.render_binning)), cam.height, cam.width)
            return img
        return np.rint(adu[:, :, 0]).astype(np.uint16)

    # ------------------------------------------------------------------------------ maps
    def _vignette_for(self, cam: CameraModel) -> np.ndarray:
        key = (cam.width, cam.height)
        v = self._vignette.get(key)
        if v is None:
            yy = (np.arange(cam.height, dtype=np.float32) - cam.height / 2) / (cam.height / 2)
            xx = (np.arange(cam.width, dtype=np.float32) - cam.width / 2) / (cam.width / 2)
            r2 = (xx[None, :] ** 2 * (cam.width / cam.height) + yy[:, None] ** 2) / 2.0
            v = (1.0 - 0.28 * r2).astype(np.float32)
            self._vignette[key] = v
        return v

    def _map_key(self, cam: CameraModel, st: OpticalState, obj: ObjectiveInfo) -> tuple:
        return (cam.width, cam.height, cam.pixel_size_um, round(st.x_um, 2), round(st.y_um, 2),
                obj.position)

    def _blur(self, cam: CameraModel, st: OpticalState, obj: ObjectiveInfo, kind: str,
              sigma_px: float) -> np.ndarray:
        key = self._map_key(cam, st, obj) + (kind, round(sigma_px * 4) / 4)
        with self._lock:
            hit = self._blurred.get(key)
            if hit is not None:
                self._blurred.move_to_end(key)
                return hit
        out = box_blur(self._map(cam, st, obj, kind), sigma_px)
        with self._lock:
            self._blurred[key] = out
            while len(self._blurred) > 12:
                self._blurred.popitem(last=False)
        return out

    def _map(self, cam: CameraModel, st: OpticalState, obj: ObjectiveInfo, kind: str) -> np.ndarray:
        key = self._map_key(cam, st, obj)
        with self._lock:
            maps = self._maps.get(key)
            if maps is not None and kind in maps:
                self._maps.move_to_end(key)
                return maps[kind]
        px = cam.pixel_size_um / obj.magnification
        x0 = st.x_um - cam.width * px / 2
        y0 = st.y_um - cam.height * px / 2
        cells = self.specimen.cells_in(x0, x0 + cam.width * px, y0, y0 + cam.height * px)
        plane = _stamp(cells, kind, x0, y0, px, cam.width, cam.height)
        with self._lock:
            maps = self._maps.setdefault(key, {})
            maps[kind] = plane
            self._maps.move_to_end(key)
            while len(self._maps) > 4:
                self._maps.popitem(last=False)
        return plane


def _upsample(a: np.ndarray, b: int, h: int, w: int) -> np.ndarray:
    if b > 1:
        a = np.repeat(np.repeat(a, b, axis=0), b, axis=1)
    return np.ascontiguousarray(a[:h, :w])


def _stamp(cells: CellBatch, kind: str, x0: float, y0: float, px: float, w: int, h: int) -> np.ndarray:
    out = np.zeros((h, w), dtype=np.float32)
    level = {"dapi": cells.dapi, "gfp": cells.gfp, "rfp": cells.rfp, "cy5": cells.cy5}.get(kind)
    for i in range(len(cells)):
        if level is not None and level[i] <= 0:
            continue
        cxp = (cells.cx[i] - x0) / px
        cyp = (cells.cy[i] - y0) / px
        rx, ry = cells.rx[i] / px, cells.ry[i] / px
        half = rx * (1.05 + cells.lobe_a[i]) + 1.0
        xa, xb = max(0, int(cxp - half)), min(w, int(cxp + half) + 2)
        ya, yb = max(0, int(cyp - half)), min(h, int(cyp + half) + 2)
        if xa >= xb or ya >= yb:
            continue
        gx = (np.arange(xa, xb, dtype=np.float32) - cxp)[None, :]
        gy = (np.arange(ya, yb, dtype=np.float32) - cyp)[:, None]
        c, s = cells.cos[i], cells.sin[i]
        u = gx * c + gy * s
        v = gy * c - gx * s
        r2 = (u / rx) ** 2 + (v / ry) ** 2
        if cells.lobe_a[i] > 0:  # irregular, lobed outline
            theta = np.arctan2(v / ry, u / rx)
            r2 = r2 / (1.0 + cells.lobe_a[i] * np.cos(cells.lobe_k[i] * theta + cells.lobe_phase[i])) ** 2
        if kind in ("thick", "nuc", "dapi", "gfp"):
            ns = cells.nuc_scale[i]
            un = u - cells.nuc_dx[i] / px
            vn = v - cells.nuc_dy[i] / px
            nuc = np.sqrt(np.clip(1.0 - (un / (rx * ns)) ** 2 - (vn / (ry * ns)) ** 2, 0.0, None))
        if kind == "thick":
            val = np.clip(1.0 - r2, 0.0, None) ** 0.6 + 0.5 * nuc
        elif kind == "nuc":
            val = nuc
        elif kind == "dapi":
            val = nuc * level[i]
        elif kind == "gfp":
            val = np.clip(1.0 - r2, 0.0, None) ** 0.5 * (1.0 - 0.45 * nuc) * level[i]
        elif kind == "rfp":
            val = np.clip(1.0 - r2, 0.0, None) ** 1.5 * level[i]
        elif kind == "cy5":
            val = np.clip(1.0 - np.abs(r2 - 0.75) / 0.3, 0.0, None) * level[i]
        else:
            raise ValueError(f"unknown map kind {kind}")
        out[ya:yb, xa:xb] += val.astype(np.float32)
    return out
