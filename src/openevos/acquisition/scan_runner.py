"""Runs a ScanPlan: verified stage move per tile, optional Find Sample / Auto Fine, one capture per
channel, then a downsampled mosaic per channel for review. Pause/stop are honoured between steps
(never mid-exposure). Optional on-disk output: one TIFF per tile+channel and a manifest.json.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from openevos.acquisition.autofocus import Autofocus
from openevos.acquisition.capture import CapturedImage, CaptureService
from openevos.acquisition.scan_plan import ScanPlan, Tile
from openevos.instrument.vessels import Vessel

Progress = Callable[[int, int, str], None]


class RunStopped(RuntimeError):
    pass


class RunControl:
    def __init__(self) -> None:
        self._pause = threading.Event()
        self._stop = threading.Event()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            self._stop.wait(0.05)
        if self._stop.is_set():
            raise RunStopped("stopped by operator")

    def sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise RunStopped("stopped by operator")


@dataclass
class ScanResult:
    plan: ScanPlan
    images: list[tuple[Tile, dict[str, CapturedImage]]] = field(default_factory=list)
    mosaics: dict[str, np.ndarray] = field(default_factory=dict)
    mosaic_scale_um_per_px: float | None = None
    focus_log: list[tuple[int, str]] = field(default_factory=list)
    completed: bool = False
    message: str = ""
    output_folder: str | None = None


def build_mosaic(images: list[tuple[Tile, dict[str, CapturedImage]]], slot: str,
                 max_px: int = 2400) -> tuple[np.ndarray | None, float | None]:
    items = [(t, d[slot]) for t, d in images if slot in d and d[slot].pixel_size_um]
    if not items:
        return None, None
    px = items[0][1].pixel_size_um
    h, w = items[0][1].data.shape[:2]
    xs = [t.stage_x for t, _ in items]
    ys = [t.stage_y for t, _ in items]
    span_x = (max(xs) - min(xs)) / px + w
    span_y = (max(ys) - min(ys)) / px + h
    ds = max(1, int(np.ceil(max(span_x, span_y) / max_px)))
    ow, oh = int(span_x / ds) + 2, int(span_y / ds) + 2
    color = items[0][1].data.ndim == 3
    acc = np.zeros((oh, ow, 3) if color else (oh, ow), np.float32)
    wt = np.zeros((oh, ow), np.float32)
    for t, img in items:
        small = img.data[::ds, ::ds].astype(np.float32)
        ox = int(((t.stage_x - min(xs)) / px) / ds)
        oy = int(((t.stage_y - min(ys)) / px) / ds)
        sh, sw = small.shape[:2]
        acc[oy:oy + sh, ox:ox + sw] += small
        wt[oy:oy + sh, ox:ox + sw] += 1
    nz = wt > 0
    if color:
        acc[nz] /= wt[nz][:, None]
        out = acc.astype(np.uint8)
    else:
        acc[nz] /= wt[nz]
        out = acc.astype(np.uint16)
    return out, px * ds


class ScanRunner:
    def __init__(self, capture: CaptureService, autofocus: Autofocus, vessel: Vessel, *,
                 auto_fine_range_percent: float = 100.0):
        self.capture, self.af, self.vessel = capture, autofocus, vessel
        self.auto_fine_range_percent = auto_fine_range_percent
        self.control = RunControl()

    def visit(self, x: float, y: float, channels: list[str], role, *, find_sample: bool, auto_fine: bool,
              log: list[tuple[int, str]], index: int, z: float | None = None) -> dict[str, CapturedImage]:
        ctl = self.capture.ctl
        self.control.checkpoint()
        ctl.run(ctl.move_stage(x, y))
        if z is not None:
            ctl.run(ctl.move_focus(z))
        if find_sample:
            r = self.af.find_sample(role, self.vessel.focus_nominal_um, self.vessel.focus_range_um)
            log.append((index, f"find sample: {r.message}"))
        if auto_fine:
            self.control.checkpoint()
            r = self.af.auto_fine(role, self.auto_fine_range_percent)
            log.append((index, f"auto fine: {r.message}"))
        out = {}
        for slot in channels:
            self.control.checkpoint()
            out[slot] = self.capture.capture(slot, role, store=False)
        return out

    def run(self, plan: ScanPlan, progress: Progress | None = None) -> ScanResult:
        r = plan.routine
        res = ScanResult(plan)
        n = len(plan.tiles)
        try:
            self.capture.ctl.run(self.capture.ctl.set_objective(r.objective_slot))
            for i, t in enumerate(plan.tiles):
                if progress:
                    progress(i, n, f"tile {i + 1}/{n} ({t.region_id} r{t.row} c{t.col})")
                imgs = self.visit(t.stage_x, t.stage_y, r.channels, r.camera,
                                  find_sample=r.find_sample and i == 0, auto_fine=r.auto_fine,
                                  log=res.focus_log, index=i)
                res.images.append((t, imgs))
            res.completed = True
            res.message = f"{n} tiles captured"
        except RunStopped as exc:
            res.message = f"{exc} after {len(res.images)}/{n} tiles"
        except Exception as exc:  # noqa: BLE001 — reported to the operator, never swallowed silently
            res.message = f"failed after {len(res.images)}/{n} tiles: {exc}"
        for slot in r.channels:
            m, scale = build_mosaic(res.images, slot)
            if m is not None:
                res.mosaics[slot] = m
                res.mosaic_scale_um_per_px = scale
        if r.output_folder and res.images:
            res.output_folder = str(write_scan(Path(r.output_folder), res))
        if progress:
            progress(n, n, res.message)
        return res


def write_scan(folder: Path, res: ScanResult) -> Path:
    import tifffile

    r = res.plan.routine
    out = folder / f"{r.name}"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"routine": json.loads(r.model_dump_json()), "completed": res.completed, "message": res.message,
                "synthetic": any(i.synthetic for _, d in res.images for i in d.values()),
                "warnings": list(res.plan.warnings), "tiles": []}
    for t, d in res.images:
        entry = {"tile": json.loads(t.model_dump_json()), "files": {}}
        for slot, img in d.items():
            name = f"tile{t.index:04d}_{t.region_id.replace(' ', '')}_{slot}.tif"
            tifffile.imwrite(out / name, np.asarray(img.data), metadata=None,
                             description=json.dumps(img.metadata()))
            entry["files"][slot] = name
        manifest["tiles"].append(entry)
    for slot, m in res.mosaics.items():
        tifffile.imwrite(out / f"mosaic_{slot}.tif", m)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), "utf-8")
    return out
