"""Focus metrics for autofocus. Higher = sharper. All work on 2-D float arrays."""

from __future__ import annotations

import numpy as np
from scipy import ndimage


def _gray(arr: np.ndarray) -> np.ndarray:
    a = arr.mean(axis=2) if arr.ndim == 3 else arr
    return a.astype(np.float64)


def normalized_variance(arr: np.ndarray) -> float:
    a = _gray(arr)
    m = a.mean()
    return float(a.var() / m) if m > 0 else 0.0


def brenner(arr: np.ndarray) -> float:
    a = _gray(arr)
    d = a[:, 2:] - a[:, :-2]
    return float((d * d).mean())


def tenengrad(arr: np.ndarray) -> float:
    a = ndimage.gaussian_filter(_gray(arr), 1.0)
    gx, gy = ndimage.sobel(a, 1), ndimage.sobel(a, 0)
    return float((gx * gx + gy * gy).mean())


METRICS = {"normalized_variance": normalized_variance, "brenner": brenner, "tenengrad": tenengrad}


def parabolic_peak(z: list[float], s: list[float]) -> tuple[float, int]:
    """Best z with 3-point parabolic refinement around the maximum sample."""
    i = int(np.argmax(s))
    if 0 < i < len(s) - 1:
        z0, z1, z2 = z[i - 1], z[i], z[i + 1]
        s0, s1, s2 = s[i - 1], s[i], s[i + 1]
        denom = (s0 - 2 * s1 + s2)
        if denom < 0 and abs(z2 - z1 - (z1 - z0)) < 1e-6:
            return z1 + 0.5 * (s0 - s2) / denom * (z1 - z0), i
    return z[i], i


def peak_prominence(s: list[float]) -> float:
    """(max - median) / median: a flat curve (no sample) gives ~0."""
    a = np.asarray(s, float)
    med = float(np.median(a))
    return float((a.max() - med) / med) if med > 0 else 0.0
