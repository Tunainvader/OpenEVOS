"""Display transforms: auto range, 8-bit mapping, pseudo colour, multichannel overlay, saturation.

Manual basis: Apply Pseudo Color / grayscale p.9, custom channel pseudo colours p.19/p.21,
Highlight Saturated Pixels in Red p.19, Overlay (multichannel) p.11-12, Transfection overlay
(one fluorescence + one transmitted image) p.21.
Display transforms never modify the raw data that is saved as 16-bit monochrome.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    c = color.lstrip("#")
    if len(c) != 6:
        raise ValueError(f"bad colour {color!r}")
    return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def auto_range(arr: np.ndarray, low_pct: float = 0.1, high_pct: float = 99.9) -> tuple[float, float]:
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    sample = arr[:: max(1, arr.shape[0] // 256), :: max(1, arr.shape[1] // 256)]
    lo, hi = np.percentile(sample, [low_pct, high_pct])
    if hi <= lo:
        hi = lo + 1
    return float(lo), float(hi)


def to_8bit(arr: np.ndarray, lo: float, hi: float, gamma: float = 1.0) -> np.ndarray:
    f = (arr.astype(np.float32) - lo) / max(1e-6, hi - lo)
    f = np.clip(f, 0, 1)
    if gamma != 1.0:
        f = f ** (1.0 / gamma)
    return (f * 255.0 + 0.5).astype(np.uint8)


def pseudocolor(gray8: np.ndarray, color: str) -> np.ndarray:
    r, g, b = hex_to_rgb(color)
    out = np.empty((*gray8.shape, 3), np.uint8)
    for i, k in enumerate((r, g, b)):
        out[..., i] = (gray8.astype(np.float32) * k).astype(np.uint8)
    return out


def gray_rgb(gray8: np.ndarray) -> np.ndarray:
    return np.repeat(gray8[..., None], 3, axis=2)


@dataclass(frozen=True)
class Layer:
    data: np.ndarray  # raw mono (uint16) or RGB uint8
    color: str = "#FFFFFF"
    lo: float | None = None
    hi: float | None = None
    visible: bool = True
    opacity: float = 1.0


def render_layer(layer: Layer, pseudo: bool = True) -> np.ndarray:
    if layer.data.ndim == 3:
        return layer.data.astype(np.uint8)
    lo, hi = (layer.lo, layer.hi) if layer.lo is not None and layer.hi is not None else auto_range(layer.data)
    g8 = to_8bit(layer.data, lo, hi)
    return pseudocolor(g8, layer.color) if pseudo else gray_rgb(g8)


def composite(layers: Sequence[Layer], pseudo: bool = True) -> np.ndarray:
    """Additive blend of visible layers (screen-like, clipped). Shapes must match."""
    vis = [layer for layer in layers if layer.visible]
    if not vis:
        raise ValueError("no visible layers")
    shape = vis[0].data.shape[:2]
    acc = np.zeros((*shape, 3), np.float32)
    for layer in vis:
        if layer.data.shape[:2] != shape:
            raise ValueError("layer sizes differ; captures must come from the same camera/objective")
        acc += render_layer(layer, pseudo).astype(np.float32) * layer.opacity
    return np.clip(acc, 0, 255).astype(np.uint8)


def transfection(fluor: Layer, transmitted: Layer, fluor_weight: float = 1.0, trans_weight: float = 0.6) -> np.ndarray:
    """Two-image overlay: transmitted light in grey under one pseudo-coloured fluorescence channel."""
    base = render_layer(Layer(transmitted.data, "#FFFFFF", transmitted.lo, transmitted.hi), pseudo=False)
    top = render_layer(fluor, pseudo=True)
    return np.clip(base.astype(np.float32) * trans_weight + top.astype(np.float32) * fluor_weight, 0, 255).astype(np.uint8)  # noqa: E501


def highlight_saturated(rgb: np.ndarray, raw: np.ndarray, max_value: int) -> np.ndarray:
    """Paint saturated pixels red (display only)."""
    out = rgb.copy()
    sat = raw >= max_value if raw.ndim == 2 else (raw >= max_value).any(axis=2)
    out[sat] = (255, 0, 0)
    return out


def adjust_color(rgb: np.ndarray, brightness: float = 0, contrast: float = 0, saturation: float = 0,
                 hue: float = 0) -> np.ndarray:
    """Colour-camera adjustments (manual p.20): brightness/contrast/saturation in -100..100, hue degrees."""
    f = rgb.astype(np.float32) / 255.0
    if hue:
        f = _rotate_hue(f, hue)
    if saturation:
        gray = f.mean(axis=2, keepdims=True)
        f = gray + (f - gray) * (1 + saturation / 100.0)
    if contrast:
        f = (f - 0.5) * (1 + contrast / 100.0) + 0.5
    if brightness:
        f = f + brightness / 200.0
    return (np.clip(f, 0, 1) * 255 + 0.5).astype(np.uint8)


def _rotate_hue(f: np.ndarray, degrees: float) -> np.ndarray:
    a = np.deg2rad(degrees)
    c, s = np.cos(a), np.sin(a)
    k = 1 / 3.0
    sq = np.sqrt(k)
    m = np.array([
        [c + (1 - c) * k, k * (1 - c) - sq * s, k * (1 - c) + sq * s],
        [k * (1 - c) + sq * s, c + k * (1 - c), k * (1 - c) - sq * s],
        [k * (1 - c) - sq * s, k * (1 - c) + sq * s, c + k * (1 - c)],
    ], np.float32)
    return f @ m.T


def histogram(arr: np.ndarray, bins: int = 128, max_value: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    a = arr.mean(axis=2) if arr.ndim == 3 else arr
    top = max_value if max_value is not None else (255 if arr.dtype == np.uint8 else int(a.max()) + 1)
    return np.histogram(a, bins=bins, range=(0, max(1, top)))
