"""InstrumentController: the one place workflows and the UI turn intent into typed commands.

* Every state change goes through `core.router.submit()` (validation, reservation, readback).
* `run()` blocks (worker threads only — never the UI thread) and raises on any non-SUCCEEDED end.
* `acquire()` takes frames through the scientific path: it reserves the camera exclusively and
  holds stage, focus, turret and illumination *shared* for the exposure, so the cube carriage,
  turret and stage cannot move mid-frame (the F-carriage interlock from the bring-up record).
"""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass

from openevos.core.application import CoreServices
from openevos.core.event_bus import CommandLifecycleEvent, DeviceStateChanged
from openevos.core.frame_transport import Frame, MemorySink
from openevos.core.models import (
    BackendKind,
    CommandOrigin,
    CommandStatus,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    SetCameraExposure,
    SetCameraGain,
    SetIllumination,
    SetTurretPosition,
)
from openevos.instrument.profile import CameraRole, InstrumentProfile
from openevos.safety.interlocks import claim_for_acquisition

#: turret command deadline: 4 steps x 10 s (telemetry TURRET_STEP_S_MAX) + the service's 5 s + Z retract margin
TURRET_TIMEOUT_S = 60.0


class CommandError(RuntimeError):
    def __init__(self, kind: str, status: CommandStatus, message: str | None):
        super().__init__(f"{kind} {status.value}: {message or 'no detail'}")
        self.status, self.detail = status, message


class AcquisitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LightState:
    channel: str | None
    intensity_percent: float
    enabled: bool

    @property
    def lit(self) -> tuple[str | None, float] | None:
        """What the sample actually gets: None when dark (off, or on at 0 %)."""
        return (self.channel, self.intensity_percent) if self.enabled and self.intensity_percent > 0 else None


class InstrumentController:
    def __init__(self, core: CoreServices, profile: InstrumentProfile, *, origin: CommandOrigin = CommandOrigin.UI):
        self.core, self.profile, self.origin = core, profile, origin
        self._light = LightState(None, 0.0, False)
        self._camera_values: dict[CameraRole, tuple[float | None, float | None]] = {}
        self._acq_lock = threading.Lock()
        self._pending_light: dict[str, LightState] = {}
        self._resume_after: dict[str, CameraRole] = {}
        self._live_role: CameraRole | None = None
        core.bus.subscribe(CommandLifecycleEvent, self._on_command)
        core.bus.subscribe(DeviceStateChanged, self._on_device)

    def _on_device(self, e: DeviceStateChanged) -> None:
        """The LED went dark without a command through this controller (operator STOP halts the device
        directly). Only ever downgrades on -> off: switching on still needs a verified command."""
        if e.device_id != self.profile.illumination_device or e.current is None:
            return
        if e.current.attr("led_enabled") is False and self._light.enabled:
            was_lit = self._light.lit is not None
            self._light = LightState(self._light.channel, self._light.intensity_percent, False)
            if was_lit:
                self._invalidate_frames("light switched off by the device")

    def _on_command(self, e: CommandLifecycleEvent) -> None:
        if e.status.is_terminal:
            state = self._pending_light.pop(e.command_id, None)
            if state is not None and e.status is CommandStatus.SUCCEEDED:
                changed = state.lit != self._light.lit
                self._light = state
                if changed:
                    self._invalidate_frames("light changed")
            role = self._resume_after.pop(e.command_id, None)
            if role is not None and self._live_role is role:
                self._resume_live(role)

    def _invalidate_frames(self, reason: str) -> None:
        """A frame in flight when the light changed was exposed partly before the change (colour camera,
        2026-11-15, ADR-0039). Cameras that pipeline frames drop their next one. This runs synchronously,
        before the light command's waiter returns, so a snap that follows never gets the stale frame."""
        for role in CameraRole:
            if not self.profile.camera(role):
                continue
            try:
                fn = getattr(self.camera_backend(role), "invalidate_frames", None)
            except AcquisitionError:
                continue
            if fn is not None:
                fn(reason)

    def _resume_live(self, role: CameraRole) -> None:
        cam = self.camera_backend(role)
        if not cam.streaming:
            try:
                cam.start_stream()
            except RuntimeError:
                pass  # an acquisition owns the camera right now; it restores live itself

    def _camera_command(self, role: CameraRole, cmd):
        """Camera settings need an idle camera (fail-closed safety). If live view is running it is
        paused for the command and resumed once the command has finished."""
        cam = self.camera_backend(role)
        # only ever pause the *live preview*; a scientific acquisition holds the camera reservation, so the
        # command is rejected by the router instead of cutting the exposure short
        if cam.streaming and getattr(cam, "current_acquisition", None) is None and self._live_role is role:
            cam.stop_stream("paused for camera setting")
            self._resume_after[cmd.command_id] = role
        return self.core.router.submit(cmd)

    # ------------------------------------------------------------------ device lookup
    def _caps(self, device_id: str):
        rec = self.core.devices.get(device_id)
        return rec.capabilities if rec else None

    def camera_backend(self, role: CameraRole):
        cam = self.profile.camera(role)
        rec = self.core.devices.get(cam.device_id) if cam else None
        if rec is None:
            raise AcquisitionError(f"no {role.value} camera registered")
        return rec.backend

    def snapshot(self, device_id: str):
        return self.core.store.get(device_id)

    # ------------------------------------------------------------------ observed state
    def stage_xy(self) -> tuple[float, float] | None:
        s = self.snapshot(self.profile.stage_device)
        p = s.observed_position if s else None
        return (p.get("x"), p.get("y")) if p else None

    def focus_z(self) -> float | None:
        s = self.snapshot(self.profile.focus_device)
        p = s.observed_position if s else None
        return p.get("z") if p else None

    def objective_slot(self) -> int | None:
        if not self.profile.turret_device:
            return None
        s = self.snapshot(self.profile.turret_device)
        p = s.observed_position if s else None
        return int(p.values[0]) if p else None

    @property
    def light(self) -> LightState:
        """Last *verified* light state commanded through this controller."""
        return self._light

    def axis_limits(self, device_id: str, axis: str) -> tuple[float, float] | None:
        caps = self._caps(device_id)
        ax = caps.axis(axis) if caps else None
        return (ax.limits.minimum, ax.limits.maximum) if ax and ax.limits else None

    def stage_frame(self) -> tuple[str, str]:
        caps = self._caps(self.profile.stage_device)
        ax = caps.axis("x") if caps else None
        return (ax.limits.unit if ax and ax.limits else "um", ax.coordinate_frame if ax else "")

    def focus_frame(self) -> tuple[str, str]:
        caps = self._caps(self.profile.focus_device)
        ax = caps.axis("z") if caps else None
        return (ax.limits.unit if ax and ax.limits else "um", ax.coordinate_frame if ax else "")

    # ------------------------------------------------------------------ commands
    def move_stage(self, x: float, y: float, timeout_s: float = 30.0):
        unit, frame = self.stage_frame()
        return self.core.router.submit(MoveStageAbsolute(device_id=self.profile.stage_device, origin=self.origin,
                                                         x=x, y=y, unit=unit, frame=frame, timeout_s=timeout_s))

    def move_stage_by(self, dx: float, dy: float):
        cur = self.stage_xy()
        if cur is None:
            raise CommandError("stage.move_relative", CommandStatus.REJECTED, "stage position unknown")
        lim_x = self.axis_limits(self.profile.stage_device, "x")
        lim_y = self.axis_limits(self.profile.stage_device, "y")
        x, y = cur[0] + dx, cur[1] + dy
        caps = self._caps(self.profile.stage_device)
        if caps is not None and (caps.backend_kind is BackendKind.PHYSICAL
                                 or self.profile.stage_device.startswith("motor.")):
            # a physical stage never has a relative move silently shortened: refuse instead (ADR-0024). The Technosoft
            # drive emulator (motor.*, labelled simulated since audit APP-02) behaves exactly like the drives
            for name, v, lim in (("x", x, lim_x), ("y", y, lim_y)):
                if lim is None or not lim[0] <= v <= lim[1]:
                    raise CommandError("stage.move_relative", CommandStatus.REJECTED,
                                       f"axis {name}: target {v:.1f} um outside the permitted "
                                       f"{'range' if lim is None else f'[{lim[0]:g}, {lim[1]:g}]'} um")
            return self.move_stage(x, y)
        if lim_x:
            x = min(max(x, lim_x[0]), lim_x[1])
        if lim_y:
            y = min(max(y, lim_y[0]), lim_y[1])
        return self.move_stage(x, y)

    def move_focus(self, z: float, timeout_s: float = 30.0, *, confirmed: bool = False):
        """`confirmed`: the operator explicitly approved going above the objective's safe height (APP-03)."""
        unit, frame = self.focus_frame()
        return self.core.router.submit(MoveFocusAbsolute(device_id=self.profile.focus_device, origin=self.origin,
                                                         z=z, unit=unit, frame=frame, timeout_s=timeout_s,
                                                         confirmed=confirmed))

    #: set by the session when the turret does not lower Z itself (audit APP-01): every objective change through this
    #: controller lowers Z to this height first, through the router, so the turret interlock is met on every path
    #: (UI, scans, time-lapse, calibration)
    turret_safe_z_um: float | None = None

    def set_objective(self, slot: int):
        if not self.profile.turret_device:
            raise CommandError("turret.set_position", CommandStatus.REJECTED, "no turret device")
        safe, z = self.turret_safe_z_um, self.focus_z()
        retract = safe is not None and z is not None and z > safe and self.objective_slot() != slot
        if retract:
            self.move_focus(safe).wait(30)  # the interlock then decides from the observed Z
        # the longest turn is 4 Geneva steps (positions decrease: 1->5->4->3->2) of up to 10 s each, plus Z's retract;
        # the drive service's own deadline is steps x 10 s + 5 s. 15 s timed a real 1->4 turn out mid-swing
        # (2026-11-18 18:33): the turret finished, but the command ended "state uncertain"
        ticket = self.core.router.submit(SetTurretPosition(device_id=self.profile.turret_device, origin=self.origin,
                                                           position=slot, timeout_s=TURRET_TIMEOUT_S))
        if retract and ticket.wait(30) is CommandStatus.SUCCEEDED:
            # Z back where it was before the turn, so every caller (calibration, scans, time-lapse, review) continues
            # at its focus height; only a turret that does not lower Z itself takes this path (the simulator)
            self.move_focus(z).wait(30)
        return ticket

    def set_light(self, channel: str, intensity_percent: float, enabled: bool):
        if not self.profile.illumination_device:
            raise CommandError("illumination.set", CommandStatus.REJECTED, "no illumination device")
        cmd = SetIllumination(device_id=self.profile.illumination_device, origin=self.origin, channel=channel,
                              intensity_percent=intensity_percent, enabled=enabled,
                              # a cube channel may first move the F carriage (ADR-0051): slot 1 -> mirror took 10.4 s
                              # on the unit (Gate 12), and F's own deadline is <= 31 s
                              timeout_s=45.0)
        self._pending_light[cmd.command_id] = LightState(channel, intensity_percent, enabled)
        return self.core.router.submit(cmd)

    def set_exposure(self, role: CameraRole, exposure_ms: float):
        cam = self.profile.camera(role)
        # any direct setting (Capture page, Actual mode) makes apply_camera's cache stale: forget it, so the
        # next capture re-sends its values instead of trusting a value the camera no longer has
        self._camera_values.pop(role, None)
        return self._camera_command(role, SetCameraExposure(device_id=cam.device_id, origin=self.origin,
                                                            exposure_ms=exposure_ms, timeout_s=5.0))

    def set_gain(self, role: CameraRole, gain: float):
        cam = self.profile.camera(role)
        if not gain >= 1.0:
            raise CommandError("camera.set_gain", CommandStatus.REJECTED, f"gain factor {gain} is below 1x")
        self._camera_values.pop(role, None)  # see set_exposure
        # the UI speaks a linear factor (EVOS "gain 1x"); the core carries dB
        return self._camera_command(role, SetCameraGain(device_id=cam.device_id, origin=self.origin,
                                                        gain_db=20.0 * math.log10(gain),
                                                        timeout_s=5.0))

    @staticmethod
    def run(ticket, timeout: float = 60.0) -> None:
        status = ticket.wait(timeout)
        if status is not CommandStatus.SUCCEEDED:
            rec = ticket.record
            raise CommandError(rec.command.kind, status, rec.message)

    def apply_camera(self, role: CameraRole, exposure_ms: float, gain: float) -> None:
        """Set exposure and gain (skips commands whose verified value already matches)."""
        last = self._camera_values.get(role, (None, None))
        if last[0] != exposure_ms:
            self.run(self.set_exposure(role, exposure_ms))
        if last[1] != gain:
            self.run(self.set_gain(role, gain))
        self._camera_values[role] = (exposure_ms, gain)

    # ------------------------------------------------------------------ live view
    def start_live(self, role: CameraRole) -> None:
        self._live_role = role
        for r in CameraRole:
            if r is not role and self.profile.camera(r):
                self.stop_live(r)
        cam = self.camera_backend(role)
        if not cam.streaming:
            cam.start_stream()

    def stop_live(self, role: CameraRole) -> None:
        if self._live_role is role:
            self._live_role = None
        if self.profile.camera(role):
            cam = self.camera_backend(role)
            if cam.streaming:
                cam.stop_stream("live view stopped")

    def is_live(self, role: CameraRole) -> bool:
        return bool(self.profile.camera(role)) and self.camera_backend(role).streaming

    # ------------------------------------------------------------------ scientific acquisition
    def acquire(self, role: CameraRole, n_frames: int = 1, timeout: float = 30.0) -> list[Frame]:
        cam_profile = self.profile.camera(role)
        if cam_profile is None:
            raise AcquisitionError(f"no {role.value} camera in the profile")
        cam = self.camera_backend(role)
        stable = [d for d in (self.profile.stage_device, self.profile.focus_device, self.profile.turret_device,
                              self.profile.illumination_device) if d]
        with self._acq_lock:
            acq_id = f"acq-{uuid.uuid4().hex[:10]}"
            conflicts = self.core.router.reserve(acq_id, claim_for_acquisition(cam_profile.device_id, stable))
            if conflicts:
                raise AcquisitionError("cannot acquire while devices are busy: " + "; ".join(conflicts))
            was_live = cam.streaming
            sink = MemorySink()
            try:
                if was_live:
                    cam.stop_stream("paused for acquisition")
                done = threading.Event()
                self.core.transport.open_stream(acq_id, sink, expected_frames=n_frames)
                try:
                    cam.start_stream(acquisition_id=acq_id, n_frames=n_frames, on_finished=lambda _r: done.set())
                    if not done.wait(timeout):
                        cam.stop_stream("acquisition timeout")
                        raise AcquisitionError(f"acquisition {acq_id} timed out")
                finally:
                    summary = self.core.transport.close_stream(acq_id)
                if not summary.complete:
                    raise AcquisitionError(f"acquisition {acq_id} incomplete: {summary.state.value} "
                                           f"written={summary.written}/{n_frames} {summary.message or ''}")
            finally:
                self.core.router.release(acq_id)
                if was_live and not cam.streaming:
                    try:
                        cam.start_stream()
                    except RuntimeError:
                        pass
        return list(sink.frames)

