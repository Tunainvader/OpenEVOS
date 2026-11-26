"""Image > Capture workflow: channel capture, Capture All, Transfection, Z-stack, Snap to Last,
Capture Focus Nominal, memory buffers.

Manual basis: Capture tab controls p.7-9; memory buffers shown as thumbnails under the channel
buttons and cleared with Clear All Channels p.8-9; Snap to Last reverts focus to the last
captured image p.9; hot-button actions p.21; the colour camera is brightfield/phase only p.9.

Every capture is a scientific acquisition (InstrumentController.acquire) after the light,
exposure and gain commands have been *verified*. Captured images keep their raw data and a
metadata record (synthetic flag, device states at exposure time) for saving.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from openevos.acquisition.controller import AcquisitionError, InstrumentController
from openevos.core.frame_transport import Frame
from openevos.core.models import utcnow
from openevos.instrument.profile import CameraRole, ChannelKind, InstrumentProfile
from openevos.instrument.settings import AppSettings
from openevos.safety.restrictions import DATA_SAVING, FOCUS_Z, MULTI_POSITION, imaging, registry_of


class CaptureRejected(RuntimeError):
    """The capture was refused before anything moved (interlock, configuration)."""


@dataclass(frozen=True)
class CapturedImage:
    data: np.ndarray  # read-only raw frame (uint16 mono or uint8 RGB)
    channel: str  # slot
    channel_label: str
    camera: CameraRole
    objective_slot: int | None
    magnification: float | None
    pixel_size_um: float | None  # at the sample; None if unknown
    stage_xy_um: tuple[float, float] | None
    z_um: float | None
    exposure_ms: float
    gain: float
    led_percent: float
    synthetic: bool
    frame_device: str
    frame_index: int
    bit_depth: int
    captured_at: datetime = field(default_factory=utcnow)
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def pixel_size_calibrated(self) -> bool:
        """True only when the pixel size was measured on this instrument (Calibration › pixel size); a nominal value
        (camera pixel / objective magnification) never drives a scale bar (operator, 2026-11-18)."""
        return self.pixel_size_um is not None and dict(self.extra).get("pixel_size_source") == "calibrated"

    @property
    def is_color(self) -> bool:
        return self.data.ndim == 3

    @property
    def max_value(self) -> int:
        return 255 if self.is_color else (1 << self.bit_depth) - 1

    def metadata(self) -> dict:
        return {
            "channel": self.channel, "channel_label": self.channel_label, "camera": self.camera.value,
            "objective_slot": self.objective_slot, "magnification": self.magnification,
            "pixel_size_um": self.pixel_size_um, "stage_xy_um": self.stage_xy_um, "z_um": self.z_um,
            "exposure_ms": self.exposure_ms, "gain": self.gain, "led_percent": self.led_percent,
            "synthetic": self.synthetic, "frame_device": self.frame_device, "frame_index": self.frame_index,
            "bit_depth": self.bit_depth, "captured_at": self.captured_at.isoformat(),
            "provenance": "SIMULATED synthetic frame (openevos optics simulator)" if self.synthetic
            else "camera frame", "extra": dict(self.extra),
        }


#: most slices one Z-stack may take (operator rule: the Z-stack is capped at 200 slices)
MAX_ZSTACK_SLICES = 200


def zstack_slices(range_um: float, step_um: float) -> int:
    """How many slices a stack over `range_um` in steps of `step_um` takes (never past the range)."""
    return int(abs(range_um) / step_um + 1e-6) + 1 if step_um > 0 else 0


def zstack_slices_centred(range_um: float, step_um: float) -> int:
    """Slices in a stack centred on the focus over `range_um` (the routines' stacks): whole steps out from the centre
    on each side, so the focus plane is a slice (acquisition.zstack.plan_positions, centre mode)."""
    return 2 * int(abs(range_um) / 2 / step_um + 1e-6) + 1 if step_um > 0 else 0


def plan_zstack(start_um: float, stop_um: float, step_um: float) -> list[float]:
    """Slice heights from `start_um` towards `stop_um` in steps of `step_um`, in acquisition order. Upward when stop is
    above start, downward when below. The last slice never passes `stop_um` (a range that is not a whole number of steps
    ends short of stop rather than beyond it, where the focus limit may be)."""
    if step_um <= 0:
        raise CaptureRejected("Z step must be positive")
    n = zstack_slices(stop_um - start_um, step_um)
    if n > MAX_ZSTACK_SLICES:
        raise CaptureRejected(f"{n} slices requested; limit is {MAX_ZSTACK_SLICES}")
    sign = 1 if stop_um >= start_um else -1
    return [start_um + sign * i * step_um for i in range(n)]


@dataclass(frozen=True)
class ZStack:
    channel: str
    z_um: tuple[float, ...]  # planned positions of the slices kept (each image also records its observed Z)
    images: tuple[CapturedImage, ...]
    complete: bool = True  # False: stopped or failed part-way (ADR-0064)
    planned: int = 0  # slices planned (0 = as many as were taken)
    stop_um: float | None = None  # the planned last slice (the engine always goes bottom to top)

    @property
    def direction(self) -> int:
        """+1 when the stack went up in Z (start below stop), -1 when it went down."""
        end = self.stop_um if self.stop_um is not None else (self.z_um[-1] if self.z_um else 0.0)
        return -1 if self.z_um and end < self.z_um[0] else 1


class ImageBuffers:
    """Per-channel memory buffers (latest capture per channel slot), thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._b: dict[str, CapturedImage] = {}
        self.zstack: ZStack | None = None
        self.zstack_set = None  # acquisition.zstack.ZStackSet: every channel of the last Z-stack run (ADR-0064)
        self.listeners: list[Callable[[], None]] = []

    def put(self, img: CapturedImage) -> None:
        with self._lock:
            self._b[img.channel] = img
        self._notify()

    def get(self, slot: str) -> CapturedImage | None:
        with self._lock:
            return self._b.get(slot)

    def all(self) -> dict[str, CapturedImage]:
        with self._lock:
            return dict(self._b)

    def clear(self) -> None:
        with self._lock:
            self._b.clear()
            self.zstack = None
            self.zstack_set = None
        self._notify()

    def remove(self, slot: str) -> None:
        """Clear Channel (manual p.11): drop one channel's memory buffer."""
        with self._lock:
            self._b.pop(slot, None)
        self._notify()

    def set_zstack(self, z: ZStack) -> None:
        with self._lock:
            self.zstack = z
            self.zstack_set = None
        self._notify()

    def set_zstack_set(self, zs) -> None:
        """A whole Z-stack run; `zstack` stays its first channel for older readers (Movies, exports)."""
        with self._lock:
            self.zstack_set = zs
            self.zstack = zs.stacks[0] if zs.stacks else None
        self._notify()

    def _notify(self) -> None:
        for fn in list(self.listeners):
            fn()


class CaptureService:
    def __init__(self, ctl: InstrumentController, profile: InstrumentProfile, settings: AppSettings):
        self.ctl, self.profile, self.settings = ctl, profile, settings
        self.buffers = ImageBuffers()
        # hooks set by Session from the calibration store
        self.defects_for: Callable[[CameraRole], list[tuple[int, int]]] = lambda _r: []
        self.pixel_size_for: Callable[[CameraRole, int | None], float | None] | None = None
        self.last_capture_z: float | None = None
        #: Capture-tab POWER toggle (live view); starts dark (a real LED never lights at launch); captures always light
        #: the sample
        self.light_power = False
        #: (objective name, safe height in µm) for the objective in the light path, or None (set by the session, the
        #: same source as the FocusSafeHeight interlock): Z-stacks check it before moving (ADR-0064)
        self.safe_height_for: Callable[[], tuple[str, float] | None] = lambda: None
        #: the RunControl of the Z-stack in progress, for Stop (None when no stack runs)
        self.zstack_control = None

    def pause_zstack(self, on: bool = True) -> bool:
        """Pause (or resume) the running Z-stack at the next slice boundary; the light is off while paused. True if
        one was running."""
        c = self.zstack_control
        if c is None or c.stopped:
            return False
        if on:
            c.pause()
        else:
            c.resume()
        return True

    def stop_zstack(self) -> bool:
        """Stop the running Z-stack after the current move or snap; its slices are kept. True if one was running."""
        c = self.zstack_control
        if c is None:
            return False
        c.stop()
        return True

    # ------------------------------------------------------------------ checks
    def check_capture(self, role: CameraRole, slot: str) -> None:
        cam = self.profile.camera(role)
        ch = self.profile.channel(slot)
        if cam is None:
            raise CaptureRejected(f"no {role.value} camera configured")
        if ch is None or not ch.installed:
            raise CaptureRejected(f"channel {slot} is not installed")
        if not cam.supports(ch):
            raise CaptureRejected(f"the {cam.display_name.lower()} cannot image {ch.display} "
                                  "(colour camera: brightfield/phase only)")
        slot_now = self.ctl.objective_slot()
        obj = self.profile.objective(slot_now) if slot_now else None
        if obj is None or not obj.installed:
            raise CaptureRejected("no objective in the light path")
        reg = registry_of(self.ctl)
        why = reg.capture_refusal(cam.device_id) if reg is not None else []
        if why:  # a restriction the operator confirmed from Health (ADR-0070)
            raise CaptureRejected(f"{cam.display_name} capture refused: " + "; ".join(why))

    def run_capabilities(self, role: CameraRole | None, *, positions: int = 1, saving: bool = True,
                         focus: bool = True) -> set[str]:
        """What a run needs, for the SafetyManager's run guard (ADR-0070)."""
        caps: set[str] = set()
        cam = self.profile.camera(role or self.settings.active_camera)
        if cam is not None:
            caps.add(imaging(cam.device_id))
        if positions > 1:
            caps.add(MULTI_POSITION)
        if saving:
            caps.add(DATA_SAVING)
        if focus:
            caps.add(FOCUS_Z)
        return caps

    def capture_all_channels(self) -> list[str]:
        sel = [s for s in self.settings.mono.capture_all if (c := self.profile.channel(s)) and c.installed]
        if sel:
            return sel
        return [c.slot for c in self.profile.channels if c.installed and c.kind is ChannelKind.FLUORESCENCE]

    # ------------------------------------------------------------------ primitives
    def prepare_channel(self, role: CameraRole, slot: str, *, light_on: bool = True) -> None:
        cs = self.settings.channel(role, slot)
        self.ctl.run(self.ctl.set_light(slot, cs.led_percent, light_on))
        self.ctl.apply_camera(role, cs.exposure_ms, cs.gain)

    def capture(self, slot: str, role: CameraRole | None = None, *, store: bool = True) -> CapturedImage:
        role = role or self.settings.active_camera
        self.check_capture(role, slot)
        prev = self.ctl.light
        self.prepare_channel(role, slot, light_on=True)
        frames = self.ctl.acquire(role, 1)
        if not frames:
            raise AcquisitionError("no frame returned")
        img = self._to_captured(frames[0], slot, role)
        if store:
            self.buffers.put(img)
        self.last_capture_z = img.z_um
        if not self.light_power:  # restore a dark live view (also when no light had been set yet this session)
            pct = prev.intensity_percent if prev.channel is not None else self.settings.channel(role, slot).led_percent
            self.ctl.run(self.ctl.set_light(slot, pct, False))
        return img

    def capture_all(self, role: CameraRole | None = None,
                    progress: Callable[[int, int, str], None] | None = None) -> list[CapturedImage]:
        role = role or self.settings.active_camera
        if role is not CameraRole.MONO:
            raise CaptureRejected("Capture All uses the installed light cubes (monochrome camera)")
        slots = self.capture_all_channels()
        if not slots:
            raise CaptureRejected("no fluorescence channels selected for Capture All")
        out = []
        for i, s in enumerate(slots):
            if progress:
                progress(i, len(slots), s)
            out.append(self.capture(s, role))
        return out

    def transfection(self, fluor_slot: str, trans_slot: str = "trans") -> tuple[CapturedImage, CapturedImage]:
        f = self.profile.channel(fluor_slot)
        t = self.profile.channel(trans_slot)
        if not f or f.kind is not ChannelKind.FLUORESCENCE:
            raise CaptureRejected("transfection needs one fluorescence channel")
        if not t or t.kind is ChannelKind.FLUORESCENCE:
            raise CaptureRejected("transfection needs one transmitted-light channel")
        return self.capture(fluor_slot, CameraRole.MONO), self.capture(trans_slot, CameraRole.MONO)

    def zstack(self, slot: str, start_um: float, stop_um: float, step_um: float,
               role: CameraRole | None = None, progress: Callable[[int, int, str], None] | None = None, *,
               store: bool = True, confirmed_above_safe: bool = False) -> ZStack:
        """One channel from start to stop (either order; acquired bottom to top) through the Z-stack engine
        (ADR-0064). Raises when nothing could be taken; a Stop returns the slices taken (`complete=False`)."""
        from openevos.acquisition.zstack import ZStackPlan, ZStackRunner

        plan = ZStackPlan(channels=(slot,), role=role or self.settings.active_camera, bottom_um=min(start_um, stop_um),
                          top_um=max(start_um, stop_um), step_um=step_um, confirmed_above_safe=confirmed_above_safe,
                          store=store)
        res = ZStackRunner(self).run(plan, progress)
        if res.status == "failed":
            raise CaptureRejected(res.message)
        st = res.stack(slot)
        if st is None:
            raise CaptureRejected("stopped before the first slice" if res.status == "stopped" else res.message)
        return st

    def run_zstack(self, plan, progress: Callable[[int, int, str], None] | None = None, *, control=None,
                   on_image=None):
        """Run a ZStackPlan (several channels, centre or range mode); returns the ZStackSet, partial when stopped.
        `control`: a RunControl to share (a time-lapse's), so its Stop also stops the stack. `on_image(slot, index,
        done, image)` after each image (the Z-stack page shows slices as they arrive)."""
        from openevos.acquisition.zstack import ZStackRunner

        return ZStackRunner(self, control=control).run(plan, progress, on_image=on_image)

    def snap_to_last(self):
        if self.last_capture_z is None:
            raise CaptureRejected("no captured image yet")
        return self.ctl.move_focus(self.last_capture_z)

    # ------------------------------------------------------------------ helpers
    def _to_captured(self, frame: Frame, slot: str, role: CameraRole) -> CapturedImage:
        md = frame.metadata
        extra = dict(md.extra)
        ch = self.profile.channel(slot)
        cam = self.profile.camera(role)
        obj_slot = int(extra["objective_slot"]) if "objective_slot" in extra else self.ctl.objective_slot()
        obj = self.profile.objective(obj_slot) if obj_slot else None
        mag = obj.magnification if obj else None
        px = cam.pixel_size_um / mag if cam and cam.pixel_size_um and mag else None
        px_source = "nominal (camera pixel / magnification)"
        if self.pixel_size_for is not None:
            measured = self.pixel_size_for(role, obj_slot)
            if measured:
                px, px_source = measured, "calibrated"
        xy = (float(extra["x_um"]), float(extra["y_um"])) if "x_um" in extra else self.ctl.stage_xy()
        z = float(extra["z_um"]) if "z_um" in extra else self.ctl.focus_z()
        if md.channel is not None and md.channel != slot:
            raise AcquisitionError(f"frame was exposed on {md.channel}, expected {slot}")
        data = frame.data
        defects = self.defects_for(role)  # mono and colour (channel by channel), audit APP-08
        extra_md = (*md.extra, ("pixel_size_source", px_source))
        if defects:
            from openevos.acquisition.calibration import correct_hot_pixels

            data = correct_hot_pixels(data, defects)
            data.flags.writeable = False
            extra_md = (*extra_md, ("hot_pixel_correction", f"{len(defects)} pixels replaced by 3x3 median"))
        return CapturedImage(
            data=data, channel=slot, channel_label=ch.display if ch else slot, camera=role,
            objective_slot=obj_slot, magnification=mag, pixel_size_um=px, stage_xy_um=xy, z_um=z,
            exposure_ms=md.exposure_ms or 0.0, gain=float(extra.get("gain", 1.0)),
            led_percent=float(extra.get("led_percent", self.ctl.light.intensity_percent)),
            synthetic=md.synthetic, frame_device=md.device_id, frame_index=md.frame_index,
            bit_depth=8 if frame.data.ndim == 3 else (cam.native_bit_depth if cam and cam.native_bit_depth else 16),
            extra=extra_md)
