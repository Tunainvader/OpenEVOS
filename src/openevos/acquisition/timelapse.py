"""Time-lapse routines (manual p.28-32): beacons (up to 384, Populate Wells), scenes with length,
interval and environmental setpoints, "capture one frame only" (single scene only), Auto find
sample / Auto fine focus per routine, pause/resume/stop. Pause adjustments do not persist into the
saved routine (manual p.32).

Environmental values are *setpoints recorded with the routine*. With no incubator control or
telemetry path (bring-up record: AMC1000 unavailable), the runner does not pretend to apply them.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevos.acquisition.capture import CapturedImage, ZStack, zstack_slices_centred
from openevos.acquisition.scan_plan import Beacon
from openevos.acquisition.scan_runner import RunStopped, ScanRunner
from openevos.instrument.profile import CameraRole, InstrumentProfile
from openevos.instrument.vessels import Vessel, VesselStageMap, well_label_sort_key
from openevos.safety.restrictions import run_guard

MAX_BEACONS = 384


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_s: float = Field(gt=0)
    interval_s: float = Field(gt=0)
    temperature_c: float | None = Field(default=None, ge=20, le=40)
    co2_percent: float | None = Field(default=None, ge=0, le=20)
    o2_percent: float | None = Field(default=None, ge=1, le=21)
    humidity: bool | None = None

    @property
    def timepoints(self) -> int:
        return math.floor(self.duration_s / self.interval_s + 1e-9) + 1


class TimelapseRoutine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    vessel_id: str
    objective_slot: int = Field(ge=1, le=5)
    camera: CameraRole = CameraRole.MONO
    channels: list[str] = Field(min_length=1)
    auto_find_sample: bool = False
    auto_fine_focus: bool = True
    focus_channel: str | None = None  # channel used for autofocus; None => first channel
    zstack: bool = False  # Z-stack at every beacon and time point
    zstack_range_um: float = Field(default=10.0, gt=0, le=500)  # total range, centred on the focus
    zstack_step_um: float = Field(default=2.0, gt=0, le=100)
    beacons: list[Beacon] = Field(min_length=1)
    scenes: list[Scene] = Field(min_length=1)
    capture_one_frame_only: bool = False
    video_per_beacon: bool = True
    watermark: bool = True
    save_channels_separately: bool = False
    output_folder: str = ""

    @model_validator(mode="after")
    def _rules(self) -> TimelapseRoutine:
        if len(self.beacons) > MAX_BEACONS:
            raise ValueError(f"at most {MAX_BEACONS} beacons (manual p.30)")
        if self.capture_one_frame_only and len(self.scenes) != 1:
            raise ValueError("'Capture one frame only' is only available with a single scene (manual p.31)")
        if self.focus_channel is not None and self.focus_channel not in self.channels:
            raise ValueError("focus channel must be one of the routine channels")
        return self


def populate_wells(vessel: Vessel, stage_map: VesselStageMap | None = None) -> list[Beacon]:
    """One beacon at the centre of each region (Populate Wells, manual p.30)."""
    regs = sorted(vessel.regions, key=lambda r: well_label_sort_key(r.region_id))
    return [Beacon(label=r.region_id, x_mm=r.cx, y_mm=r.cy) for r in regs][:MAX_BEACONS]


@dataclass(frozen=True)
class TimelapseEstimate:
    timepoints: int
    images: int
    cycle_s: float
    total_s: float
    warnings: tuple[str, ...]


def _n_slices(r: TimelapseRoutine) -> int:
    """Slices per stack, counted as the stack itself counts them (capture.plan_zstack: never past the range)."""
    return zstack_slices_centred(r.zstack_range_um, r.zstack_step_um)


def estimate(r: TimelapseRoutine, profile: InstrumentProfile, *, environment_available: bool = False,
             seconds_per_position: float = 1.2) -> TimelapseEstimate:
    w: list[str] = []
    for ch in r.channels:
        c, cam = profile.channel(ch), profile.camera(r.camera)
        if not c or not cam or not cam.supports(c):
            w.append(f"channel {ch} not available on the {r.camera.value} camera")
    tp = 1 if r.capture_one_frame_only else sum(s.timepoints for s in r.scenes)
    n_z = _n_slices(r) if r.zstack else 1
    per_pos = seconds_per_position + 0.4 * len(r.channels) + (2.5 if r.auto_fine_focus else 0) \
        + (6.0 if r.auto_find_sample else 0)
    cycle = len(r.beacons) * per_pos
    for i, s in enumerate(r.scenes, 1):
        if cycle > s.interval_s and not r.capture_one_frame_only:
            w.append(f"scene {i}: one cycle takes ~{cycle:.0f} s but the interval is {s.interval_s:.0f} s; "
                     "timepoints will run late")
        if any(v is not None for v in (s.temperature_c, s.co2_percent, s.o2_percent, s.humidity)) \
                and not environment_available:
            w.append(f"scene {i}: environmental setpoints are recorded only — incubator control unavailable")
    if r.zstack:
        n_z = _n_slices(r)
        cycle += len(r.beacons) * len(r.channels) * n_z * 0.35
        if n_z > 200:
            w.append(f"Z-stack of {n_z} slices exceeds the 200-slice limit")
    total = cycle if r.capture_one_frame_only else sum(s.duration_s for s in r.scenes) + cycle
    return TimelapseEstimate(tp, tp * len(r.beacons) * len(r.channels) * n_z, cycle, total, tuple(w))


@dataclass
class TimelapseResult:
    routine: TimelapseRoutine
    frames: list[tuple[int, str, dict[str, CapturedImage]]] = field(default_factory=list)  # (timepoint, beacon, imgs)
    stacks: dict[tuple[int, str], dict] = field(default_factory=dict)  # (timepoint, beacon) -> {slot: ZStack}
    #: with an output folder, Z-stacks go to disk as they are taken and only their folders are kept here, so a long
    #: time-lapse does not fill memory (ADR-0064)
    stack_folders: dict[tuple[int, str], str] = field(default_factory=dict)
    completed: bool = False
    message: str = ""
    late_timepoints: int = 0
    output_folder: str | None = None  # where the Z-stacks were written (routine output folder / name)


class TimelapseRunner:
    """Runs on a worker thread. `clock_monotonic` + RunControl.sleep pace the timepoints."""

    def __init__(self, scan_runner: ScanRunner, stage_map: VesselStageMap,
                 monotonic: Callable[[], float]):
        self.sr, self.map, self.monotonic = scan_runner, stage_map, monotonic
        self.control = scan_runner.control

    def run(self, r: TimelapseRoutine, progress: Callable[[int, int, str], None] | None = None,
            on_timepoint: Callable[[int, list[tuple[str, dict[str, CapturedImage]]]], None] | None = None
            ) -> TimelapseResult:
        """Inside the SafetyManager's run guard (ADR-0070), like a scan."""
        cap = self.sr.capture
        caps = cap.run_capabilities(r.camera, positions=2, saving=bool(r.output_folder)) \
            if hasattr(cap, "run_capabilities") else set()
        with run_guard(cap, "Time-lapse", caps, self.control.stop):
            return self._run(r, progress, on_timepoint)

    def _run(self, r: TimelapseRoutine, progress: Callable[[int, int, str], None] | None = None,
             on_timepoint: Callable[[int, list[tuple[str, dict[str, CapturedImage]]]], None] | None = None
             ) -> TimelapseResult:
        res = TimelapseResult(r)
        schedule: list[float] = []
        t_acc = 0.0
        for s in ([r.scenes[0]] if r.capture_one_frame_only else r.scenes):
            n = 1 if r.capture_one_frame_only else s.timepoints
            schedule += [t_acc + i * s.interval_s for i in range(n)]
            t_acc += s.duration_s
        ctl = self.sr.capture.ctl
        z_by_beacon: dict[str, float | None] = {b.label: b.z_um for b in r.beacons}
        folder: Path | None = None
        if r.output_folder and r.zstack:  # each stack is on disk as soon as it is taken
            folder = Path(r.output_folder) / r.name
            res.output_folder = str(folder)
        try:
            ctl.run(ctl.set_objective(r.objective_slot))
            start = self.monotonic()
            for tp, due in enumerate(schedule):
                wait = start + due - self.monotonic()
                if wait > 0:
                    self.control.sleep(wait)
                elif tp:
                    res.late_timepoints += 1
                batch = []
                for b in r.beacons:
                    if progress:
                        progress(tp, len(schedule), f"time point {tp + 1}/{len(schedule)} · {b.label}")
                    x, y = self.map.to_stage(b.x_mm, b.y_mm)
                    imgs = self.sr.visit(x, y, r.channels, r.camera, find_sample=r.auto_find_sample and tp == 0,
                                         auto_fine=r.auto_fine_focus, log=[], index=tp, z=z_by_beacon[b.label])
                    z_by_beacon[b.label] = ctl.focus_z()  # track focus drift per beacon
                    if r.zstack:
                        self._zstack(r, res, tp, b.label, progress, len(schedule))
                    res.frames.append((tp, b.label, imgs))
                    batch.append((b.label, imgs))
                if on_timepoint:
                    on_timepoint(tp, batch)
            res.completed = True
            res.message = f"{len(schedule)} time points captured"
        except RunStopped as exc:
            res.message = f"{exc} at time point {len({f[0] for f in res.frames})}/{len(schedule)}"
        except Exception as exc:  # noqa: BLE001
            res.message = f"failed: {exc}"
        res.stacks = {k: v for k, v in res.stacks.items() if v}  # a stop before a stack's first slice leaves none
        if progress:
            progress(len(schedule), len(schedule), res.message)
        return res

    def _zstack(self, r: TimelapseRoutine, res: TimelapseResult, tp: int, label: str,
                progress: Callable[[int, int, str], None] | None = None, n_tp: int = 1) -> None:
        """Every channel of one position's Z-stack in one engine run centred on the tracked focus (ADR-0064), sharing
        the routine's RunControl. It never replaces the Z-stack page's stack. With an output folder the stacks are
        written at once, one folder per position and time point (`t001_<position>`, a stopped one too), and only the
        folder is kept, so a long time-lapse does not fill memory; without one they stay in the result. The
        Time-lapse page opens either on the Z-stack page. Then a Stop raises RunStopped and a failure RuntimeError."""
        from openevos.imaging.zstack_io import save_zstack_set

        out: dict[str, ZStack] = res.stacks.setdefault((tp, label), {})

        def report(text: str) -> None:
            if progress:
                progress(tp, n_tp, f"time point {tp + 1}/{n_tp} · {label} · Z-stack: {text}")

        failure: Exception | None = None
        try:
            self.sr.stacks(r.channels, r.camera, r.zstack_range_um, r.zstack_step_um, out, report=report)
        except (RunStopped, RuntimeError) as exc:
            failure = exc
        if out and res.output_folder:
            from openevos.acquisition.zstack import zstack_set_of

            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
            zs = zstack_set_of(out, r.camera, str(failure or ""))
            stopped = "" if zs.complete else "_stopped"
            res.stack_folders[(tp, label)] = str(save_zstack_set(Path(res.output_folder), zs,
                                                                 name=f"t{tp + 1:03d}_{safe}{stopped}"))
            res.stacks.pop((tp, label), None)
        if failure is not None:
            raise failure
