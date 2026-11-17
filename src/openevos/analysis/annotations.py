"""Measure-tab annotations (manual p.12-13): text box, arrow, line (intensity profile), rectangle, ellipse and
histogram ROI; up to 12 regions of interest; statistics per underlying channel; CSV export; burn-in.

Coordinates are image pixels. Statistics are computed on the raw data of every channel given, so a
measurement on an overlay reports each channel separately (as the original does from Image Capture).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from scipy import ndimage

MAX_ROIS = 12


class AnnKind(str, Enum):
    TEXT = "text"
    ARROW = "arrow"
    LINE = "line"
    RECT = "rect"
    ELLIPSE = "ellipse"
    HISTOGRAM = "histogram"

    @property
    def label(self) -> str:
        return {"text": "Text box", "arrow": "Arrow", "line": "Line", "rect": "Rectangle", "ellipse": "Ellipse",
                "histogram": "Histogram"}[self.value]

    @property
    def is_roi(self) -> bool:
        return self in (AnnKind.RECT, AnnKind.ELLIPSE, AnnKind.HISTOGRAM)


@dataclass
class Annotation:
    kind: AnnKind
    p0: tuple[float, float]
    p1: tuple[float, float]
    name: str = ""
    color: str = "#FFD24D"
    width: float = 2.0
    text: str = ""

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        x0, x1 = sorted((self.p0[0], self.p1[0]))
        y0, y1 = sorted((self.p0[1], self.p1[1]))
        return round(x0), round(y0), round(x1), round(y1)

    def length_px(self) -> float:
        return math.hypot(self.p1[0] - self.p0[0], self.p1[1] - self.p0[1])

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        h, w = shape
        x0, y0, x1, y1 = self.bbox
        m = np.zeros((h, w), bool)
        if self.kind in (AnnKind.RECT, AnnKind.HISTOGRAM):
            m[max(0, y0):min(h, y1 + 1), max(0, x0):min(w, x1 + 1)] = True
        elif self.kind is AnnKind.ELLIPSE:
            cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
            ry, rx = max(0.5, (y1 - y0) / 2), max(0.5, (x1 - x0) / 2)
            yy, xx = np.ogrid[:h, :w]
            m = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2 <= 1.0
        return m


@dataclass
class AnnotationSet:
    items: list[Annotation] = field(default_factory=list)
    visible: bool = True

    @property
    def roi_count(self) -> int:
        return sum(1 for a in self.items if a.kind.is_roi)

    def add(self, a: Annotation) -> Annotation:
        if a.kind.is_roi and self.roi_count >= MAX_ROIS:
            raise ValueError(f"at most {MAX_ROIS} regions of interest (manual p.12)")
        if not a.name:
            n = sum(1 for x in self.items if x.kind is a.kind) + 1
            a.name = f"{a.kind.label} {n}"
        self.items.append(a)
        return a

    def remove(self, index: int) -> None:
        self.items.pop(index)

    def clear(self) -> None:
        self.items.clear()


def roi_stats(a: Annotation, channels: dict[str, np.ndarray], pixel_um: float | None) -> list[dict]:
    rows = []
    for name, data in channels.items():
        img = data.mean(axis=2) if data.ndim == 3 else data
        if a.kind.is_roi:
            m = a.mask(img.shape)
            v = img[m].astype(np.float64)
            if v.size == 0:
                continue
            area = int(m.sum())
            rows.append({"annotation": a.name, "type": a.kind.label, "channel": name, "area_px": area,
                         "area_um2": round(area * pixel_um ** 2, 3) if pixel_um else None,
                         "mean": round(float(v.mean()), 2), "sd": round(float(v.std()), 2), "min": float(v.min()),
                         "max": float(v.max()), "integrated": round(float(v.sum()), 1)})
        elif a.kind is AnnKind.LINE:
            prof = line_profile(img, a.p0, a.p1)
            rows.append({"annotation": a.name, "type": a.kind.label, "channel": name,
                         "length_px": round(a.length_px(), 2),
                         "length_um": round(a.length_px() * pixel_um, 3) if pixel_um else None,
                         "mean": round(float(prof.mean()), 2), "min": float(prof.min()), "max": float(prof.max())})
    return rows


def line_profile(img: np.ndarray, p0: tuple[float, float], p1: tuple[float, float]) -> np.ndarray:
    n = max(2, int(math.hypot(p1[0] - p0[0], p1[1] - p0[1])) + 1)
    xs = np.linspace(p0[0], p1[0], n)
    ys = np.linspace(p0[1], p1[1], n)
    return ndimage.map_coordinates(img.astype(np.float64), [ys, xs], order=1)


def roi_histogram(a: Annotation, img: np.ndarray, bins: int = 64, max_value: int | None = None):
    g = img.mean(axis=2) if img.ndim == 3 else img
    v = g[a.mask(g.shape)]
    top = max_value or (int(v.max()) + 1 if v.size else 1)
    return np.histogram(v, bins=bins, range=(0, top))


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    cols = list(dict.fromkeys(k for r in rows for k in r))
    out = [",".join(cols)]
    for r in rows:
        out.append(",".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
    return "\n".join(out) + "\n"


def burn_in(rgb: np.ndarray, anns: AnnotationSet | None = None, *, pixel_um: float | None = None,
            units: str = "µm", count_markers: list[tuple[float, float, str, str]] | None = None,
            count_summary: list[str] | None = None, grid: int | None = None) -> np.ndarray:
    """Draw annotations / count tags / grid onto a copy of an 8-bit RGB image (for 'include annotations' and
    'save image as screenshot')."""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.fromarray(np.ascontiguousarray(rgb))
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(11, rgb.shape[1] // 60))
    except OSError:
        font = ImageFont.load_default()
    if grid:
        h, w = rgb.shape[:2]
        for x in range(0, w, w // grid):
            d.line([(x, 0), (x, h)], fill=(255, 255, 255), width=1)
        for y in range(0, h, h // grid):
            d.line([(0, y), (w, y)], fill=(255, 255, 255), width=1)
    if anns is not None and anns.visible:
        for a in anns.items:
            col = a.color
            w_ = max(1, int(a.width))
            x0, y0, x1, y1 = a.bbox
            if a.kind in (AnnKind.RECT, AnnKind.HISTOGRAM):
                d.rectangle([x0, y0, x1, y1], outline=col, width=w_)
            elif a.kind is AnnKind.ELLIPSE:
                d.ellipse([x0, y0, x1, y1], outline=col, width=w_)
            elif a.kind in (AnnKind.LINE, AnnKind.ARROW):
                d.line([a.p0, a.p1], fill=col, width=w_)
                if a.kind is AnnKind.ARROW:
                    ang = math.atan2(a.p1[1] - a.p0[1], a.p1[0] - a.p0[0])
                    for s in (+0.45, -0.45):
                        d.line([a.p1, (a.p1[0] - 12 * math.cos(ang + s), a.p1[1] - 12 * math.sin(ang + s))],
                               fill=col, width=w_)
            if a.kind is AnnKind.TEXT:
                d.text(a.p0, a.text or a.name, fill=col, font=font)
            else:
                tag = a.name
                if a.kind is AnnKind.LINE:
                    tag += f"  {a.length_px() * pixel_um:.2f} {units}" if pixel_um and units == "µm" \
                        else f"  {a.length_px():.1f} px"
                d.text((x0 + 3, max(0, y0 - 16)), tag, fill=col, font=font)
    for x, y, color, lab in count_markers or []:
        d.ellipse([x - 6, y - 6, x + 6, y + 6], outline=color, width=2)
        d.text((x + 7, y - 7), lab, fill=color, font=font)
    if count_summary:
        txt = "\n".join(count_summary)
        bb = d.multiline_textbbox((8, 8), txt, font=font)
        d.rectangle([bb[0] - 4, bb[1] - 4, bb[2] + 4, bb[3] + 4], fill=(15, 17, 21))
        d.multiline_text((8, 8), txt, fill=(243, 245, 247), font=font)
    return np.asarray(im)
