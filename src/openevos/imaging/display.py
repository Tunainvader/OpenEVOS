"""Display mapping with a Scientific mode and an edit log (Craftimon Image Adjust learnings).

* The raw frame is never changed: display settings map raw -> 8-bit for the screen and for colour saves.
* Scientific mode allows only whole-image, linear-in-pixel-value mappings (black/white point). Gamma
  (non-linear) is refused while it is on, and the log states that the display is linear.
* `log_lines()` is the text for a figure legend; it is written into every saved file's sidecar.
* Clipping overlay: pixels at/below the black point in blue, at/above the white point in red.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from openevos.imaging.composite import auto_range

CLIP_LOW = (64, 128, 255)
CLIP_HIGH = (255, 64, 64)


class NonLinearRefused(ValueError):
    pass


@dataclass(frozen=True)
class DisplaySettings:
    auto: bool = True
    lo: float = 0.0
    hi: float = 4095.0
    gamma: float = 1.0
    scientific: bool = True
    saturation_pct: float = 0.1  # auto range clips this % at each end

    def __post_init__(self) -> None:
        if self.scientific and abs(self.gamma - 1.0) > 1e-9:
            raise NonLinearRefused("Scientific mode allows linear display mappings only (gamma must be 1.0)")
        if self.hi <= self.lo:
            object.__setattr__(self, "hi", self.lo + 1.0)

    @property
    def linear(self) -> bool:
        return abs(self.gamma - 1.0) <= 1e-9

    def with_levels(self, lo: float, hi: float) -> DisplaySettings:
        return replace(self, auto=False, lo=float(lo), hi=float(max(hi, lo + 1)))

    def with_gamma(self, gamma: float) -> DisplaySettings:
        return replace(self, gamma=float(gamma))  # raises NonLinearRefused in scientific mode

    def resolved(self, raw: np.ndarray) -> tuple[float, float]:
        if self.auto:
            return auto_range(raw, self.saturation_pct, 100 - self.saturation_pct)
        return self.lo, self.hi

    def log_lines(self, raw: np.ndarray | None = None, bit_depth: int | None = None) -> list[str]:
        lo, hi = self.resolved(raw) if raw is not None else (self.lo, self.hi)
        lines = [f"Display: {'linear' if self.linear else 'NON-LINEAR'} mapping of raw values {lo:.0f}–{hi:.0f}"
                 + (f" (of 0–{(1 << bit_depth) - 1})" if bit_depth else "") + " to 0–255"
                 + (" (auto range, " + f"{self.saturation_pct:g}% clipped each end)" if self.auto else "")]
        if not self.linear:
            lines.append(f"Gamma {self.gamma:g} applied (non-linear; not valid for intensity comparison)")
        lines.append("Raw data unchanged; display mapping applied to the whole image.")
        lines.append("Scientific mode: on" if self.scientific else "Scientific mode: off")
        return lines


def map_to_8bit(raw: np.ndarray, ds: DisplaySettings) -> np.ndarray:
    lo, hi = ds.resolved(raw)
    f = (raw.astype(np.float32) - lo) / max(1e-6, hi - lo)
    f = np.clip(f, 0, 1)
    if not ds.linear:
        f = f ** (1.0 / ds.gamma)
    return (f * 255.0 + 0.5).astype(np.uint8)


def clipping_overlay(rgb: np.ndarray, raw: np.ndarray, ds: DisplaySettings) -> np.ndarray:
    a = raw.mean(axis=2) if raw.ndim == 3 else raw
    lo, hi = ds.resolved(raw)
    out = rgb.copy()
    out[a <= lo] = CLIP_LOW
    out[a >= hi] = CLIP_HIGH
    return out


def clipped_fractions(raw: np.ndarray, ds: DisplaySettings) -> tuple[float, float]:
    a = raw.mean(axis=2) if raw.ndim == 3 else raw
    lo, hi = ds.resolved(raw)
    return float((a <= lo).mean()), float((a >= hi).mean())
