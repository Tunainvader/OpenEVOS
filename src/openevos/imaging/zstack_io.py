"""Save a Z-stack run (ADR-0064): one folder per run, readable without OpenEVOS.

    <root>/<name>_<YYYYmmdd-HHMMSS>[_2]/
        stack.ome.tif            monochrome: Z x C x Y x X, every channel, pixel size, Z step and each plane's Z
        <slot>.ome.tif           colour, or channels of different lengths (a stopped run): one file per channel
        <slot>_<method>.tif      projections asked for (max, edf, ...), raw values, same dtype as the slices
        zstack.json              run id, software version, plan and its hash, status, planned and observed Z, every
                                 slice's capture metadata, each channel's focus curve, file list

* The folder is claimed with mkdir (never shared between two saves, unlike checking for a free name first).
* Every file is written to a temporary name in the folder and renamed into place, so a crash never leaves a
  truncated file under its real name.
* SIMULATED runs say so in the folder's JSON, the OME description and each projection's description.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np

FORMAT = "openevos-zstack/1"


def _claim_folder(root: Path, name: str, when: datetime) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = f"{name}_{when.astimezone().strftime('%Y%m%d-%H%M%S')}"
    for i in range(1, 1000):
        p = root / (base if i == 1 else f"{base}_{i}")
        try:
            p.mkdir()
            return p
        except FileExistsError:
            continue
    raise OSError(f"no free folder name for {base} in {root}")


def _atomic(path: Path, write) -> Path:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _ome(path: Path, arr: np.ndarray, axes: str, *, pixel_um, step_um, z_um, channels, description, rgb=False,
         exposure_ms=None, delta_s=None):
    """`z_um`, `exposure_ms`, `delta_s`: one entry per plane in Z-major order (Z, then channel), as the planes are
    written. Exposure and the time since the run started go into each OME Plane (ExposureTime, DeltaT)."""
    import tifffile

    md: dict = {"axes": axes, "Description": description}
    if pixel_um:
        md.update(PhysicalSizeX=float(pixel_um), PhysicalSizeXUnit="µm", PhysicalSizeY=float(pixel_um),
                  PhysicalSizeYUnit="µm")
    if step_um:
        md.update(PhysicalSizeZ=float(step_um), PhysicalSizeZUnit="µm")
    if channels and not rgb:
        md["Channel"] = {"Name": list(channels)}
    n_c = 1 if rgb else len(channels)
    n_planes = len(z_um) * n_c
    md["Plane"] = {"PositionZ": [float(z) for z in z_um for _c in range(n_c)], "PositionZUnit": ["µm"] * n_planes}
    if exposure_ms is not None and len(exposure_ms) == n_planes:
        md["Plane"].update(ExposureTime=[float(e) for e in exposure_ms], ExposureTimeUnit=["ms"] * n_planes)
    if delta_s is not None and len(delta_s) == n_planes:
        md["Plane"].update(DeltaT=[float(t) for t in delta_s], DeltaTUnit=["s"] * n_planes)
    _atomic(path, lambda t: tifffile.imwrite(t, np.ascontiguousarray(arr), ome=True, metadata=md,
                                             photometric="rgb" if rgb else "minisblack"))


def save_zstack_set(root: Path, zset, *, name: str = "zstack", projections: tuple[str, ...] = ("max",)) -> Path:
    """Write one run; returns its folder. Partial runs are saved as they are and marked in zstack.json."""
    if not zset.stacks:
        raise ValueError("nothing to save: the run kept no slices")
    out = _claim_folder(Path(root), name, zset.started_at)
    write_zstack_folder(out, zset, projections=projections)
    return out


def write_zstack_folder(out: Path, zset, *, projections: tuple[str, ...] = ("max",), extra: dict | None = None) -> Path:
    """Write one run into an existing, empty folder `out` (Multi-D writes each volume into a temporary folder and
    renames it when complete, ADR-0065). `extra` is merged into zstack.json."""
    import hashlib
    import uuid

    import tifffile

    from openevos import __version__
    from openevos.imaging.zproject import METHODS, focus_curve, project
    from openevos.imaging.zviews import sharp_range, z_at

    def dt(img) -> float:
        return round((img.captured_at - zset.started_at).total_seconds(), 3)

    if not zset.stacks:
        raise ValueError("nothing to save: the run kept no slices")
    synthetic = zset.synthetic
    tag = "SIMULATED synthetic frames · " if synthetic else ""
    files: list[str] = []
    first = zset.stacks[0].images[0]
    step = zset.plan.step_um
    mono = all(s.images[0].data.ndim == 2 for s in zset.stacks)
    same = len({(len(s.images), s.images[0].data.shape) for s in zset.stacks}) == 1
    if mono and same:
        arr = np.stack([np.stack([np.asarray(i.data) for i in s.images]) for s in zset.stacks], axis=1)  # Z C Y X
        z_obs = [i.z_um if i.z_um is not None else z for i, z in zip(zset.stacks[0].images, zset.stacks[0].z_um,
                                                                        strict=True)]
        planes = [s.images[k] for k in range(len(z_obs)) for s in zset.stacks]  # Z-major, as written
        _ome(out / "stack.ome.tif", arr, "ZCYX", pixel_um=first.pixel_size_um, step_um=step, z_um=z_obs,
             channels=[s.images[0].channel_label for s in zset.stacks],
             exposure_ms=[i.exposure_ms for i in planes], delta_s=[dt(i) for i in planes],
             description=f"{tag}OpenEVOS Z-stack ({zset.status})")
        files.append("stack.ome.tif")
    else:
        for s in zset.stacks:
            rgb = s.images[0].data.ndim == 3
            arr = np.stack([np.asarray(i.data) for i in s.images])
            z_obs = [i.z_um if i.z_um is not None else z for i, z in zip(s.images, s.z_um, strict=True)]
            fname = f"{s.channel}.ome.tif"
            _ome(out / fname, arr, "ZYXS" if rgb else "ZYX", pixel_um=s.images[0].pixel_size_um, step_um=step,
                 z_um=z_obs, channels=[s.images[0].channel_label], rgb=rgb,
                 exposure_ms=[i.exposure_ms for i in s.images], delta_s=[dt(i) for i in s.images],
                 description=f"{tag}OpenEVOS Z-stack {s.images[0].channel_label} ({zset.status})")
            files.append(fname)
    for s in zset.stacks:
        for m in projections:
            if m not in METHODS:
                continue
            img = project([i.data for i in s.images], m)
            fname = f"{s.channel}_{m}.tif"
            desc = json.dumps({"projection": m, "method": METHODS[m], "channel": s.channel, "slices": len(s.images),
                               "z_range_um": [s.z_um[0], s.z_um[len(s.images) - 1]], "synthetic": synthetic})
            _atomic(out / fname, lambda t, a=img, d=desc: tifffile.imwrite(
                t, a, photometric="rgb" if a.ndim == 3 else "minisblack", description=d, metadata=None))
            files.append(fname)
    focus = {}
    for s in zset.stacks:
        if len(s.images) < 2:
            continue
        curve = focus_curve([i.data for i in s.images])
        b = int(np.argmax(curve))
        sr = sharp_range(curve)
        zz = list(s.z_um[:len(s.images)])
        focus[s.channel] = {"metric": "variance of the Laplacian (relative; not calibrated)",
                            "values": [round(float(v), 4) for v in curve], "sharpest_slice": b + 1,
                            "sharpest_z_um": zz[b],
                            "sharp_range_um": [round(z_at(sr[0], zz), 3), round(z_at(sr[1], zz), 3)] if sr else None}
    plan = zset.plan.describe()
    record = {
        "format": FORMAT, "run_id": str(uuid.uuid4()), "software": {"name": "OpenEVOS", "version": __version__},
        "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True, default=str).encode()).hexdigest(),
        "status": zset.status, "message": zset.message, "synthetic": synthetic,
        "provenance": "SIMULATED synthetic frames (openevos optics simulator)" if synthetic else "camera frames",
        "started_at": zset.started_at.isoformat(), "seconds": round(zset.seconds, 2),
        "plan": plan, "planned_z_um": list(zset.planned_z_um), "focus": focus,
        "channels": [{"slot": s.channel, "label": s.images[0].channel_label, "complete": s.complete,
                      "z_planned_um": list(s.z_um), "slices": [i.metadata() for i in s.images]} for s in zset.stacks],
        "files": files,
    }
    if extra:
        record.update(extra)
    _atomic(out / "zstack.json", lambda t: t.write_text(json.dumps(record, indent=1, default=str), "utf-8"))
    return out


def load_zstack_set(folder: Path):
    """Read back a folder written by `save_zstack_set` as a ZStackSet (every slice with its saved metadata), so a
    time-lapse that kept only its stack folders can still open a stack on the Z-stack page."""
    import tifffile

    from openevos.acquisition.capture import CapturedImage, ZStack
    from openevos.acquisition.zstack import ChannelOrder, ZStackPlan, ZStackSet
    from openevos.instrument.profile import CameraRole

    folder = Path(folder)
    rec = json.loads((folder / "zstack.json").read_text("utf-8"))
    if rec.get("format") != FORMAT:
        raise ValueError(f"{folder} is not an OpenEVOS Z-stack folder ({rec.get('format')!r})")
    chans = rec["channels"]
    if "stack.ome.tif" in rec["files"]:
        arr = tifffile.imread(folder / "stack.ome.tif")  # Z C Y X (C dropped by tifffile when there is one)
        if arr.ndim == 3:
            arr = arr[:, None]
        data = {c["slot"]: [arr[k, j] for k in range(arr.shape[0])] for j, c in enumerate(chans)}
    else:
        data = {c["slot"]: list(tifffile.imread(folder / f"{c['slot']}.ome.tif")) for c in chans}

    def image(px: np.ndarray, m: dict) -> CapturedImage:
        px = np.ascontiguousarray(px)
        px.setflags(write=False)
        return CapturedImage(
            data=px, channel=m["channel"], channel_label=m["channel_label"], camera=CameraRole(m["camera"]),
            objective_slot=m["objective_slot"], magnification=m["magnification"], pixel_size_um=m["pixel_size_um"],
            stage_xy_um=tuple(m["stage_xy_um"]) if m["stage_xy_um"] is not None else None, z_um=m["z_um"],
            exposure_ms=m["exposure_ms"], gain=m["gain"], led_percent=m["led_percent"], synthetic=m["synthetic"],
            frame_device=m["frame_device"], frame_index=m["frame_index"], bit_depth=m["bit_depth"],
            captured_at=datetime.fromisoformat(m["captured_at"]),
            extra=tuple((str(k), str(v)) for k, v in (m.get("extra") or {}).items()))

    planned = tuple(rec["planned_z_um"])
    stacks = tuple(ZStack(c["slot"], tuple(c["z_planned_um"]),
                          tuple(image(px, m) for px, m in zip(data[c["slot"]], c["slices"], strict=True)),
                          complete=c["complete"], planned=len(planned), stop_um=planned[-1] if planned else None)
                   for c in chans)
    p = rec["plan"]
    plan = ZStackPlan(channels=tuple(p["channels"]), role=CameraRole(p["camera"]), bottom_um=p["bottom_um"],
                      top_um=p["top_um"], step_um=p["step_um"], centre_um=p["centre_um"],
                      order=ChannelOrder(p["order"]), light_between_slices=p["light_between_slices"],
                      settle_ms=p["settle_ms"], approach_um=p["approach_um"], return_to_start=p["return_to_start"],
                      confirmed_above_safe=p["confirmed_above_safe"], store=False)
    return ZStackSet(stacks, planned, rec["status"], rec["message"], plan,
                     started_at=datetime.fromisoformat(rec["started_at"]), seconds=rec["seconds"])
