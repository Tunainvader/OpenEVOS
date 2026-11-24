"""Saving captured images with the manual's format rules (Image > Save, p.15-17):

* Default: 8-bit colour TIFF including annotations.
* 16-bit is only possible as TIFF *and* monochrome (raw camera data, no annotations burnt in).
* JPEG and PNG, and anything saved as colour (composites, pseudo colour, colour camera), are 8-bit.
* "Save underlying channels" writes each channel of an overlay as well (Mono settings: underlying
  channel save as 8-bit colour or 16-bit monochrome; 16-bit fills 0-65535 like the main 16-bit save, and the sidecar
  and each file's description record the counts scale).

Each image gets a JSON sidecar with acquisition metadata and provenance; simulated frames are
labelled SIMULATED there and in the TIFF description.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from openevos.acquisition.capture import CapturedImage
from openevos.instrument.settings import ImageFormat


class SaveError(ValueError):
    pass


@dataclass(frozen=True)
class SaveOptions:
    image_format: ImageFormat = ImageFormat.TIFF
    bit_depth: int = 8
    as_color: bool = True
    include_annotations: bool = True
    save_underlying: bool = False
    underlying_mode: str = "16bit_mono"  # "8bit_color" | "16bit_mono"


class SavePreset(str, Enum):
    """Save presets named in the manual (p.10): Default, Quantitative Monochrome, Custom."""

    DEFAULT = "default"
    QUANTITATIVE_MONO = "quantitative_mono"
    CUSTOM = "custom"

    @property
    def label(self) -> str:
        return {"default": "Default", "quantitative_mono": "Quantitative Monochrome", "custom": "Custom"}[self.value]

    def options(self, base: SaveOptions | None = None) -> SaveOptions:
        if self is SavePreset.DEFAULT:
            return SaveOptions(ImageFormat.TIFF, 8, True, True)
        if self is SavePreset.QUANTITATIVE_MONO:  # the camera's own counts, unscaled (12-bit mono camera)
            return SaveOptions(ImageFormat.TIFF, 12, False, False)
        return base or SaveOptions()


def validate(opts: SaveOptions, *, source_is_color: bool, source_bits: int | None = None) -> list[str]:
    """`source_bits`: the raw capture's native bit depth (12 for the mono camera), when known."""
    errs = []
    if opts.bit_depth not in (8, 12, 16):
        errs.append("bit depth must be 8, 12 or 16")
    if opts.bit_depth in (12, 16):
        b = opts.bit_depth
        if opts.image_format is not ImageFormat.TIFF:
            errs.append(f"{b}-bit is only available for TIFF")
        if opts.as_color or source_is_color:
            errs.append(f"{b}-bit requires monochrome (colour images are 8-bit)")
        if opts.include_annotations:
            errs.append(f"annotations cannot be burnt into {b}-bit raw data; save them as colour 8-bit")
    if opts.bit_depth == 12 and source_bits is not None and source_bits > 12:
        errs.append(f"12-bit keeps the camera's counts unscaled; this capture is {source_bits}-bit (save 16-bit)")
    if source_is_color and not opts.as_color:
        errs.append("a colour-camera image cannot be saved as monochrome")
    return errs


def allowed_bit_depths(fmt: ImageFormat, as_color: bool) -> tuple[int, ...]:
    return (8, 12, 16) if fmt is ImageFormat.TIFF and not as_color else (8,)


def mono_pixels(raw: CapturedImage, bit_depth: int) -> tuple[np.ndarray, int]:
    """The monochrome pixels for a save at `bit_depth`, and the factor applied to the camera's counts.

    12-bit: the camera's counts unchanged (0-4095 in a 16-bit TIFF container, the quantitative choice).
    16-bit: the counts scaled by 2**(16 - native bits) to fill 0-65535 (exact, reversible; opens at normal brightness
    in ordinary viewers). 8-bit: auto-ranged for display."""
    if bit_depth == 8:
        return _mono8(raw.data), 0
    if bit_depth == 16 and 0 < raw.bit_depth < 16:
        shift = 16 - raw.bit_depth
        return (raw.data.astype(np.uint16) << shift).astype(np.uint16), 1 << shift
    return raw.data, 1


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def next_free_path(folder: Path, base: str, suffix: str) -> Path:
    base = _SAFE.sub("_", base).strip() or "Image"
    i = 1
    while True:
        p = folder / f"{base}_{i:04d}{suffix}"
        if not p.exists() and not p.with_suffix(".json").exists():
            return p
        i += 1


@dataclass
class SavedFiles:
    image: Path
    sidecar: Path
    underlying: list[Path] = field(default_factory=list)


def save(folder: Path, base: str, *, rgb8: np.ndarray | None, raw: CapturedImage | None,
         opts: SaveOptions, channels: dict[str, CapturedImage] | None = None,
         extra_metadata: dict | None = None) -> SavedFiles:
    """`rgb8` is the displayed (composited/annotated) 8-bit RGB image; `raw` the primary capture."""
    errs = validate(opts, source_is_color=raw.is_color if raw is not None else True,
                    source_bits=raw.bit_depth if raw is not None else None)
    if errs:
        raise SaveError("; ".join(errs))
    folder.mkdir(parents=True, exist_ok=True)
    path = next_free_path(folder, base, opts.image_format.suffix)
    scale = 0
    if opts.as_color:
        if rgb8 is None:
            raise SaveError("nothing to save in colour")
        data = rgb8
    else:
        if raw is None:
            raise SaveError("monochrome save needs a raw capture")
        data, scale = mono_pixels(raw, opts.bit_depth)
    meta = {"openevos_save": {"format": opts.image_format.value, "bit_depth": opts.bit_depth,
                              "color": opts.as_color, "annotations": opts.include_annotations and opts.as_color,
                              # pixel = camera count x counts_scale (12-bit: 1; 16-bit: 16 for the 12-bit camera;
                              # 0 = auto-ranged 8-bit display data, not counts)
                              **({} if opts.as_color else {"counts_scale": scale})},
            "primary": raw.metadata() if raw else None,
            "channels": {k: v.metadata() for k, v in (channels or {}).items()},
            **(extra_metadata or {})}
    synthetic = (raw is not None and raw.synthetic) or any(v.synthetic for v in (channels or {}).values())
    meta["provenance"] = "SIMULATED — synthetic simulator frames, not measurements" if synthetic else "camera data"
    # Underlying channel files. "16bit_mono" fills 0-65535 exactly as the main 16-bit save does (counts x 2**(16 -
    # native bits)): operator 2026-11-24, the unscaled 12-bit counts opened nearly black in ordinary viewers.
    under: list[tuple[str, Path, np.ndarray, ImageFormat, int]] = []  # slot, path, pixels, format, counts scale
    if opts.save_underlying and channels:
        for slot, img in channels.items():
            if opts.underlying_mode == "16bit_mono" and not img.is_color:
                px, k = mono_pixels(img, 16)
                under.append((slot, path.with_name(f"{path.stem}_{slot}.tif"), px, ImageFormat.TIFF, k))
            else:
                under.append((slot, path.with_name(f"{path.stem}_{slot}{opts.image_format.suffix}"),
                              img.data if img.is_color else _mono8(img.data), opts.image_format, 0))
        meta["openevos_save"]["underlying"] = {"mode": opts.underlying_mode,
                                               "files": {s: p.name for s, p, _d, _f, _k in under},
                                               "counts_scale": {s: k for s, _p, _d, _f, k in under}}
    _write(path, data, opts.image_format, json.dumps(meta, default=str))
    sidecar = path.with_suffix(".json")
    sidecar.write_text(json.dumps(meta, indent=2, default=str), "utf-8")
    out = SavedFiles(path, sidecar)
    for slot, p, px, fmt, k in under:
        _write(p, px, fmt, json.dumps({**(channels or {})[slot].metadata(), "counts_scale": k}, default=str))
        out.underlying.append(p)
    return out


def _mono8(a: np.ndarray) -> np.ndarray:
    from openevos.imaging.composite import auto_range, to_8bit

    lo, hi = auto_range(a)
    return to_8bit(a, lo, hi)


def _write(path: Path, data: np.ndarray, fmt: ImageFormat, description: str) -> None:
    if fmt is ImageFormat.TIFF:
        import tifffile

        tifffile.imwrite(path, np.ascontiguousarray(data), description=description, metadata=None,
                         photometric="rgb" if data.ndim == 3 else "minisblack")
        return
    from PIL import Image

    if data.dtype != np.uint8:
        raise SaveError(f"{fmt.value.upper()} is 8-bit only")
    im = Image.fromarray(data)
    if fmt is ImageFormat.JPEG:
        im.save(path, quality=95)
    else:
        im.save(path)
