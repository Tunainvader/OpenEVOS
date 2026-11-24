"""OME-TIFF export (open format for Fiji/QuPath/napari): physical pixel size, channel names, Z step, time.

Arrays are written as TZCYX (singleton axes kept so readers know what each axis is).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import numpy as np


def write_ome_tiff(path: Path, data: np.ndarray, *, axes: str = "TZCYX", pixel_um: float | None = None,
                   z_step_um: float | None = None, t_interval_s: float | None = None,
                   channel_names: Sequence[str] | None = None, description: str = "") -> Path:
    import tifffile

    if data.ndim != len(axes):
        raise ValueError(f"data has {data.ndim} dimensions, axes '{axes}' has {len(axes)}")
    md: dict = {"axes": axes}
    if pixel_um:
        md.update(PhysicalSizeX=float(pixel_um), PhysicalSizeXUnit="µm", PhysicalSizeY=float(pixel_um),
                  PhysicalSizeYUnit="µm")
    if z_step_um:
        md.update(PhysicalSizeZ=float(z_step_um), PhysicalSizeZUnit="µm")
    if t_interval_s:
        md.update(TimeIncrement=float(t_interval_s), TimeIncrementUnit="s")
    if channel_names:
        md["Channel"] = {"Name": list(channel_names)}
    if description:
        md["Description"] = description
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.ascontiguousarray(data), ome=True, metadata=md, photometric="minisblack")
    return path


def zstack_to_ome(path: Path, stack, channel_label: str, synthetic: bool) -> Path:
    """One Z-stack as T Z C Y X. The description is JSON with every slice height in acquisition order, the direction
    and whether Stop cut the stack short, so a reader knows where each slice was taken."""
    if any(np.asarray(i.data).ndim != 2 for i in stack.images):
        raise ValueError("OME-TIFF export is for monochrome channels")
    arr = np.stack([np.asarray(i.data) for i in stack.images])[None, :, None]  # T Z C Y X
    step = abs(stack.z_um[1] - stack.z_um[0]) if len(stack.z_um) > 1 else None
    return write_ome_tiff(path, arr, pixel_um=stack.images[0].pixel_size_um, z_step_um=step,
                          channel_names=[channel_label], description=_zstack_description(stack, channel_label,
                                                                                         synthetic))


def _zstack_description(stack, channel_label: str, synthetic: bool) -> str:
    planned = getattr(stack, "planned", 0) or len(stack.images)
    return json.dumps({"source": "SIMULATED synthetic frames" if synthetic else "OpenEVOS Z-stack",
                       "channel": channel_label, "z_um": [round(float(z), 3) for z in stack.z_um],
                       "direction": "down" if getattr(stack, "direction", 1) < 0 else "up",
                       "slices": len(stack.images), "planned": planned, "complete": len(stack.images) >= planned})


def zstack_file_name(stack, prefix: str = "ZStack", when: datetime | None = None) -> str:
    """`<prefix>_<channel>_<YYYYmmdd-HHMMSS>[_stopped-k-of-n].ome.tif` (no path characters)."""
    when = when or datetime.now()
    part = "" if stack.complete else f"_stopped-{len(stack.images)}-of-{stack.planned}"
    name = f"{prefix}_{stack.channel}_{when:%Y%m%d-%H%M%S}{part}"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) + ".ome.tif"


def save_zstack(folder: Path, stack, *, prefix: str = "ZStack", name: str | None = None) -> Path:
    """Write a Z-stack into `folder` under a new name (never over an earlier file) and return its path. Monochrome
    stacks are OME-TIFF (Z, Z step, pixel size); colour stacks are a multi-page RGB TIFF (`.tif`) with the same JSON
    description (OME-TIFF here is monochrome only)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    colour = any(np.asarray(i.data).ndim == 3 for i in stack.images)
    name = name or zstack_file_name(stack, prefix)
    stem = name[:-len(".ome.tif")] if name.endswith(".ome.tif") else name.rsplit(".", 1)[0]
    ext = ".tif" if colour else ".ome.tif"
    path, k = folder / f"{stem}{ext}", 2
    while path.exists():
        path, k = folder / f"{stem}_{k}{ext}", k + 1
    synthetic = any(i.synthetic for i in stack.images)
    if not colour:
        return zstack_to_ome(path, stack, stack.channel, synthetic)
    import tifffile

    tifffile.imwrite(path, np.stack([np.asarray(i.data) for i in stack.images]), photometric="rgb",
                     description=_zstack_description(stack, stack.channel, synthetic))
    return path


def timelapse_to_ome(path: Path, result, beacon: str, interval_s: float | None) -> Path:
    frames = [(tp, imgs) for tp, lab, imgs in result.frames if lab == beacon]
    if not frames:
        raise ValueError(f"no frames for beacon {beacon}")
    chans = list(frames[0][1])
    if any(np.asarray(imgs[c].data).ndim != 2 for _tp, imgs in frames for c in chans):
        raise ValueError("OME-TIFF export is for monochrome channels; export colour frames as a movie")
    first = frames[0][1][chans[0]]
    z = _timelapse_z(result, beacon, [tp for tp, _imgs in frames], chans)
    if z is not None:  # T Z C Y X from the Z-stack taken at every time point
        arr, z_step = z
    else:
        arr = np.stack([np.stack([np.asarray(imgs[c].data) for c in chans]) for _tp, imgs in frames])[:, None]
        z_step = None
    return write_ome_tiff(path, arr, pixel_um=first.pixel_size_um, z_step_um=z_step, t_interval_s=interval_s,
                          channel_names=chans,
                          description="SIMULATED synthetic frames" if first.synthetic else "OpenEVOS time-lapse")


def _timelapse_z(result, beacon: str, tps: list[int], chans: list[str]) -> tuple[np.ndarray, float | None] | None:
    """The routine's Z-stacks for one beacon as T Z C Y X, or None when there are none or they do not form a full
    block (a stack Stop cut short, a time point without one): then the export is the plain time-lapse."""
    stacks = getattr(result, "stacks", None) or {}
    try:
        per_tp = [[stacks[(tp, beacon)][c] for c in chans] for tp in tps]
    except KeyError:
        return None
    lens = {len(s.images) for row in per_tp for s in row}
    if len(lens) != 1 or any(not s.complete for row in per_tp for s in row):
        return None
    arr = np.stack([np.stack([np.stack([np.asarray(i.data) for i in s.images]) for s in row], axis=1)
                    for row in per_tp])  # T Z C Y X
    z = per_tp[0][0].z_um
    return arr, (abs(z[1] - z[0]) if len(z) > 1 else None)
