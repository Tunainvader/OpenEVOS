"""OME-TIFF export (open format for Fiji/QuPath/napari): physical pixel size, channel names, Z step, time.

Arrays are written as TZCYX (singleton axes kept so readers know what each axis is).
"""

from __future__ import annotations

from collections.abc import Sequence
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
    arr = np.stack([np.asarray(i.data) for i in stack.images])[None, :, None]  # T Z C Y X
    step = abs(stack.z_um[1] - stack.z_um[0]) if len(stack.z_um) > 1 else None
    return write_ome_tiff(path, arr, pixel_um=stack.images[0].pixel_size_um, z_step_um=step,
                          channel_names=[channel_label],
                          description="SIMULATED synthetic frames" if synthetic else "OpenEVOS Z-stack")


def timelapse_to_ome(path: Path, result, beacon: str, interval_s: float | None) -> Path:
    frames = [(tp, imgs) for tp, lab, imgs in result.frames if lab == beacon]
    if not frames:
        raise ValueError(f"no frames for beacon {beacon}")
    chans = list(frames[0][1])
    if any(np.asarray(imgs[c].data).ndim != 2 for _tp, imgs in frames for c in chans):
        raise ValueError("OME-TIFF export is for monochrome channels; export colour frames as a movie")
    arr = np.stack([np.stack([np.asarray(imgs[c].data) for c in chans]) for _tp, imgs in frames])[:, None]  # T Z C Y X
    first = frames[0][1][chans[0]]
    return write_ome_tiff(path, arr, pixel_um=first.pixel_size_um, t_interval_s=interval_s, channel_names=chans,
                          description="SIMULATED synthetic frames" if first.synthetic else "OpenEVOS time-lapse")
