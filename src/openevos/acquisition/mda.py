"""Multi-D Acquisition (ADR-0065): one experiment over Z, channels (C), timepoints (T) and XY positions (P).

The plan says *what* to acquire; the compiler turns it into a deterministic, lazy sequence of events (*the order*);
the runner (``mda_runner``) executes it through the existing ``ZStackRunner`` and the safety checks; the writer
(``imaging.mda_store``) records *what actually happened*. Nothing in this module moves hardware or imports Qt.

Model
-----
* Z is relative to a focus **anchor**: one global anchor (an absolute focus Z, usually "the focus now"), or one anchor
  per position. Centre mode: the anchor plane plus whole steps below and above it. First/last mode: from
  ``anchor + first`` up to ``anchor + last`` in whole steps (never past the last). Z off: one plane at the anchor.
* C: channels with a snapshot of exposure, gain and light taken when the plan is made.
* T: timepoints with a minimum start-to-start interval (default) or a fixed schedule with a missed-point policy.
* P: named XY stage positions (a list of fields, *not* tiles and not an image axis). With no positions the run stays
  where the stage is.
* Traversal: two supported orders, outer to inner, the right-most changing fastest: ``T → P → C → Z`` (default,
  channel by channel in each volume) and ``T → P → Z → C`` (all channels at each plane). No free permutation editor:
  other orders change the meaning of time and are deferred.

Event identity: ``T0001P0002C01Z0007`` (one-based in the id and the UI, zero-based indices in the fields). Ids are
stable for a frozen plan, so a journal can be reconciled against the plan after a stop.

Kit references: OpenEVOS Multi-Dimensional Acquisition Kit v1.0, docs 00-06 (MDA-001..MDA-005 in its backlog).
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.zstack import (
    CHANNEL_CHANGE_S,
    DEFAULT_FRAME,
    MAX_SLICES,
    SLICE_OVERHEAD_S,
    ChannelOrder,
    ZStackPlan,
    plan_positions,
)
from openevos.instrument.profile import CameraRole

SCHEMA_VERSION = 1
#: a plan above this many images is refused (memory, journal size and run time have not been qualified beyond it)
MAX_EVENTS = 100_000
MAX_TIMEPOINTS = 10_000
MAX_POSITIONS = 384  # as the time-lapse beacons (manual p.30)
#: XY moves are estimated, not measured: stage speed and settling on the AMAFD1000 have not been timed
XY_SPEED_UM_S = 5_000.0
XY_SETTLE_S = 0.5
STORAGE_RESERVE_MB = 512.0
#: the one physical gate the kit sets that software can check: multi-position runs on the real stage stay off until the
#: XY stage and its cross-axis feedback are qualified (kit doc 04 §9, MDA-035). Simulation is not affected.
PHYSICAL_POSITIONS_QUALIFIED = False


class Traversal(str, Enum):
    T_P_C_Z = "time_position_channel_z"
    T_P_Z_C = "time_position_z_channel"

    @property
    def label(self) -> str:
        return "T → P → C → Z" if self is Traversal.T_P_C_Z else "T → P → Z → C"

    @property
    def order(self) -> ChannelOrder:
        return ChannelOrder.CHANNEL_FIRST if self is Traversal.T_P_C_Z else ChannelOrder.Z_FIRST


class Schedule(str, Enum):
    MIN_INTERVAL = "minimum_start_interval"
    FIXED = "fixed_schedule"


class Missed(str, Enum):
    SKIP = "skip_missed"
    RUN_LATE_ONCE = "run_late_once"


class FocusPolicy(str, Enum):
    GLOBAL = "global"
    PER_POSITION = "per_position"


@dataclass(frozen=True)
class ZAxisPlan:
    enabled: bool = True
    mode: str = "centre"  # "centre" | "first_last"
    below_um: float = 10.0  # centre mode
    above_um: float = 10.0
    first_um: float = -10.0  # first/last mode, relative to the anchor
    last_um: float = 10.0
    interval_um: float = 1.0
    keep: str = "interval"  # "interval" | "slices": which one holds when the range changes
    target_slices: int | None = None
    approach_um: float = 2.0
    settle_ms: float = 0.0
    light_between_slices: bool = False


@dataclass(frozen=True)
class ChannelPlan:
    slot: str
    label: str
    exposure_ms: float
    gain: float
    led_percent: float
    emission_nm: int | None = None
    fluorescence: bool = True


@dataclass(frozen=True)
class PositionPlan:
    position_id: str  # "P0001"
    label: str
    x: float | None = None  # None: stay where the stage is (no XY move)
    y: float | None = None
    unit: str = "um"
    frame: str = ""  # the stage's coordinate frame id; required when x/y are given
    z_anchor_um: float | None = None
    source: str = "current"  # "current" | "saved" | "vessel"

    @property
    def moves(self) -> bool:
        return self.x is not None and self.y is not None


@dataclass(frozen=True)
class TimeAxisPlan:
    enabled: bool = False
    timepoints: int = 1
    interval_s: float = 600.0
    schedule: Schedule = Schedule.MIN_INTERVAL
    missed: Missed = Missed.SKIP


@dataclass(frozen=True)
class MultiDimPlan:
    channels: tuple[ChannelPlan, ...]
    camera: CameraRole = CameraRole.MONO
    z: ZAxisPlan = field(default_factory=ZAxisPlan)
    time: TimeAxisPlan = field(default_factory=TimeAxisPlan)
    positions: tuple[PositionPlan, ...] = (PositionPlan("P0001", "Here"),)
    traversal: Traversal = Traversal.T_P_C_Z
    focus_policy: FocusPolicy = FocusPolicy.GLOBAL
    global_anchor_z_um: float | None = None
    projections: tuple[str, ...] = ("max",)
    name: str = "Experiment"
    instrument: str = ""
    simulated: bool = True
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # ------------------------------------------------------------------ identity and serialisation
    def to_dict(self) -> dict:
        d = asdict(self)
        d["camera"] = self.camera.value
        d["traversal"] = self.traversal.value
        d["focus_policy"] = self.focus_policy.value
        d["time"]["schedule"] = self.time.schedule.value
        d["time"]["missed"] = self.time.missed.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> MultiDimPlan:
        v = int(d.get("schema_version", 0))
        if v != SCHEMA_VERSION:
            raise ValueError(f"plan schema {v} is not supported (this OpenEVOS reads schema {SCHEMA_VERSION})")
        t = dict(d["time"])
        t["schedule"], t["missed"] = Schedule(t["schedule"]), Missed(t["missed"])
        return cls(channels=tuple(ChannelPlan(**c) for c in d["channels"]), camera=CameraRole(d["camera"]),
                   z=ZAxisPlan(**d["z"]), time=TimeAxisPlan(**t),
                   positions=tuple(PositionPlan(**p) for p in d["positions"]), traversal=Traversal(d["traversal"]),
                   focus_policy=FocusPolicy(d["focus_policy"]), global_anchor_z_um=d.get("global_anchor_z_um"),
                   projections=tuple(d.get("projections", ())), name=d.get("name", "Experiment"),
                   instrument=d.get("instrument", ""), simulated=bool(d.get("simulated", True)),
                   plan_id=d["plan_id"], schema_version=v, created_at=d["created_at"])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> MultiDimPlan:
        return cls.from_dict(json.loads(text))

    def content_hash(self) -> str:
        """SHA-256 of what the plan acquires (not its id or creation time): equal plans hash equal."""
        d = self.to_dict()
        for k in ("plan_id", "created_at", "name"):
            d.pop(k, None)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()

    def fresh(self) -> MultiDimPlan:
        """The same plan with a new id and creation time (a re-run is a new experiment)."""
        return replace(self, plan_id=uuid.uuid4().hex[:12], created_at=datetime.now(UTC).isoformat())

    # ------------------------------------------------------------------ dimensions
    @property
    def n_t(self) -> int:
        return self.time.timepoints if self.time.enabled else 1

    @property
    def n_p(self) -> int:
        return len(self.positions)

    @property
    def n_c(self) -> int:
        return len(self.channels)

    def z_offsets(self) -> list[float]:
        """Planned planes relative to the anchor, bottom first."""
        z = self.z
        if not z.enabled:
            return [0.0]
        if z.mode == "first_last":
            return plan_positions(z.first_um, z.last_um, z.interval_um)
        return plan_positions(-z.below_um, z.above_um, z.interval_um, 0.0)

    @property
    def n_z(self) -> int:
        try:
            return len(self.z_offsets())
        except CaptureRejected:
            return 0

    @property
    def n_events(self) -> int:
        return self.n_t * self.n_p * self.n_c * self.n_z

    def anchor_for(self, pos: PositionPlan) -> float | None:
        if self.focus_policy is FocusPolicy.PER_POSITION:
            return pos.z_anchor_um
        return self.global_anchor_z_um


def event_id(t: int, p: int, c: int, z: int) -> str:
    return f"T{t + 1:04d}P{p + 1:04d}C{c + 1:02d}Z{z + 1:04d}"


@dataclass(frozen=True)
class VolumeJob:
    """One Z x C volume at one position and timepoint: what ``ZStackRunner`` executes."""

    t_index: int
    p_index: int
    position: PositionPlan
    anchor_um: float
    planned_z_um: tuple[float, ...]

    @property
    def key(self) -> str:
        return f"T{self.t_index + 1:06d}P{self.p_index + 1:04d}"

    def zstack_plan(self, plan: MultiDimPlan, *, confirmed_above_safe: bool) -> ZStackPlan:
        z = plan.z
        lo, hi = self.planned_z_um[0], self.planned_z_um[-1]
        centre = self.anchor_um if (not z.enabled or z.mode == "centre") else None
        return ZStackPlan(channels=tuple(c.slot for c in plan.channels), role=plan.camera, bottom_um=lo, top_um=hi,
                          step_um=z.interval_um if z.enabled else 1.0, centre_um=centre, order=plan.traversal.order,
                          light_between_slices=z.light_between_slices, settle_ms=z.settle_ms,
                          approach_um=z.approach_um if z.enabled else 0.0, return_to_start=True,
                          confirmed_above_safe=confirmed_above_safe, store=False)


@dataclass(frozen=True)
class MDAEvent:
    event_id: str
    t_index: int
    p_index: int
    c_index: int
    z_index: int
    position_id: str
    channel: str
    z_um: float
    scheduled_offset_s: float | None


def compile_jobs(plan: MultiDimPlan) -> Iterator[VolumeJob]:
    """Lazily, timepoints outer and positions inner (both supported traversals are timepoint-synchronous)."""
    offsets = plan.z_offsets()
    for t in range(plan.n_t):
        for p, pos in enumerate(plan.positions):
            anchor = plan.anchor_for(pos)
            if anchor is None:
                raise CaptureRejected(f"no focus reference for {pos.label}")
            yield VolumeJob(t, p, pos, anchor, tuple(anchor + o for o in offsets))


def compile_events(plan: MultiDimPlan) -> Iterator[MDAEvent]:
    """Every frame in acquisition order, lazily (a 100,000-image plan is never held in memory)."""
    step = plan.time.interval_s if plan.time.enabled else None
    for job in compile_jobs(plan):
        sched = job.t_index * step if step is not None and plan.time.schedule is Schedule.FIXED else None
        zs = range(len(job.planned_z_um))
        cs = range(plan.n_c)
        pairs = ((c, z) for c in cs for z in zs) if plan.traversal is Traversal.T_P_C_Z else \
            ((c, z) for z in zs for c in cs)
        for c, z in pairs:
            yield MDAEvent(event_id(job.t_index, job.p_index, c, z), job.t_index, job.p_index, c, z,
                           job.position.position_id, plan.channels[c].slot, job.planned_z_um[z], sched)


# ====================================================================================== validation
def validate(plan: MultiDimPlan) -> list[str]:
    """Plan errors that need no hardware (kit MDA-002, MDA-P005..P009)."""
    e: list[str] = []
    if not plan.channels:
        e.append("choose at least one channel")
    slots = [c.slot for c in plan.channels]
    if len(set(slots)) != len(slots):
        e.append("a channel is listed twice")
    z = plan.z
    if z.enabled:
        if z.interval_um <= 0:
            e.append("the Z step must be greater than zero")
        if z.mode == "centre" and (z.below_um < 0 or z.above_um < 0):
            e.append("Z range below and above the anchor cannot be negative")
        if z.mode == "first_last" and z.last_um <= z.first_um:
            e.append("the last plane must be above the first")
        if z.mode not in ("centre", "first_last"):
            e.append(f"unknown Z mode {z.mode!r}")
        n = plan.n_z
        if n > MAX_SLICES:
            e.append(f"{n} Z planes; the limit is {MAX_SLICES}")
    if plan.time.enabled:
        if not 1 <= plan.time.timepoints <= MAX_TIMEPOINTS:
            e.append(f"timepoints must be 1 to {MAX_TIMEPOINTS}")
        if plan.time.timepoints > 1 and plan.time.interval_s <= 0:
            e.append("the time interval must be greater than zero")
    if not plan.positions:
        e.append("at least one position")
    if len(plan.positions) > MAX_POSITIONS:
        e.append(f"at most {MAX_POSITIONS} positions")
    ids = [p.position_id for p in plan.positions]
    if len(set(ids)) != len(ids):
        e.append("two positions have the same id")
    for p in plan.positions:
        if (p.x is None) != (p.y is None):
            e.append(f"{p.label}: give both X and Y, or neither")
        if p.moves and not p.frame:
            e.append(f"{p.label}: unknown coordinate frame")
        if plan.focus_policy is FocusPolicy.PER_POSITION and p.z_anchor_um is None:
            e.append(f"{p.label}: no focus reference (per-position focus needs one for every position)")
    if plan.focus_policy is FocusPolicy.GLOBAL and plan.global_anchor_z_um is None:
        e.append("no focus reference: set it from the focus now")
    if not isinstance(plan.traversal, Traversal):
        e.append(f"unsupported order {plan.traversal!r}")
    if plan.n_events > MAX_EVENTS:
        e.append(f"{plan.n_events:,} images; the limit is {MAX_EVENTS:,}")
    return e


# ====================================================================================== summary
@dataclass(frozen=True)
class PlanSummary:
    """Counts are exact; times and sizes are estimates and say what they come from (kit doc 01 §11)."""

    n_t: int
    n_p: int
    n_c: int
    n_z: int
    images: int
    frame_shape: tuple[int, int]
    bytes_per_image: int
    frame_source: str  # "camera profile" | "default (camera size unknown)"
    volume_s: float
    xy_cycle_s: float
    cycle_s: float
    total_s: float
    late: bool
    light_s: dict[str, float]
    preview: tuple[MDAEvent, ...]

    @property
    def megabytes(self) -> float:
        return self.images * self.bytes_per_image / 1e6


def summarize(plan: MultiDimPlan, profile, *, preview: int = 8) -> PlanSummary:
    """Never commands hardware (kit MDA-004)."""
    cam = profile.camera(plan.camera) if profile is not None else None
    if cam and cam.height and cam.width:
        shape, source = (cam.height, cam.width), "camera profile"
    else:
        shape, source = DEFAULT_FRAME, "default (camera size unknown)"
    deep = (cam.native_bit_depth or 16) > 8 if cam else True
    bpp = 3 if plan.camera is CameraRole.COLOR else (2 if deep else 1)
    n_z, n_c = max(plan.n_z, 0), plan.n_c
    exp_s = sum(c.exposure_ms for c in plan.channels) / 1000
    z = plan.z
    settle = z.settle_ms / 1000
    per_plane = SLICE_OVERHEAD_S * n_c + exp_s + settle * (n_c if plan.traversal is Traversal.T_P_C_Z else 1)
    changes = (n_c - 1) if plan.traversal is Traversal.T_P_C_Z else (n_c - 1) * n_z
    volume_s = n_z * per_plane + changes * CHANNEL_CHANGE_S + 1.0
    xy = 0.0
    pts = [(p.x, p.y) for p in plan.positions if p.moves]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1], strict=False):
        assert x0 is not None and y0 is not None and x1 is not None and y1 is not None
        xy += math.hypot(x1 - x0, y1 - y0) / XY_SPEED_UM_S + XY_SETTLE_S
    if len(pts) <= 1:
        xy = XY_SETTLE_S if pts else 0.0
    cycle = plan.n_p * volume_s + xy
    t = plan.time
    if plan.n_t > 1:
        total = (plan.n_t - 1) * max(t.interval_s, cycle) + cycle
        late = cycle > t.interval_s
    else:
        total, late = cycle, False
    per_volume_light = {c.slot: (n_z * c.exposure_ms / 1000 if z.light_between_slices else
                                 volume_s / max(1, n_c)) for c in plan.channels}
    light = {k: v * plan.n_p * plan.n_t for k, v in per_volume_light.items()}
    ev: tuple[MDAEvent, ...] = ()
    try:
        it = compile_events(plan)
        ev = tuple(e for _, e in zip(range(preview), it, strict=False))
    except CaptureRejected:
        pass
    return PlanSummary(plan.n_t, plan.n_p, n_c, n_z, plan.n_events, shape, shape[0] * shape[1] * bpp, source,
                       volume_s, xy, cycle, total, late, light, ev)


# ====================================================================================== preflight
@dataclass(frozen=True)
class MDAPreflight:
    errors: tuple[str, ...] = ()
    confirm: str | None = None
    notes: tuple[str, ...] = ()
    checks: tuple[tuple[str, str, str], ...] = ()  # (name, "ok" | "warn" | "fail" | "skip", detail)

    @property
    def ok(self) -> bool:
        return not self.errors


def _free_mb(folder: Path) -> float | None:
    p = folder
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e6
    except OSError:
        return None


def preflight(plan: MultiDimPlan, session, summary: PlanSummary | None = None, *,
              save_root: Path | None = None) -> MDAPreflight:
    """Experiment-level checks before the first move (kit MDA-005, doc 01 §11). Never moves anything."""
    errors = list(validate(plan))
    notes: list[str] = []
    checks: list[tuple[str, str, str]] = []
    cap, ctl, prof = session.capture, session.controller, session.profile
    summary = summary or summarize(plan, prof)
    # channels on this camera
    bad = []
    for c in plan.channels:
        try:
            cap.check_capture(plan.camera, c.slot)
        except CaptureRejected as exc:
            bad.append(str(exc))
    errors += bad
    checks.append(("Channels", "fail" if bad else "ok", "; ".join(bad) or f"{plan.n_c} on the {plan.camera.value} "
                                                                           "camera"))
    # focus telemetry and every plane of every position inside the focus limits; safe height
    zf = ctl.focus_z()
    if zf is None:
        errors.append("focus position unknown (no fresh focus read-back)")
    lim = ctl.axis_limits(prof.focus_device, "z") if prof.focus_device else None
    safe = cap.safe_height_for() if getattr(cap, "safe_height_for", None) else None
    out_of_range: list[str] = []
    above = 0
    above_where: list[str] = []
    try:
        offsets = plan.z_offsets()
    except CaptureRejected as exc:
        offsets = []
        errors.append(str(exc))
    for pos in plan.positions:
        a = plan.anchor_for(pos)
        if a is None or not offsets:
            continue
        lo, hi = a + offsets[0], a + offsets[-1]
        lo_app = lo - (plan.z.approach_um if plan.z.enabled else 0.0)
        if lim and (lo < lim[0] - 1e-6 or hi > lim[1] + 1e-6):
            out_of_range.append(f"{pos.label} ({lo:,.1f}–{hi:,.1f} µm)")
        elif lim and lo_app < lim[0]:
            notes.append(f"{pos.label}: the bottom is at the focus limit; its first plane is approached from above")
        if safe and hi > safe[1] + 0.5:
            above += sum(1 for o in offsets if a + o > safe[1] + 0.5)
            above_where.append(pos.label)
    if out_of_range:
        errors.append("outside the focus limits"
                      + (f" {lim[0]:,.0f}–{lim[1]:,.0f} µm" if lim else "") + ": " + ", ".join(out_of_range))
    checks.append(("Travel", "fail" if out_of_range else "ok",
                   "; ".join(out_of_range) or "every plane inside the focus limits"))
    confirm = None
    if above:
        assert safe is not None
        confirm = (f"{above} plane{'s' if above != 1 else ''} at {', '.join(above_where[:4])}"
                   f"{'…' if len(above_where) > 4 else ''} {'are' if above != 1 else 'is'} above the {safe[0]} safe "
                   f"height ({safe[1]:,.1f} µm), where the objective nears the vessel. Run above the safe height?")
    checks.append(("Safe height", "warn" if confirm else ("ok" if safe else "skip"),
                   "planes above it: you will be asked" if confirm else
                   ("below it everywhere" if safe else "no safe height set for this objective")))
    # XY positions: limits, frame, telemetry, and the physical gate
    movers = [p for p in plan.positions if p.moves]
    if movers:
        physical = (not getattr(session, "simulated", True)) or getattr(session, "physical_stage", False)
        if physical and not PHYSICAL_POSITIONS_QUALIFIED:
            errors.append("multi-position runs on the instrument stay off until the XY stage and its cross-axis "
                          "feedback are qualified (ADR-0065); run in simulation, or with no positions")
        if ctl.stage_xy() is None:
            errors.append("stage position unknown (no fresh XY read-back)")
        lx = ctl.axis_limits(prof.stage_device, "x")
        ly = ctl.axis_limits(prof.stage_device, "y")
        unit, frame = ctl.stage_frame()
        outside = [p.label for p in movers if lx and ly and p.x is not None and p.y is not None and
                   not (lx[0] <= p.x <= lx[1] and ly[0] <= p.y <= ly[1])]
        wrong = [p.label for p in movers if (p.unit != unit) or (frame and p.frame != frame)]
        if outside:
            errors.append("outside the stage limits: " + ", ".join(outside))
        if wrong:
            errors.append("saved in another coordinate frame or unit: " + ", ".join(wrong))
        checks.append(("Stage", "fail" if (outside or wrong or (physical and not PHYSICAL_POSITIONS_QUALIFIED))
                       else "ok", f"{len(movers)} position{'s' if len(movers) != 1 else ''}"
                       + (" (simulated stage)" if not physical else "")))
    else:
        checks.append(("Stage", "skip", "no XY moves: the run stays where the stage is"))
    # time
    if plan.n_t > 1 and summary.late:
        notes.append(f"one cycle takes about {summary.cycle_s:.0f} s but the interval is {plan.time.interval_s:.0f} s:"
                     + (" each timepoint starts when the last one ends, marked late"
                        if plan.time.schedule is Schedule.MIN_INTERVAL else
                        (" missed timepoints are skipped" if plan.time.missed is Missed.SKIP else
                         " a missed timepoint runs late once; no catch-up burst")))
    # storage
    if save_root is not None:
        need = summary.megabytes * 1.1 + STORAGE_RESERVE_MB
        free = _free_mb(save_root)
        if free is None:
            checks.append(("Storage", "warn", f"cannot read free space at {save_root}"))
        elif free < need:
            errors.append(f"not enough space at {save_root}: needs ≈ {need:,.0f} MB (with a {STORAGE_RESERVE_MB:.0f} "
                          f"MB reserve), {free:,.0f} MB free")
            checks.append(("Storage", "fail", f"{free:,.0f} MB free, needs ≈ {need:,.0f} MB"))
        else:
            checks.append(("Storage", "ok", f"{free / 1000:,.1f} GB free, needs ≈ {need / 1000:,.2f} GB"))
    else:
        errors.append("choose where to save the experiment")
        checks.append(("Storage", "fail", "no save folder"))
    if summary.frame_source != "camera profile":
        notes.append("the size estimate uses a default frame size: the camera profile has none")
    return MDAPreflight(tuple(dict.fromkeys(errors)), confirm, tuple(notes), tuple(checks))
