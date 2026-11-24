"""Display helpers for the Z-stack viewer (design v4): merged channels, XZ / YZ cross-sections, the sharp range of a
focus curve. Pure numpy, display only: nothing here changes or saves raw pixel values.

* ``merge_rgb``: channels rendered in their display colours, added (Fiji Merge Channels, ZEN's multichannel view).
  Transmitted-light and phase channels are the grey base at half brightness, so fluorescence stays readable on top.
* ``ortho_composite``: XY with the YZ section to its right and the XZ section below (Fiji Orthogonal Views, ZEN
  Ortho). Z is drawn to scale from the step and the pixel size; when that would be too thin to read it is stretched,
  and the factor is returned so the page can say so.
* ``sharp_range``: where the focus curve stays above half its height (full width at half maximum). A rough "depth in
  focus" from a relative sharpness score; not an optical measurement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


def merge_rgb(layers: Sequence[tuple[np.ndarray, bool]]) -> np.ndarray:
    """`layers` = [(rgb8 image, is_fluorescence)]. Returns uint8 RGB: brightfield base at 50 %, fluorescence added."""
    if not layers:
        raise ValueError("no channels to merge")
    shape = layers[0][0].shape[:2]
    acc = np.zeros((*shape, 3), dtype=np.float32)
    for rgb, fluo in layers:
        if rgb.shape[:2] != shape:
            continue  # another camera's frame size: not merged
        a = rgb[..., :3].astype(np.float32)
        acc += a if fluo else a * 0.5
    return np.clip(acc, 0, 255).astype(np.uint8)


def resample_rows(n_in: int, n_out: int) -> np.ndarray:
    """Nearest-neighbour source index for each of `n_out` rows spread over `n_in` slices."""
    n_out = max(1, int(n_out))
    if n_in <= 1:
        return np.zeros(n_out, dtype=int)
    return np.clip(np.floor((np.arange(n_out) + 0.5) * n_in / n_out), 0, n_in - 1).astype(int)


@dataclass(frozen=True)
class OrthoLayout:
    width: int  # XY width (px)
    height: int  # XY height
    depth: int  # rows (XZ) / columns (YZ) the stack takes on screen
    gap: int
    stretch: float  # >1: Z drawn this many times taller than true scale (too thin otherwise)
    to_scale: bool  # False: the pixel size or the step is unknown, so Z is not to scale

    def region(self, x: float, y: float) -> str:
        """Which panel a canvas point is in: "xy", "yz", "xz" or ""."""
        if x < self.width and y < self.height:
            return "xy"
        if x >= self.width + self.gap and y < self.height:
            return "yz"
        if y >= self.height + self.gap and x < self.width:
            return "xz"
        return ""

    def slice_at(self, offset: float, n: int) -> int:
        """Slice index for a position `offset` px into the XZ rows / YZ columns (top / left = first slice)."""
        return int(min(max(offset / max(1, self.depth) * n, 0), n - 1))


def ortho_composite(vol: np.ndarray, x: int, y: int, i: int, *, step_um: float | None, pixel_um: float | None,
                    min_fraction: float = 0.125, gap: int = 4, background: tuple[int, int, int] = (40, 44, 52),
                    cross: tuple[int, int, int] = (239, 171, 87), plane: tuple[int, int, int] = (98, 147, 255),
                    ) -> tuple[np.ndarray, OrthoLayout]:
    """`vol` = (slices, H, W, 3) uint8 rendered slices, first slice = bottom of the stack. Returns the composite and
    its layout. The XZ / YZ sections put the first slice at the top (row 0) and left (column 0), as Fiji does; the
    current slice `i` is marked in blue, the cut through (x, y) in amber."""
    n, h, w = vol.shape[:3]
    x, y, i = int(min(max(x, 0), w - 1)), int(min(max(y, 0), h - 1)), int(min(max(i, 0), n - 1))
    to_scale = step_um is not None and pixel_um is not None and step_um > 0 and pixel_um > 0
    true_depth = n * (step_um / pixel_um) if step_um is not None and pixel_um is not None and to_scale else float(n)
    least = max(n, int(min(h, w) * min_fraction))
    depth = round(max(true_depth, least))
    depth = min(depth, max(n, min(h, w)))  # never taller than the image itself
    stretch = depth / true_depth if true_depth > 0 else 1.0
    rows = resample_rows(n, depth)
    xz = vol[rows, y, :, :]  # (depth, W, 3)
    yz = vol[rows, :, x, :].transpose(1, 0, 2)  # (H, depth, 3)
    canvas = np.empty((h + gap + depth, w + gap + depth, 3), dtype=np.uint8)
    canvas[:] = background
    canvas[:h, :w] = vol[i]
    canvas[:h, w + gap:] = yz
    canvas[h + gap:, :w] = xz
    # the slice shown, on both sections
    rows_i = np.nonzero(rows == i)[0]
    if rows_i.size:
        r0, r1 = int(rows_i[0]), int(rows_i[-1]) + 1
        canvas[h + gap + r0:h + gap + r1, :w] = (canvas[h + gap + r0:h + gap + r1, :w] * 0.4 + np.array(plane) * 0.6)
        canvas[:h, w + gap + r0:w + gap + r1] = (canvas[:h, w + gap + r0:w + gap + r1] * 0.4 + np.array(plane) * 0.6)
    # the cut: x through XY and XZ, y through XY and YZ
    canvas[:h, x] = cross
    canvas[h + gap:, x] = cross
    canvas[y, :w] = cross
    canvas[y, w + gap:] = cross
    return canvas, OrthoLayout(w, h, depth, gap, round(stretch, 2), to_scale)


def sharp_range(curve: Sequence[float] | np.ndarray) -> tuple[float, float] | None:
    """Fractional slice indices where the curve crosses half its height (above its minimum), around its peak.
    None when the curve is flat, or the peak's half height is not crossed on both sides (the stack does not span the
    in-focus region)."""
    v = np.asarray(curve, dtype=float)
    if v.size < 3 or not np.isfinite(v).all():
        return None
    lo, hi = float(v.min()), float(v.max())
    if hi - lo <= 1e-12 * max(1.0, abs(hi)):
        return None
    half = lo + (hi - lo) / 2
    b = int(np.argmax(v))
    left = right = None
    for k in range(b, 0, -1):
        if v[k - 1] < half <= v[k]:
            left = (k - 1) + (half - v[k - 1]) / (v[k] - v[k - 1])
            break
    for k in range(b, v.size - 1):
        if v[k + 1] < half <= v[k]:
            right = k + (v[k] - half) / (v[k] - v[k + 1])
            break
    if left is None or right is None:
        return None
    return float(left), float(right)


def z_at(index: float, z: Sequence[float] | np.ndarray) -> float:
    """Z at a fractional slice index (linear between slices)."""
    zz = np.asarray(z, dtype=float)
    return float(np.interp(index, np.arange(zz.size), zz))
