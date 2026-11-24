"""Z projections and focus measures for Z-stacks (ADR-0064).

* ``max`` / ``min`` / ``mean`` / ``sum`` / ``median`` / ``std``: the intensity projections of Fiji's Z Project
  (Average, Max, Min, Sum, Median, Standard Deviation). Max suits fluorescence; min suits dark objects on a bright
  background; std shows what changes through the stack.
* ``best``: the single sharpest slice, named "Sharpest plane" (design review 2026-11-25: one whole slice chosen by a
  global score, not a region-wise best-focus fusion such as MetaXpress Best Focus).
* ``edf``: extended depth of field. Each pixel comes from the slice where its neighbourhood is sharpest (local
  Laplacian energy), with the choice map smoothed so neighbouring pixels come from neighbouring slices (as ZEN's
  variance method, suited to brightfield and phase; fluorescence often looks the same as max).

Pixel values are never rescaled. max/min/mean/median/best/edf return the input dtype (best, edf and max/min return
values that were measured, each pixel from one slice). ``sum`` returns uint32 for integer slices (200 x 16-bit cannot
overflow it) and float32 otherwise; ``std`` returns float32. Colour slices are judged on their luminance and keep
their colour. Memory: projections run slice by slice (two image-sized arrays), never stacking 200 slices in float;
median works on bands of rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

METHODS: dict[str, str] = {
    "max": "Maximum",
    "min": "Minimum",
    "mean": "Mean",
    "sum": "Sum",
    "median": "Median",
    "std": "Standard deviation",
    "best": "Sharpest plane",
    "edf": "Extended focus",
}
#: methods whose output is derived arithmetic (not measured pixel values) and may leave the camera's range
DERIVED = frozenset({"sum", "std"})


def _luma(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    if a.ndim == 3:
        return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
    return a


def _laplacian(g: np.ndarray) -> np.ndarray:
    from scipy import ndimage

    return ndimage.laplace(g)


def focus_measure(img: np.ndarray, *, max_side: int = 512) -> float:
    """Variance of the Laplacian (higher = sharper), on a copy reduced to at most `max_side` pixels a side."""
    g = _luma(img)
    k = max(1, int(np.ceil(max(g.shape) / max_side)))
    if k > 1:
        g = g[::k, ::k]
    return float(np.var(_laplacian(g)))


def focus_curve(images: Sequence[np.ndarray]) -> np.ndarray:
    return np.array([focus_measure(np.asarray(i)) for i in images], dtype=float)


def best_index(images: Sequence[np.ndarray]) -> int:
    return int(np.argmax(focus_curve(images))) if len(images) else 0


def project(images: Sequence[np.ndarray], method: str, *, edf_radius: int = 4) -> np.ndarray:
    if not len(images):
        raise ValueError("no slices")
    if method not in METHODS:
        raise ValueError(f"unknown projection {method!r} (one of {', '.join(METHODS)})")
    first = np.asarray(images[0])
    if method in ("max", "min"):
        out = first.copy()
        f = np.maximum if method == "max" else np.minimum
        for a in images[1:]:
            f(out, np.asarray(a), out=out)
        return out
    if method == "mean":
        acc = np.zeros(first.shape, dtype=np.float64)
        for a in images:
            acc += np.asarray(a)
        acc /= len(images)
        if np.issubdtype(first.dtype, np.integer):
            return np.clip(np.rint(acc), np.iinfo(first.dtype).min, np.iinfo(first.dtype).max).astype(first.dtype)
        return acc.astype(first.dtype)
    if method == "sum":
        integer = np.issubdtype(first.dtype, np.integer)
        out_sum = np.zeros(first.shape, dtype=np.uint64 if integer else np.float64)
        for a in images:
            out_sum += np.asarray(a)
        if integer:
            return np.minimum(out_sum, np.iinfo(np.uint32).max).astype(np.uint32)
        return out_sum.astype(np.float32)
    if method == "std":
        mean = np.zeros(first.shape, dtype=np.float64)
        sq = np.zeros(first.shape, dtype=np.float64)
        for a in images:
            fa = np.asarray(a, dtype=np.float64)
            mean += fa
            sq += fa * fa
        n = len(images)
        mean /= n
        return np.sqrt(np.maximum(sq / n - mean * mean, 0.0)).astype(np.float32)  # population std, as Fiji
    if method == "median":
        return _median(images)
    if method == "best":
        return np.asarray(images[best_index(images)]).copy()
    return _edf(images, edf_radius)


def _median(images: Sequence[np.ndarray], rows: int = 128) -> np.ndarray:
    """Per-pixel median, a band of rows at a time (a 200-slice float stack never sits in memory at once)."""
    first = np.asarray(images[0])
    out = np.empty_like(first)
    for r0 in range(0, first.shape[0], rows):
        band = np.stack([np.asarray(a)[r0:r0 + rows] for a in images])
        m = np.median(band, axis=0)
        if np.issubdtype(first.dtype, np.integer):
            m = np.rint(m)
        out[r0:r0 + rows] = m.astype(first.dtype)
    return out


def _edf(images: Sequence[np.ndarray], radius: int) -> np.ndarray:
    from scipy import ndimage

    size = 2 * radius + 1
    best_e: np.ndarray | None = None
    idx = np.zeros(np.asarray(images[0]).shape[:2], dtype=np.int16)
    for i, a in enumerate(images):
        e = ndimage.uniform_filter(_laplacian(_luma(a)) ** 2, size=size)
        if best_e is None:
            best_e = e
            continue
        better = e > best_e
        idx[better] = i
        best_e = np.where(better, e, best_e)
    idx = ndimage.median_filter(idx, size=size)  # neighbours from neighbouring slices: no salt-and-pepper seams
    out = np.asarray(images[0]).copy()
    for i, a in enumerate(images[1:], start=1):
        m = idx == i
        if m.any():
            out[m] = np.asarray(a)[m]
    return out
