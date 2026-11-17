"""Counting (manual Image > Edit > Count, p.13-14).

* Manual count: click to add markers under up to six labels; counts per label.
* Auto count: Brightness, Area and Circularity sliders 0-100 %; moving a slider toward 100 %
  makes detection more sensitive (lower intensity threshold, smaller minimum area, lower minimum
  circularity). The mapping from slider to threshold is an OpenEVOS design, documented here and
  in the result so a count is reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from skimage import filters, measure, segmentation

MAX_LABELS = 6
DEFAULT_LABEL_COLORS = ("#FF5A5A", "#4DD8FF", "#FFD24D", "#7CFF6B", "#C88BFF", "#FF9F43")


@dataclass
class ManualCount:
    labels: list[str] = field(default_factory=lambda: ["Label 1"])
    markers: list[tuple[float, float, int]] = field(default_factory=list)  # x, y (px), label index

    def add_label(self, name: str) -> int:
        if len(self.labels) >= MAX_LABELS:
            raise ValueError(f"at most {MAX_LABELS} count labels")
        self.labels.append(name)
        return len(self.labels) - 1

    def add(self, x: float, y: float, label: int) -> None:
        if not 0 <= label < len(self.labels):
            raise ValueError("unknown label")
        self.markers.append((x, y, label))

    def remove_near(self, x: float, y: float, radius: float = 8.0) -> bool:
        if not self.markers:
            return False
        d = [math.hypot(mx - x, my - y) for mx, my, _ in self.markers]
        i = int(np.argmin(d))
        if d[i] <= radius:
            self.markers.pop(i)
            return True
        return False

    def percentages(self) -> dict[str, float]:
        """Running tally with percentages per label (manual p.14)."""
        c = self.counts()
        total = sum(c.values())
        return {k: (100.0 * v / total if total else 0.0) for k, v in c.items()}

    def counts(self) -> dict[str, int]:
        c = {name: 0 for name in self.labels}
        for _, _, k in self.markers:
            c[self.labels[k]] += 1
        return c

    def clear(self) -> None:
        self.markers.clear()


@dataclass(frozen=True)
class AutoCountParams:
    brightness: float = 50.0
    area: float = 50.0
    circularity: float = 50.0
    invert: bool = False  # dark objects on bright background (brightfield)
    split_touching: bool = True  # "Separate touching cells"
    aggressive: bool = False  # "Aggressive boundaries": tighter boundaries -> more, smaller ROI


@dataclass(frozen=True)
class CountedObject:
    label: int
    centroid: tuple[float, float]  # x, y px
    area_px: int
    area_um2: float | None
    circularity: float
    mean_intensity: float
    bbox: tuple[int, int, int, int]


@dataclass(frozen=True)
class AutoCountResult:
    objects: tuple[CountedObject, ...]
    threshold: float
    min_area_px: int
    min_circularity: float
    label_image: np.ndarray
    params: AutoCountParams

    @property
    def count(self) -> int:
        return len(self.objects)

    def summary(self, pixel_size_um: float | None = None) -> dict:
        areas = [o.area_px for o in self.objects]
        out = {"count": self.count, "threshold": round(self.threshold, 2), "min_area_px": self.min_area_px,
               "min_circularity": round(self.min_circularity, 3),
               "mean_area_px": round(float(np.mean(areas)), 1) if areas else 0.0,
               "mean_circularity": round(float(np.mean([o.circularity for o in self.objects])), 3) if areas else 0.0}
        if pixel_size_um:
            out["mean_area_um2"] = round(out["mean_area_px"] * pixel_size_um ** 2, 1)
        return out


def slider_mapping(p: AutoCountParams, otsu: float, background: float) -> tuple[float, int, float]:
    b = min(100.0, max(0.0, p.brightness)) / 100.0
    thr = background + (otsu - background) * (2.0 - 2.0 * b) if b < 1 else background + 1e-6
    min_area = round(4 + 400 * (1 - min(100.0, max(0.0, p.area)) / 100.0) ** 2)
    min_circ = 0.9 * (1 - min(100.0, max(0.0, p.circularity)) / 100.0)
    return thr, min_area, min_circ


def _drop_small(mask: np.ndarray, min_px: int) -> np.ndarray:
    lab, n = ndimage.label(mask)
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    keep = sizes >= min_px
    keep[0] = False
    return keep[lab]


def auto_count(image: np.ndarray, params: AutoCountParams | None = None, *,
               pixel_size_um: float | None = None, roi: np.ndarray | None = None) -> AutoCountResult:
    params = params or AutoCountParams()
    a = image.mean(axis=2) if image.ndim == 3 else image
    a = a.astype(np.float32)
    if params.invert:
        a = a.max() - a
    sm = ndimage.gaussian_filter(a, 1.5)
    background = float(np.percentile(sm, 20))
    try:
        otsu = float(filters.threshold_otsu(sm))
    except ValueError:
        otsu = background + 1
    thr, min_area, min_circ = slider_mapping(params, otsu, background)
    mask = sm > thr
    if roi is not None:
        mask &= roi.astype(bool)
    mask = ndimage.binary_fill_holes(_drop_small(mask, max(1, min_area // 2)))
    if params.aggressive and mask.any():
        # tighten boundaries: per region, keep pixels above half of (region peak - background); soft edges
        # shrink and touching objects separate. Smaller pieces are accepted.
        lab0, n0 = ndimage.label(mask)
        peaks = np.asarray(ndimage.maximum(sm, lab0, index=np.arange(1, n0 + 1)), dtype=np.float32)
        half = np.concatenate([[np.inf], background + 0.5 * (peaks - background)]).astype(np.float32)
        mask &= sm > half[lab0]
        min_area = max(4, int(min_area * 0.6))
    if params.split_touching and mask.any():
        dist = ndimage.distance_transform_edt(mask)
        peaks = (dist == ndimage.maximum_filter(dist, size=5 if params.aggressive else 7)) & (dist > 2)
        markers, _ = ndimage.label(peaks)
        lab = segmentation.watershed(-dist, markers, mask=mask)
    else:
        lab, _ = ndimage.label(mask)
    objs = []
    keep = np.zeros_like(lab)
    k = 0
    for rp in measure.regionprops(lab, intensity_image=a):
        if rp.area < min_area:
            continue
        perim = rp.perimeter if rp.perimeter > 0 else 1.0
        circ = float(min(1.0, 4 * math.pi * rp.area / (perim * perim)))
        if circ < min_circ:
            continue
        k += 1
        keep[lab == rp.label] = k
        cy, cx = rp.centroid
        objs.append(CountedObject(k, (float(cx), float(cy)), int(rp.area),
                                  rp.area * pixel_size_um ** 2 if pixel_size_um else None, circ,
                                  float(a[lab == rp.label].mean()), tuple(int(v) for v in rp.bbox)))
    return AutoCountResult(tuple(objs), thr, min_area, min_circ, keep, params)
