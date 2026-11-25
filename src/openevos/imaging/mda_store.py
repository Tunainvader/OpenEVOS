"""Multi-D experiment folder (ADR-0065; kit doc 05): written as the run goes, readable without OpenEVOS.

    Experiment_<YYYYmmdd-HHMMSS>_<plan id>/
        experiment.json     identity: software, instrument, simulated or not, start, final status
        plan.json           the frozen plan, its schema version and SHA-256
        manifest.json       every planned volume: completed, partial or not acquired; counts; stop reason
        events.jsonl        append-only journal: one line per image (event id, indices, planned and observed Z,
                            channel settings, timestamps, CRC-32 of the pixels), plus timepoint and state records
        Positions/P0001_<label>/T000001/
            stack.ome.tif   Z x C x Y x X (or one OME-TIFF per channel), as the Z-stack writer makes it
            <slot>_<method>.tif  projections asked for (derived; raw planes are never changed)
            zstack.json     the volume's own record, with its T / P indices and event ids

P (positions) is a folder level with the stage coordinates in metadata, never an image axis; T is a folder level too,
so an irregular or partial schedule stays honest. Each volume is written into a hidden temporary folder and renamed
when complete, so an interrupted write is never mistaken for a finished one. Before every volume the free space is
checked against the volume plus a reserve: a full disk stops the run safely (``StorageFull``) with the volumes
already written intact and the manifest saying what is missing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import numpy as np

from openevos import __version__
from openevos.imaging.zstack_io import _atomic, write_zstack_folder

FORMAT = "openevos-mda/1"


class StorageFull(OSError):
    pass


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")[:40] or "position"


def _write_json(path: Path, data: dict) -> None:
    _atomic(path, lambda t: t.write_text(json.dumps(data, indent=1, default=str), "utf-8"))


def volume_dir(position_id: str, label: str, t_index: int) -> str:
    return f"Positions/{position_id}_{_safe(label)}/T{t_index + 1:06d}"


class ExperimentWriter:
    """One authoritative writer per run. Not thread-safe: the runner calls it from its own thread only."""

    def __init__(self, root: Path, *, reserve_mb: float = 512.0, disk_usage=shutil.disk_usage):
        self.root = Path(root)
        self.reserve_mb = reserve_mb
        self._disk_usage = disk_usage
        self.folder: Path | None = None
        self.manifest: dict = {}
        self._journal: TextIO | None = None

    # ------------------------------------------------------------------ run lifecycle
    def begin(self, plan, summary, *, started_at: datetime | None = None) -> Path:
        started = started_at or datetime.now(UTC)
        self.root.mkdir(parents=True, exist_ok=True)
        base = f"Experiment_{started.astimezone().strftime('%Y%m%d-%H%M%S')}_{plan.plan_id}"
        for i in range(1, 1000):
            cand = self.root / (base if i == 1 else f"{base}_{i}")
            try:
                cand.mkdir()
            except FileExistsError:
                continue
            self.folder = cand
            break
        else:
            raise OSError(f"no free folder name for {base} in {self.root}")
        assert self.folder is not None
        _write_json(self.folder / "plan.json", {"format": FORMAT, "plan_sha256": plan.content_hash(),
                                                "plan": plan.to_dict()})
        _write_json(self.folder / "experiment.json", {
            "format": FORMAT, "name": plan.name, "plan_id": plan.plan_id, "started_at": started.isoformat(),
            "software": {"name": "OpenEVOS", "version": __version__}, "instrument": plan.instrument,
            "simulated": plan.simulated,
            "provenance": "SIMULATED synthetic frames (openevos optics simulator)" if plan.simulated else
            "camera frames", "dimensions": {"T": summary.n_t, "P": summary.n_p, "C": summary.n_c, "Z": summary.n_z},
            "images_planned": summary.images, "status": "running"})
        vols = []
        for t in range(plan.n_t):
            for p, pos in enumerate(plan.positions):
                vols.append({"key": f"T{t + 1:06d}P{p + 1:04d}", "t_index": t, "p_index": p,
                             "position_id": pos.position_id, "label": pos.label, "x": pos.x, "y": pos.y,
                             "unit": pos.unit, "frame": pos.frame, "path": volume_dir(pos.position_id, pos.label, t),
                             "status": "not acquired", "images": 0})
        self.manifest = {"format": FORMAT, "status": "running", "images_planned": summary.images,
                         "images_written": 0, "volumes": vols, "timepoints": [], "message": "",
                         "last_event_id": None}
        self._save_manifest()
        self._journal = open(self.folder / "events.jsonl", "a", encoding="utf-8")  # noqa: SIM115 - kept for the run
        self.record("run", state="running", plan_id=plan.plan_id)
        return self.folder

    def record(self, kind: str, **data) -> None:
        """One journal line, flushed and synced (the journal is what a stopped or crashed run is rebuilt from)."""
        if self._journal is None:
            return
        line = {"kind": kind, "at": datetime.now(UTC).isoformat(), **data}
        self._journal.write(json.dumps(line, default=str) + "\n")
        self._journal.flush()
        try:
            os.fsync(self._journal.fileno())
        except OSError:
            pass

    def timepoint(self, rec: dict) -> None:
        self.manifest["timepoints"].append(rec)
        self.record("timepoint", **rec)
        self._save_manifest()

    def check_space(self, need_mb: float) -> None:
        assert self.folder is not None
        try:
            free = self._disk_usage(self.folder).free / 1e6
        except OSError as exc:
            raise StorageFull(f"cannot read free space: {exc}") from exc
        if free < need_mb + self.reserve_mb:
            raise StorageFull(f"disk nearly full: {free:,.0f} MB free, the next volume needs ≈ {need_mb:,.0f} MB "
                              f"plus a {self.reserve_mb:,.0f} MB reserve")

    def commit_volume(self, plan, job, zset, *, need_mb: float = 0.0) -> Path:
        """Write one volume (complete or partial) and journal every image in it. Raises StorageFull before writing
        when the disk is too full; raises OSError when writing fails (the temporary folder is removed)."""
        from openevos.acquisition.mda import event_id

        assert self.folder is not None
        self.check_space(need_mb)
        rel = volume_dir(job.position.position_id, job.position.label, job.t_index)
        final = self.folder / rel
        tmp = final.with_name(f".{final.name}.tmp")
        final.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        slots = [c.slot for c in plan.channels]
        events = []
        for st in zset.stacks:
            c = slots.index(st.channel) if st.channel in slots else -1
            for z, img in enumerate(st.images):
                arr = np.ascontiguousarray(img.data)
                events.append({
                    "event_id": event_id(job.t_index, job.p_index, c, z), "t_index": job.t_index,
                    "p_index": job.p_index, "c_index": c, "z_index": z, "position_id": job.position.position_id,
                    "x_planned": job.position.x, "y_planned": job.position.y,
                    "x_observed": img.stage_xy_um[0] if img.stage_xy_um else None,
                    "y_observed": img.stage_xy_um[1] if img.stage_xy_um else None,
                    "channel": st.channel, "channel_label": img.channel_label, "z_planned_um": st.z_um[z],
                    "z_observed_um": img.z_um, "anchor_um": job.anchor_um, "exposure_ms": img.exposure_ms,
                    "gain": img.gain, "led_percent": img.led_percent, "camera": img.camera.value,
                    "shape": list(arr.shape), "dtype": str(arr.dtype), "bit_depth": img.bit_depth,
                    "pixel_size_um": img.pixel_size_um, "captured_at": img.captured_at.isoformat(),
                    "crc32": f"{zlib.crc32(arr.tobytes()) & 0xFFFFFFFF:08x}", "synthetic": img.synthetic,
                    "file": f"{rel}/stack.ome.tif"})
        extra = {"mda": {"plan_id": plan.plan_id, "t_index": job.t_index, "p_index": job.p_index,
                         "position": {"id": job.position.position_id, "label": job.position.label,
                                      "x": job.position.x, "y": job.position.y, "unit": job.position.unit,
                                      "frame": job.position.frame},
                         "anchor_um": job.anchor_um, "event_ids": [e["event_id"] for e in events]}}
        try:
            write_zstack_folder(tmp, zset, projections=tuple(plan.projections), extra=extra)
            if final.exists():
                shutil.rmtree(final)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        files = set(os.listdir(final))
        for e in events:  # per-channel files when the volume could not be one ZCYX file
            if "stack.ome.tif" not in files:
                e["file"] = f"{rel}/{e['channel']}.ome.tif"
            self.record("image", **e)
        v = next(v for v in self.manifest["volumes"] if v["t_index"] == job.t_index and v["p_index"] == job.p_index)
        v["status"] = "completed" if zset.complete else "partial"
        v["images"] = len(events)
        v["started_at"] = zset.started_at.isoformat()
        v["seconds"] = round(zset.seconds, 2)
        self.manifest["images_written"] += len(events)
        if events:
            self.manifest["last_event_id"] = events[-1]["event_id"]
        self._save_manifest()
        return final

    def finish(self, status: str, message: str = "") -> None:
        """Final manifest: status ("completed" | "partial" | "failed"), reason, and every volume not written."""
        if self.folder is None:
            return
        self.manifest["status"] = status
        self.manifest["message"] = message
        self.manifest["finished_at"] = datetime.now(UTC).isoformat()
        self.manifest["volumes_missing"] = [v["key"] for v in self.manifest["volumes"] if v["status"] != "completed"]
        self._save_manifest()
        exp = json.loads((self.folder / "experiment.json").read_text("utf-8"))
        exp.update(status=status, message=message, finished_at=self.manifest["finished_at"],
                   images_written=self.manifest["images_written"])
        _write_json(self.folder / "experiment.json", exp)
        self.record("run", state=status, message=message)
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    def _save_manifest(self) -> None:
        assert self.folder is not None
        _write_json(self.folder / "manifest.json", self.manifest)


# ====================================================================================== reading back
class ExperimentReader:
    """Opens an experiment folder for review. Volumes load lazily, one at a time."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.manifest = json.loads((self.folder / "manifest.json").read_text("utf-8"))
        self.plan = json.loads((self.folder / "plan.json").read_text("utf-8"))["plan"]
        self.experiment = json.loads((self.folder / "experiment.json").read_text("utf-8"))
        self._cache: dict[tuple[int, int], dict] = {}

    @property
    def n_t(self) -> int:
        return int(self.experiment["dimensions"]["T"])

    @property
    def n_p(self) -> int:
        return int(self.experiment["dimensions"]["P"])

    def channels(self) -> list[tuple[str, str]]:
        return [(c["slot"], c["label"]) for c in self.plan["channels"]]

    def volume_info(self, t: int, p: int) -> dict:
        return next(v for v in self.manifest["volumes"] if v["t_index"] == t and v["p_index"] == p)

    def load(self, t: int, p: int) -> dict | None:
        """{"record": zstack.json, "stacks": {slot: (Z x Y x X[ x 3] array, [observed Z])}} or None if not acquired."""
        import tifffile

        if (t, p) in self._cache:
            return self._cache[(t, p)]
        v = self.volume_info(t, p)
        d = self.folder / v["path"]
        if v["status"] == "not acquired" or not d.is_dir():
            return None
        rec = json.loads((d / "zstack.json").read_text("utf-8"))
        stacks: dict[str, tuple[np.ndarray, list]] = {}
        chans = rec["channels"]
        if (d / "stack.ome.tif").exists():
            arr = tifffile.imread(d / "stack.ome.tif")  # Z C Y X (Z Y X when one channel was squeezed)
            if arr.ndim == 3:
                arr = arr[:, None]
            for ci, ch in enumerate(chans):
                stacks[ch["slot"]] = (arr[:, ci], [s["z_um"] for s in ch["slices"]])
        else:
            for ch in chans:
                f = d / f"{ch['slot']}.ome.tif"
                if f.exists():
                    stacks[ch["slot"]] = (tifffile.imread(f), [s["z_um"] for s in ch["slices"]])
        out = {"record": rec, "stacks": stacks}
        if len(self._cache) > 6:  # bounded: a long experiment is reviewed one volume at a time
            self._cache.pop(next(iter(self._cache)))
        self._cache[(t, p)] = out
        return out

    def events(self) -> list[dict]:
        out = []
        with open(self.folder / "events.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
