"""Runs a ScanPlan: verified stage move per tile, optional Find Sample / Auto Fine, one capture per
channel, then a downsampled mosaic per channel for review. Pause/stop are honoured between steps
(never mid-exposure). Optional on-disk output, written per tile as the scan runs: one TIFF per tile+channel,
tiles.jsonl (append-only), then manifest.json and the mosaics at the end.

With "Z-stack" ticked, every tile also takes one Z-stack per channel centred on the focus found there (after the
in-focus capture that feeds the mosaic). Each stack is an OME-TIFF on disk as soon as it is taken; Stop ends a stack
after the slice in progress, keeps (and saves) it, then ends the scan.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from openevos.acquisition.autofocus import Autofocus
from openevos.acquisition.capture import CapturedImage, CaptureRejected, CaptureService, ZStack
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
    stacks: dict[int, dict[str, ZStack]] = field(default_factory=dict)  # tile index -> {slot: ZStack}
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

    def stacks(self, channels: list[str], role, range_um: float, step_um: float, out: dict[str, ZStack], *,
               report: Callable[[str, str], None] | None = None,
               saved: Callable[[str, ZStack], None] | None = None) -> None:
        """One Z-stack per channel centred on the focus here, put in `out` (a routine result, never the memory buffer,
        which holds the operator's own Z-stack) as each finishes, and handed to `saved` (the disk writer).
        `report(slot, "slice i/n · z = …")` follows each slice. Stop ends a stack after the slice in progress, keeps
        it, then raises RunStopped; pause waits until the stack is done (the LED is not left on over a pause)."""
        capture = self.capture
        zc = capture.ctl.focus_z()
        if zc is None:
            raise RuntimeError("Z-stack: focus position unknown")
        half = range_um / 2
        for slot in channels:
            self.control.checkpoint()

            def slice_progress(i: int, n: int, msg: str, slot: str = slot) -> None:
                if self.control.stopped:
                    capture.cancel_zstack()
                if report and i < n:
                    report(slot, msg)

            try:
                st = capture.zstack(slot, zc - half, zc + half, step_um, role, slice_progress, store=False)
            except CaptureRejected:
                if self.control.stopped:  # stopped before the first slice
                    raise RunStopped("stopped by operator") from None
                raise
            out[slot] = st
            if saved:
                saved(slot, st)
        self.control.checkpoint()  # a stack Stop cut short ends the routine here

    def run(self, plan: ScanPlan, progress: Progress | None = None) -> ScanResult:
        r = plan.routine
        res = ScanResult(plan)
        n = len(plan.tiles)
        # with an output folder, every tile goes to disk as soon as it is captured (append-only tiles.jsonl), and
        # memory keeps a 1/MEMORY_DOWNSAMPLE copy for the mosaic: a crash keeps every finished tile, and memory no
        # longer grows with full tiles (audit APP-05)
        writer = TileWriter(Path(r.output_folder), res) if r.output_folder else None
        try:
            self.capture.ctl.run(self.capture.ctl.set_objective(r.objective_slot))
            for i, t in enumerate(plan.tiles):
                if progress:
                    progress(i, n, f"tile {i + 1}/{n} ({t.region_id} r{t.row} c{t.col})")
                imgs = self.visit(t.stage_x, t.stage_y, r.channels, r.camera,
                                  find_sample=r.find_sample and i == 0, auto_fine=r.auto_fine,
                                  log=res.focus_log, index=i)
                stacks: dict[str, ZStack] = {}
                stopped: RunStopped | None = None
                if r.zstack:
                    def report(slot: str, msg: str, i: int = i, t: Tile = t) -> None:
                        if progress:
                            progress(i, n, f"tile {i + 1}/{n} ({t.region_id} r{t.row} c{t.col}) · Z-stack {slot} "
                                           f"{msg}")

                    def saved(slot: str, st: ZStack, t: Tile = t) -> None:
                        if writer is not None:
                            writer.stack(t, slot, st)

                    try:
                        self.stacks(r.channels, r.camera, r.zstack_range_um, r.zstack_step_um, stacks,
                                    report=report, saved=saved)
                    except RunStopped as exc:  # keep this tile and the stack Stop cut short, then end the scan
                        stopped = exc
                if writer is not None:
                    writer.tile(t, imgs)
                    imgs = {slot: downsampled(img, MEMORY_DOWNSAMPLE) for slot, img in imgs.items()}
                    stacks = {slot: _downsampled_stack(st, MEMORY_DOWNSAMPLE) for slot, st in stacks.items()}
                res.images.append((t, imgs))
                if stacks:
                    res.stacks[t.index] = stacks
                if stopped is not None:
                    raise stopped
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
        if writer is not None and res.images:
            res.output_folder = str(writer.finish())
        if progress:
            progress(n, n, res.message)
        return res


#: in-memory copies of tiles already written to disk keep 1 pixel in N per side (for the mosaic and the review)
MEMORY_DOWNSAMPLE = 4


def downsampled(img: CapturedImage, n: int) -> CapturedImage:
    """A smaller copy of a tile for the mosaic; its pixel size follows, and its metadata says so."""
    import dataclasses

    data = np.ascontiguousarray(img.data[::n, ::n])
    data.flags.writeable = False
    return dataclasses.replace(img, data=data, pixel_size_um=img.pixel_size_um * n if img.pixel_size_um else None,
                               extra=(*img.extra, ("in_memory_copy", f"1/{n} per side; full tile on disk")))


def _downsampled_stack(st: ZStack, n: int) -> ZStack:
    import dataclasses

    return dataclasses.replace(st, images=tuple(downsampled(i, n) for i in st.images))


class TileWriter:
    """Writes each tile's TIFFs as soon as it is captured and appends one line per tile to tiles.jsonl, so a crash
    leaves every finished tile and a valid partial manifest on disk. `finish()` writes manifest.json and mosaics."""

    def __init__(self, folder: Path, res: ScanResult):
        self.res = res
        self.out = folder / res.plan.routine.name
        self.out.mkdir(parents=True, exist_ok=True)
        self.index = self.out / "tiles.jsonl"
        self.entries: list[dict] = []
        self.synthetic = False
        self._stacks: dict[str, str] = {}  # this tile's stack files, written before the tile's index line
        (self.out / "routine.json").write_text(res.plan.routine.model_dump_json(indent=2), "utf-8")

    def stack(self, t: Tile, slot: str, st: ZStack) -> None:
        from openevos.imaging.ome import save_zstack

        stem = f"tile{t.index:04d}_{t.region_id.replace(' ', '')}_{slot}_z"
        name = stem + ("" if st.complete else f"_stopped-{len(st.images)}-of-{st.planned}") + ".ome.tif"
        self._stacks[slot] = save_zstack(self.out, st, name=name).name
        self.synthetic = self.synthetic or any(i.synthetic for i in st.images)

    def tile(self, t: Tile, imgs: dict[str, CapturedImage]) -> None:
        import tifffile

        entry: dict = {"tile": json.loads(t.model_dump_json()), "files": {}}
        if self._stacks:
            entry["stacks"], self._stacks = self._stacks, {}
        for slot, img in imgs.items():
            name = f"tile{t.index:04d}_{t.region_id.replace(' ', '')}_{slot}.tif"
            tifffile.imwrite(self.out / name, np.asarray(img.data), metadata=None,
                             description=json.dumps(img.metadata()))
            entry["files"][slot] = name
            self.synthetic = self.synthetic or img.synthetic
        with open(self.index, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self.entries.append(entry)

    def finish(self) -> Path:
        import tifffile

        res = self.res
        for slot, m in res.mosaics.items():
            tifffile.imwrite(self.out / f"mosaic_{slot}.tif", m)
        manifest = {"routine": json.loads(res.plan.routine.model_dump_json()), "completed": res.completed,
                    "message": res.message, "synthetic": self.synthetic, "warnings": list(res.plan.warnings),
                    "tiles": self.entries}
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), "utf-8")
        return self.out
