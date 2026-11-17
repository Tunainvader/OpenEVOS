"""Find Sample (coarse, wide range around the vessel focus nominal) and Auto Fine (narrow range
around the current Z), manual p.9/p.24/p.41.

Each step is a verified focus move followed by a scientific single-frame acquisition, so the
camera never exposes while Z is moving. A flat focus curve is reported as "no sample found"
instead of moving to an arbitrary maximum.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from openevos.acquisition.controller import InstrumentController
from openevos.imaging.focus_metrics import METRICS, parabolic_peak, peak_prominence
from openevos.instrument.profile import CameraRole, InstrumentProfile


@dataclass(frozen=True)
class AutofocusResult:
    ok: bool
    best_z: float | None
    z: tuple[float, ...]
    scores: tuple[float, ...]
    metric: str
    message: str
    prominence: float


class AutofocusCancelled(RuntimeError):
    pass


def fine_range_um(profile: InstrumentProfile, slot: int | None, range_percent: float) -> float:
    """Auto Fine search span: ~8 depths of field of the active objective, scaled by the System >
    Basic 'Auto Fine Adjustment' range (100 % = default)."""
    obj = profile.objective(slot) if slot else None
    na = obj.numerical_aperture if obj and obj.numerical_aperture else 0.25
    dof = 0.55 / (na * na)
    return max(6.0, 8.0 * dof) * range_percent / 100.0


class Autofocus:
    def __init__(self, ctl: InstrumentController, profile: InstrumentProfile, *, metric: str = "tenengrad",
                 min_prominence: float = 0.08):
        self.ctl, self.profile = ctl, profile
        self.metric = metric
        self.min_prominence = min_prominence
        self.cancel_event = threading.Event()

    def sweep(self, role: CameraRole, center: float, span: float, steps: int,
              progress: Callable[[int, int, str], None] | None = None) -> AutofocusResult:
        fn = METRICS[self.metric]
        light = self.ctl.light
        if light.channel is None or not light.enabled or light.intensity_percent <= 0:
            return AutofocusResult(False, None, (), (), self.metric,
                                   "light is off or unknown: select a channel and switch the light on", 0.0)
        lim = self.ctl.axis_limits(self.profile.focus_device, "z")
        lo, hi = center - span / 2, center + span / 2
        if lim:
            lo, hi = max(lo, lim[0]), min(hi, lim[1])
        zs = [float(z) for z in np.linspace(lo, hi, steps)]
        scores: list[float] = []
        self.cancel_event.clear()
        for i, z in enumerate(zs):
            if self.cancel_event.is_set():
                raise AutofocusCancelled("autofocus cancelled")
            if progress:
                progress(i, len(zs), f"z={z:.1f} µm")
            self.ctl.run(self.ctl.move_focus(z))
            frame = self.ctl.acquire(role, 1)[0]
            scores.append(fn(frame.data))
        prom = peak_prominence(scores)
        best, idx = parabolic_peak(zs, scores)
        if prom < self.min_prominence:
            self.ctl.run(self.ctl.move_focus(center if (not lim or lim[0] <= center <= lim[1]) else zs[len(zs) // 2]))
            return AutofocusResult(False, None, tuple(zs), tuple(scores), self.metric,
                                   f"no sample found (focus curve flat, prominence {prom:.3f})", prom)
        if idx in (0, len(zs) - 1):
            msg = "best focus at the edge of the search range; consider Find Sample or a wider range"
        else:
            msg = f"best focus at z={best:.2f} µm"
        self.ctl.run(self.ctl.move_focus(best))
        return AutofocusResult(True, best, tuple(zs), tuple(scores), self.metric, msg, prom)

    def find_sample(self, role: CameraRole, focus_nominal_um: float, focus_range_um: float,
                    progress=None) -> AutofocusResult:
        coarse = self.sweep(role, focus_nominal_um, focus_range_um, 17, progress)
        if not coarse.ok or coarse.best_z is None:
            return coarse
        step = focus_range_um / 16
        refined = self.sweep(role, coarse.best_z, 2 * step, 7, progress)
        return refined if refined.ok else coarse

    def auto_fine(self, role: CameraRole, range_percent: float = 100.0, progress=None) -> AutofocusResult:
        z = self.ctl.focus_z()
        if z is None:
            return AutofocusResult(False, None, (), (), self.metric, "focus position unknown", 0.0)
        span = fine_range_um(self.profile, self.ctl.objective_slot(), range_percent)
        return self.sweep(role, z, span, 11, progress)
