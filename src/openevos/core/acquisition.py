"""Minimal multi-dimensional acquisition: channels × positions × Z × time-lapse.

Loop order is T → P → C → Z (time outermost, Z innermost), which keeps carriage and LED
changes to one per channel per position. Every device action goes through the gateway as actor
ACQUISITION under an exclusive reservation, so operator jogs are rejected while a plan runs and
a STOP aborts it at the next step. LEDs are on only while a channel's Z series is acquired.

Frames are kept in memory (most recent `keep_last`) and, if `save_dir` is given, written as
`.npy` plus one JSON line of metadata per frame. OME-TIFF export is a later phase (plan §15).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openevos.core.channels import ChannelSettings
from openevos.core.command_language import Actor, CommandAborted, CommandFailed, CommandRejected
from openevos.hal.types import Frame, Role

if TYPE_CHECKING:
    from openevos.core.controller import MicroscopeController

MAX_IMAGES = 20000
log = logging.getLogger(__name__)


@dataclass
class ChannelStep:
    preset: str
    exposure_ms: float
    intensity_pct: float
    gain_db: float = 0.0


@dataclass
class StagePosition:
    label: str
    x_um: float
    y_um: float
    z_um: float | None = None  # None: use the focus position at the start of the run


@dataclass
class ZStack:
    range_um: float  # total range, centred on the position's focus
    step_um: float

    def offsets(self) -> list[float]:
        if self.step_um <= 0 or self.range_um <= 0:
            return [0.0]
        n = round(self.range_um / self.step_um) + 1
        start = -self.step_um * (n - 1) / 2.0
        return [round(start + i * self.step_um, 6) for i in range(n)]


@dataclass
class TimeLapse:
    count: int
    interval_s: float


@dataclass
class AcquisitionPlan:
    channels: list[ChannelStep]
    positions: list[StagePosition] = field(default_factory=list)  # empty: current position
    z_stack: ZStack | None = None
    time_lapse: TimeLapse | None = None
    name: str = "acquisition"

    def z_offsets(self) -> list[float]:
        return self.z_stack.offsets() if self.z_stack else [0.0]

    def timepoints(self) -> int:
        return self.time_lapse.count if self.time_lapse else 1

    def image_count(self) -> int:
        return len(self.channels) * max(1, len(self.positions)) * len(self.z_offsets()) * self.timepoints()

    def validate(self, preset_names: set[str] | None = None) -> list[str]:
        errs = []
        if not self.channels:
            errs.append("select at least one channel")
        for c in self.channels:
            if preset_names is not None and c.preset not in preset_names:
                errs.append(f"unknown channel '{c.preset}'")
            if c.exposure_ms <= 0:
                errs.append(f"{c.preset}: exposure must be > 0 ms")
            if not 0 <= c.intensity_pct <= 100:
                errs.append(f"{c.preset}: intensity must be 0–100 %")
        if self.z_stack and (self.z_stack.step_um <= 0 or self.z_stack.range_um < 0):
            errs.append("Z step must be > 0 and range ≥ 0")
        if self.time_lapse and (self.time_lapse.count < 1 or self.time_lapse.interval_s < 0):
            errs.append("time-lapse needs ≥ 1 time point and an interval ≥ 0 s")
        if self.image_count() > MAX_IMAGES:
            errs.append(f"plan has {self.image_count()} images; the limit is {MAX_IMAGES}")
        return errs

    def estimated_duration_s(self) -> float:
        per_t = sum(c.exposure_ms / 1000.0 + 0.05 for c in self.channels) * len(self.z_offsets()) \
            * max(1, len(self.positions)) + 0.5 * max(1, len(self.positions))
        if self.time_lapse and self.time_lapse.count > 1:
            return max(per_t, self.time_lapse.interval_s) * (self.time_lapse.count - 1) + per_t
        return per_t

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AcquiredFrame:
    t: int
    p: int
    c: int
    z: int
    channel: str
    position: str
    z_um: float
    frame: Frame


@dataclass(frozen=True)
class Progress:
    done: int
    total: int
    t: int
    p: int
    c: int
    z: int
    message: str


@dataclass
class AcquisitionResult:
    run_id: str
    plan: AcquisitionPlan
    status: str = "running"  # running | completed | cancelled | failed
    error: str = ""
    images: int = 0
    started: float = 0.0
    finished: float = 0.0
    frames: deque = field(default_factory=lambda: deque(maxlen=50))
    save_dir: Path | None = None


class AcquisitionEngine:
    def __init__(self, controller: MicroscopeController):
        self.ctl = controller
        self.gw = controller.gw
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, plan: AcquisitionPlan, *, on_progress: Callable[[Progress], None] | None = None,
            on_frame: Callable[[AcquiredFrame], None] | None = None,
            save_dir: Path | None = None, keep_last: int = 50) -> AcquisitionResult:
        """Run the plan on the calling thread (the controller runs it on its worker)."""
        errs = plan.validate({p.name for p in self.ctl.presets})
        if errs:
            raise ValueError("; ".join(errs))
        run_id = f"acq-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        owner = f"acquisition:{run_id}"
        result = AcquisitionResult(run_id=run_id, plan=plan, started=time.time(),
                                   frames=deque(maxlen=max(1, keep_last)))
        if save_dir is not None:
            result.save_dir = Path(save_dir) / run_id
            result.save_dir.mkdir(parents=True, exist_ok=False)
            (result.save_dir / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2), "utf-8")
        self._cancel.clear()

        def on_stop(_reason: str) -> None:
            self._cancel.set()

        self.gw.add_stop_listener(on_stop)
        self.gw.note("acquisition.start", actor=Actor.ACQUISITION,
                     details={"run_id": run_id, "images": plan.image_count()})
        kw = {"actor": Actor.ACQUISITION, "owner": owner}
        try:
            with self.gw.exclusive(owner):
                self._run(plan, result, kw, on_progress, on_frame)
            result.status = "cancelled" if self._cancel.is_set() else "completed"
        except (CommandAborted, _Cancelled):
            result.status = "cancelled"
        except (CommandRejected, CommandFailed) as exc:
            result.status = "cancelled" if self._cancel.is_set() else "failed"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - report, then make the instrument safe below
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.gw.remove_stop_listener(on_stop)
            try:
                self.gw.execute("led.all_off", actor=Actor.ACQUISITION)
            except Exception:
                log.exception("could not switch LEDs off after acquisition %s", run_id)
            result.finished = time.time()
            self.gw.note("acquisition.end", actor=Actor.ACQUISITION,
                         details={"run_id": run_id, "status": result.status, "images": result.images},
                         reason=result.error)
        return result

    # ------------------------------------------------------------------------------
    def _check(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    def _run(self, plan: AcquisitionPlan, result: AcquisitionResult, kw: dict[str, Any],
             on_progress: Callable[[Progress], None] | None,
             on_frame: Callable[[AcquiredFrame], None] | None) -> None:
        gw, ctl = self.gw, self.ctl
        for role in (Role.CAMERA_MONO, Role.CAMERA_COLOR):
            if gw.has(role) and gw.read(role).is_live():
                gw.execute("camera.stop_live", camera=role.value, **kw)
        x0, y0 = ctl.xy_um()
        z_ref = ctl.z_um()
        positions = plan.positions or [StagePosition("current", x0, y0, z_ref)]
        offsets = plan.z_offsets()
        total = plan.image_count()
        meta_file = (result.save_dir / "frames.jsonl") if result.save_dir else None
        for t in range(plan.timepoints()):
            t_start = time.monotonic()
            for p, pos in enumerate(positions):
                self._check()
                gw.execute("xy.move_absolute", x_um=pos.x_um, y_um=pos.y_um, wait=True, **kw)
                z_base = pos.z_um if pos.z_um is not None else z_ref
                for c, step in enumerate(plan.channels):
                    self._check()
                    preset = ctl.preset(step.preset)
                    ctl._apply_channel(preset, kw["actor"], kw["owner"], light_on=True,
                                       settings=ChannelSettings(step.exposure_ms, step.intensity_pct,
                                                                step.gain_db), start_live=False)
                    for z, dz in enumerate(offsets):
                        self._check()
                        z_um = z_base + dz
                        gw.execute("focus.move_absolute", z_um=z_um, wait=True, **kw)
                        frame = gw.execute("camera.snap", camera=preset.camera.value, **kw).value
                        acq = AcquiredFrame(t, p, c, z, preset.name, pos.label, z_um, frame)
                        result.frames.append(acq)
                        result.images += 1
                        if meta_file is not None:
                            self._save(result.save_dir, meta_file, acq)
                        if on_frame:
                            on_frame(acq)
                        if on_progress:
                            on_progress(Progress(result.images, total, t, p, c, z,
                                                 f"t{t + 1} · {pos.label} · {preset.name} · z{z + 1}"))
                    gw.execute("led.all_off", **kw)
            if t + 1 < plan.timepoints() and plan.time_lapse:
                remaining = plan.time_lapse.interval_s - (time.monotonic() - t_start)
                if remaining > 0 and self._cancel.wait(remaining):
                    raise _Cancelled()
        # leave focus where the operator had it
        gw.execute("focus.move_absolute", z_um=z_ref, wait=True, **kw)

    @staticmethod
    def _save(folder: Path | None, meta_file: Path, acq: AcquiredFrame) -> None:
        import numpy as np  # local: keeps `core` importable without numpy for non-imaging use

        assert folder is not None
        name = f"t{acq.t:04d}_p{acq.p:03d}_c{acq.c:02d}_z{acq.z:03d}.npy"
        np.save(folder / name, acq.frame.pixels)
        record = {"file": name, "t": acq.t, "p": acq.p, "c": acq.c, "z": acq.z,
                  "channel": acq.channel, "position": acq.position, "z_um": acq.z_um,
                  "exposure_ms": acq.frame.exposure_ms, "gain_db": acq.frame.gain_db,
                  "timestamp": acq.frame.timestamp, "simulated": acq.frame.simulated,
                  "metadata": acq.frame.metadata}
        with meta_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


class _Cancelled(Exception):
    pass
