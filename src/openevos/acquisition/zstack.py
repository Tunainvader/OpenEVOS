"""Z-stack engine (ADR-0064): plan, pre-flight, run, keep.

What the audit of 2026-11-24 asked of a Z-stack, and where it is done here:

* **Plan inside the range asked** (`plan_positions`): first/last mode goes up from the bottom in whole steps and never
  past the top; centre mode builds outward from the centre so the focus plane itself is a slice (ZF-07).
* **Check everything before moving** (`preflight`): steps, the 200-slice cap, channels the camera can image, the focus
  limits, and the objective's safe height. A top above the safe height needs the operator's confirmation *before*
  the first move, instead of a refusal mid-stack (ZF-04). It also estimates time, memory and light-on time.
* **Run cleanly** (`ZStackRunner`):
  - live view is paused once for the whole stack, not around every slice (ZF-03);
  - the first slice is approached from below like the rest (a small under-shoot; moving down is always allowed),
    so every slice sees the drive's backlash the same way (ZF-08);
  - optional settle time after each move (ZF-09), and optional light off between exposures;
  - the light is put back exactly as it was, on every exit path (ZF-01);
  - Stop is checked between every move and snap, and keeps the slices taken (ZF-02, ZF-05);
  - focus returns to the start after success or a refusal, but a Stop never causes a new move (ZF-11);
  - channel order: all slices of one channel, then the next (default: a cube change can take 10 s), or every channel
    at each Z.
* **Keep the record** (`ZStackSet`): per-channel stacks, the planned and observed Z of every slice, status
  (completed, stopped, failed), the plan, timings.

Qt-free: tests run it against the simulator.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from openevos.acquisition.capture import CapturedImage, CaptureRejected, ZStack
from openevos.acquisition.controller import CommandError
from openevos.acquisition.scan_runner import RunControl, RunStopped
from openevos.core.models import CommandStatus
from openevos.instrument.profile import CameraRole
from openevos.safety.restrictions import run_guard

_log = logging.getLogger(__name__)
MAX_SLICES = 200
#: rough per-slice cost on top of the exposure (verified Z move, snap hand-off); simulator: ~0.2 s, used for the
#: estimate only
SLICE_OVERHEAD_S = 0.35
#: a light-cube change can move the F carriage (up to ~10 s on the unit, ADR-0051); a middle value for the estimate
CHANNEL_CHANGE_S = 3.0
DEFAULT_FRAME = (960, 1280)  # rows, columns when the camera profile has no size


class ChannelOrder(str, Enum):
    CHANNEL_FIRST = "channel_first"  # all slices of channel 1, then channel 2: fewest light-cube changes
    Z_FIRST = "z_first"  # every channel at each Z: fewest Z moves, channels best registered in time


class ZStackNeedsConfirmation(CaptureRejected):
    """The top of the stack is above the objective's safe height: ask the operator, then run with
    `confirmed_above_safe=True`."""


@dataclass(frozen=True)
class ZStackPlan:
    channels: tuple[str, ...]
    role: CameraRole
    bottom_um: float
    top_um: float
    step_um: float
    #: centre mode: slices are built outward from here, so this plane is always a slice
    centre_um: float | None = None
    order: ChannelOrder = ChannelOrder.CHANNEL_FIRST
    light_between_slices: bool = False  # light off while Z moves (slower; less light on the sample)
    settle_ms: float = 0.0
    approach_um: float = 2.0  # first slice of each sweep approached from this far below (backlash)
    return_to_start: bool = True
    confirmed_above_safe: bool = False
    #: True: the result becomes the Z-stack page's stack (time-lapse keeps its own)
    store: bool = True

    def describe(self) -> dict:
        return {"channels": list(self.channels), "camera": self.role.value, "bottom_um": self.bottom_um,
                "top_um": self.top_um, "step_um": self.step_um, "centre_um": self.centre_um,
                "order": self.order.value, "light_between_slices": self.light_between_slices,
                "settle_ms": self.settle_ms, "approach_um": self.approach_um,
                "return_to_start": self.return_to_start, "confirmed_above_safe": self.confirmed_above_safe}


def plan_positions(bottom_um: float, top_um: float, step_um: float, centre_um: float | None = None) -> list[float]:
    """Slice positions, bottom to top, all inside [bottom, top]. Whole steps only (the last step is never shorter)."""
    if step_um <= 0:
        raise CaptureRejected("Z step must be positive")
    lo, hi = min(bottom_um, top_um), max(bottom_um, top_um)
    eps = 1e-6 * max(1.0, step_um)
    if centre_um is None:
        n = math.floor((hi - lo) / step_um + eps) + 1
        return [round(lo + i * step_um, 6) for i in range(n)]
    c = min(max(centre_um, lo), hi)
    below = math.floor((c - lo) / step_um + eps)
    above = math.floor((hi - c) / step_um + eps)
    return [round(c + k * step_um, 6) for k in range(-below, above + 1)]


def optimal_step_um(numerical_aperture: float | None, emission_nm: int | None = None, immersion_index: float = 1.0,
                    ) -> float | None:
    """Axial Nyquist step for widefield imaging: lambda / (2 n (1 - cos a)), a = asin(NA / n).
    Transmitted light (no emission) uses 550 nm. None without an NA."""
    if not numerical_aperture or numerical_aperture <= 0 or numerical_aperture >= immersion_index:
        return None
    lam_um = (emission_nm or 550) / 1000
    a = math.asin(numerical_aperture / immersion_index)
    return lam_um / (2 * immersion_index * (1 - math.cos(a)))


@dataclass(frozen=True)
class Preflight:
    positions: tuple[float, ...]
    errors: tuple[str, ...] = ()
    confirm: str | None = None  # a question for the operator (safe height)
    notes: tuple[str, ...] = ()
    seconds: float = 0.0
    megabytes: float = 0.0
    light_seconds: float = 0.0
    images: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        n = len(self.positions)
        bits = [f"{n} slice{'s' if n != 1 else ''}"]
        if self.images != n:
            bits.append(f"{self.images} images")
        bits += [f"≈ {_fmt_s(self.seconds)}", f"{self.megabytes:,.0f} MB" if self.megabytes >= 10 else
                 f"{self.megabytes:.1f} MB", f"light on ≈ {_fmt_s(self.light_seconds)}"]
        return " · ".join(bits)

    def figures(self, channels: int = 1) -> dict[str, str]:
        """The four numbers the page shows above Acquire: slices (x channels), time, size, light on."""
        n = len(self.positions)
        mb = f"{self.megabytes:,.0f} MB" if self.megabytes >= 10 else f"{self.megabytes:.1f} MB"
        return {"slices": f"{n} × {channels}" if channels > 1 else f"{n}", "time": f"≈ {_fmt_s(self.seconds)}",
                "size": mb, "light": f"≈ {_fmt_s(self.light_seconds)}"}


def fmt_seconds(s: float) -> str:
    return _fmt_s(s)


def _fmt_s(s: float) -> str:
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min"


def preflight(capture, plan: ZStackPlan) -> Preflight:
    """Everything that can be known before the first move. Never moves anything."""
    errors: list[str] = []
    notes: list[str] = []
    try:
        pos = plan_positions(plan.bottom_um, plan.top_um, plan.step_um, plan.centre_um)
    except CaptureRejected as exc:
        return Preflight((), (str(exc),))
    if not plan.channels:
        errors.append("choose at least one channel")
    if len(pos) > MAX_SLICES:
        errors.append(f"{len(pos)} slices; the limit is {MAX_SLICES} (use a larger step or a smaller range)")
    hi = max(plan.bottom_um, plan.top_um)
    if pos and pos[-1] < hi - 1e-6 and plan.centre_um is None:
        notes.append(f"top slice at {pos[-1]:,.2f} µm: the range is not a whole number of steps "
                     f"({hi - pos[-1]:.2f} µm short of the top asked)")
    for slot in plan.channels:
        try:
            capture.check_capture(plan.role, slot)
        except CaptureRejected as exc:
            errors.append(str(exc))
    ctl = capture.ctl
    lim = ctl.axis_limits(capture.profile.focus_device, "z") if capture.profile.focus_device else None
    approach_low = (pos[0] - plan.approach_um) if pos and plan.approach_um > 0 else None
    if lim and pos:
        if pos[0] < lim[0] - 1e-6 or pos[-1] > lim[1] + 1e-6:
            errors.append(f"Z range {pos[0]:,.1f}–{pos[-1]:,.1f} µm is outside the focus limits "
                          f"{lim[0]:,.0f}–{lim[1]:,.0f} µm")
        elif approach_low is not None and approach_low < lim[0]:
            notes.append("the bottom is at the focus limit: the first slice is approached from above")
    safe = capture.safe_height_for() if getattr(capture, "safe_height_for", None) else None
    confirm = None
    if safe and pos and pos[-1] > safe[1] + 0.5:
        above = sum(1 for z in pos if z > safe[1] + 0.5)
        confirm = (f"The top of the stack ({pos[-1]:,.1f} µm) is above the {safe[0]} safe height ({safe[1]:,.1f} µm), "
                   f"where the objective nears the vessel: {above} slice{'s' if above != 1 else ''} above it. "
                   "Go higher than the safe height for this stack?")
    # estimate
    n_ch = max(1, len(plan.channels))
    exp_s = 0.0
    for slot in plan.channels:
        exp_s += capture.settings.channel(plan.role, slot).exposure_ms / 1000
    per_slice = SLICE_OVERHEAD_S * n_ch + exp_s + plan.settle_ms / 1000 * (1 if plan.order is ChannelOrder.Z_FIRST
                                                                          else n_ch)
    changes = (n_ch - 1) if plan.order is ChannelOrder.CHANNEL_FIRST else (n_ch - 1) * len(pos)
    seconds = len(pos) * per_slice + changes * CHANNEL_CHANGE_S + 1.0
    light_s = len(pos) * exp_s if plan.light_between_slices else seconds
    cam = capture.profile.camera(plan.role)
    rows, cols = (cam.height, cam.width) if cam and cam.height and cam.width else DEFAULT_FRAME
    bytes_per = rows * cols * (3 if plan.role is CameraRole.COLOR else 2)
    images = len(pos) * len(plan.channels)
    return Preflight(tuple(pos), tuple(errors), confirm, tuple(notes), seconds, images * bytes_per / 1e6,
                     light_s, images)


@dataclass(frozen=True)
class ZStackSet:
    """One Z-stack run: a stack per channel (same planned positions), its status and its plan."""

    stacks: tuple[ZStack, ...]
    planned_z_um: tuple[float, ...]
    status: str  # "completed" | "stopped" | "failed"
    message: str
    plan: ZStackPlan
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return self.status == "completed"

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(s.channel for s in self.stacks)

    def stack(self, slot: str | None = None) -> ZStack | None:
        if slot is None:
            return self.stacks[0] if self.stacks else None
        return next((s for s in self.stacks if s.channel == slot), None)

    @property
    def synthetic(self) -> bool:
        return any(i.synthetic for s in self.stacks for i in s.images)


def zstack_set_of(stacks: dict[str, ZStack] | tuple[ZStack, ...] | list[ZStack], role: CameraRole,
                  message: str = "") -> ZStackSet:
    """A ZStackSet around stacks a routine kept (Scan field, time-lapse position), so the Z-stack page can show and
    save them. Planned Z is the longest stack's; the status is "stopped" when any stack is incomplete."""
    sts = tuple(stacks.values()) if isinstance(stacks, dict) else tuple(stacks)
    if not sts:
        raise ValueError("no stacks")
    longest = max(sts, key=lambda s: s.planned or len(s.z_um))
    zs = tuple(longest.z_um)
    step = abs(zs[1] - zs[0]) if len(zs) > 1 else 1.0
    planned = tuple(round(zs[0] + i * step, 6) for i in range(max(longest.planned, len(zs)))) if zs else ()
    done = all(s.complete for s in sts)
    plan = ZStackPlan(channels=tuple(s.channel for s in sts), role=role, bottom_um=min(planned or zs),
                      top_um=max(planned or zs), step_um=step, store=False)
    started = min((i.captured_at for s in sts for i in s.images), default=datetime.now(timezone.utc))
    return ZStackSet(sts, planned, "completed" if done else "stopped", message, plan, started_at=started)


class ZStackRunner:
    """Runs one plan on a CaptureWorkflow. `control` is shared with Stop (CaptureWorkflow.stop_zstack)."""

    def __init__(self, capture, *, sleep: Callable[[float], None] | None = None,
                 control: RunControl | None = None) -> None:
        self.cap = capture
        self.ctl = capture.ctl
        self.control = control or RunControl()  # a time-lapse passes its own, so its Stop also stops the stack
        self._sleep = sleep

    def run(self, plan: ZStackPlan, progress: Callable[[int, int, str], None] | None = None, *,
            on_image: Callable[[str, int, int, CapturedImage], None] | None = None) -> ZStackSet:
        """`progress(done, total, text)` before each image; `on_image(slot, slice_index, done, image)` after each one
        (the page shows slices as they arrive). Neither may stop the stack: an error in `on_image` is logged.
        Inside the SafetyManager's run guard (ADR-0070)."""
        caps = self.cap.run_capabilities(plan.role) if hasattr(self.cap, "run_capabilities") else set()
        with run_guard(self.cap, "Z-stack", caps, self.control.stop):
            return self._run(plan, progress, on_image=on_image)

    def _run(self, plan: ZStackPlan, progress: Callable[[int, int, str], None] | None = None, *,
             on_image: Callable[[str, int, int, CapturedImage], None] | None = None) -> ZStackSet:
        pf = preflight(self.cap, plan)
        if pf.errors:
            raise CaptureRejected("; ".join(pf.errors))
        if pf.confirm and not plan.confirmed_above_safe:
            raise ZStackNeedsConfirmation(pf.confirm)
        safe = self.cap.safe_height_for() if getattr(self.cap, "safe_height_for", None) else None
        lim = self.ctl.axis_limits(self.cap.profile.focus_device, "z")
        pos = list(pf.positions)
        ctl, role = self.ctl, plan.role
        z_start = ctl.focus_z()
        light_before = ctl.light
        was_live = ctl.is_live(role)
        images: dict[str, list[CapturedImage]] = {s: [] for s in plan.channels}
        total = len(pos) * len(plan.channels)
        done = 0
        status, message = "completed", ""
        error: Exception | None = None
        t0 = time.monotonic()
        started_at = datetime.now(timezone.utc)  # before the first move (design review: v1 stamped the end)
        self.cap.zstack_control = self.control
        lit: dict[str, str | None] = {"slot": None}  # the channel whose light is on between snaps, if any

        def checkpoint() -> None:
            """Stop and Pause are honoured here, between moves and snaps. While paused the light is off (an
            indefinite pause must not keep illuminating the sample, ADR-0064 consequence) and comes back on resume."""
            if self.control.paused and not self.control.stopped:
                slot = lit["slot"]
                if slot is not None:
                    self.cap.prepare_channel(role, slot, light_on=False)
                if progress:
                    progress(done, total, f"Paused after {done} of {total} images; the light is off")
                self.control.checkpoint()  # waits for Resume, or raises RunStopped
                if slot is not None:
                    self.cap.prepare_channel(role, slot, light_on=True)
                if progress:
                    progress(done, total, "Resumed")
            self.control.checkpoint()

        def light_channel(slot: str) -> None:
            checkpoint()
            on = not plan.light_between_slices
            self.cap.prepare_channel(role, slot, light_on=on)
            lit["slot"] = slot if on else None

        def move(z: float) -> None:
            checkpoint()
            confirmed = bool(plan.confirmed_above_safe and safe and z > safe[1] + 0.5)
            ctl.run(ctl.move_focus(z, confirmed=confirmed))
            if plan.settle_ms > 0:
                if self._sleep is None:
                    self.control.sleep(plan.settle_ms / 1000)
                else:
                    self._sleep(plan.settle_ms / 1000)

        def approach(first: float) -> None:
            under = first - plan.approach_um if plan.approach_um > 0 else None
            here = ctl.focus_z()
            if under is not None and (lim is None or under >= lim[0]) and (here is None or here > under):
                checkpoint()
                ctl.run(ctl.move_focus(under))  # down: always allowed, away from the vessel

        def snap(slot: str, z_planned: float) -> None:
            nonlocal done
            checkpoint()
            if progress:
                progress(done, total, f"Slice {pos.index(z_planned) + 1} of {len(pos)}, {self._label(slot)}, "
                                      f"z {z_planned:,.2f} µm")
            if plan.light_between_slices:
                self.cap.prepare_channel(role, slot, light_on=True)
            frames = ctl.acquire(role, 1)
            if not frames:
                raise CaptureRejected("no frame returned")
            img = self.cap._to_captured(frames[0], slot, role)
            images[slot].append(img)
            if plan.light_between_slices:
                self.cap.prepare_channel(role, slot, light_on=False)
            done += 1
            if on_image:
                try:
                    on_image(slot, pos.index(z_planned), done, img)
                except Exception:  # noqa: BLE001 - a display problem must not stop the stack
                    _log.warning("Z-stack on_image callback failed", exc_info=True)

        try:
            if was_live:
                ctl.stop_live(role)  # once for the whole stack (ZF-03)
            if plan.order is ChannelOrder.CHANNEL_FIRST:
                for slot in plan.channels:
                    light_channel(slot)
                    approach(pos[0])
                    for z in pos:
                        move(z)
                        snap(slot, z)
            else:
                approach(pos[0])
                for z in pos:
                    move(z)
                    for slot in plan.channels:
                        light_channel(slot)
                        snap(slot, z)
            message = f"{done} images, {len(pos)} slices"
        except RunStopped:
            status, message = "stopped", f"stopped after {done} of {total} images"
        except CommandError as exc:
            if self.control.stopped or exc.status is CommandStatus.CANCELLED:
                status, message = "stopped", f"stopped after {done} of {total} images"
            else:
                status, message, error = "failed", f"{exc} (after {done} of {total} images)", exc
        except Exception as exc:  # noqa: BLE001 - reported with the partial result, then re-raised if nothing was kept
            status, message, error = "failed", f"{exc} (after {done} of {total} images)", exc
        finally:
            self._restore_light(light_before)
            if status != "stopped" and plan.return_to_start and z_start is not None:
                try:
                    ctl.run(ctl.move_focus(z_start))
                except Exception as exc:  # noqa: BLE001
                    message += f"; focus did not return to {z_start:,.2f} µm: {exc}"
            if was_live:
                try:
                    ctl.start_live(role)
                except Exception:  # noqa: BLE001 - live view is a convenience; the stack is already safe
                    _log.warning("live view did not restart after the Z-stack", exc_info=True)
            self.cap.zstack_control = None

        stacks = tuple(ZStack(slot, tuple(pos[:len(imgs)]), tuple(imgs), complete=status == "completed",
                              planned=len(pos), stop_um=pos[-1])
                       for slot, imgs in images.items() if imgs)
        result = ZStackSet(stacks, tuple(pos), status, message, plan, started_at=started_at,
                           seconds=time.monotonic() - t0)
        if stacks and plan.store:
            self.cap.buffers.set_zstack_set(result)
        if status == "failed" and not stacks and error is not None:
            raise error
        if progress:
            progress(total, total, message)
        return result

    def _label(self, slot: str) -> str:
        ch = self.cap.profile.channel(slot)
        return ch.display if ch else slot

    def _restore_light(self, before) -> None:
        """The light exactly as it was before the stack (ZF-01): on stays on, off goes off."""
        ctl = self.ctl
        try:
            now = ctl.light
            if before.channel is not None:
                if (now.channel, now.intensity_percent, now.enabled) != (before.channel, before.intensity_percent,
                                                                         before.enabled):
                    ctl.run(ctl.set_light(before.channel, before.intensity_percent, before.enabled))
            elif now.enabled and now.channel is not None:
                ctl.run(ctl.set_light(now.channel, now.intensity_percent, False))
        except Exception:  # noqa: BLE001 - never mask the stack's own result; the light state shows in the UI
            _log.warning("light not restored after the Z-stack", exc_info=True)


def plan_from_settings(zs, role: CameraRole, channels: tuple[str, ...], focus_um: float, *,
                       bottom_um: float | None = None, top_um: float | None = None,
                       confirmed_above_safe: bool = False, store: bool = True) -> ZStackPlan:
    """A plan from the saved Z-stack settings (`AppSettings.zstack`): centre mode around `focus_um`, or range mode
    from `bottom_um` to `top_um` (both required in range mode)."""
    common = {"channels": tuple(channels), "role": role, "step_um": zs.step_um, "order": ChannelOrder(zs.order),
              "light_between_slices": zs.light_between_slices, "settle_ms": zs.settle_ms,
              "approach_um": zs.approach_um, "confirmed_above_safe": confirmed_above_safe, "store": store}
    if zs.mode == "range":
        if bottom_um is None or top_um is None:
            raise CaptureRejected("set the top and the bottom first")
        return ZStackPlan(bottom_um=min(bottom_um, top_um), top_um=max(bottom_um, top_um), **common)
    return ZStackPlan(bottom_um=focus_um - zs.below_um, top_um=focus_um + zs.above_um, centre_um=focus_um, **common)
