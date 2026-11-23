"""MicroscopeController: the operations the UI performs, expressed as named commands.

`self.gw` is the `CoreGateway` (ADR-0010): each `execute()` becomes a typed command that the
command core validates, reserves and verifies. The UI calls this service and the read-only
device views; it never touches a device or a backend. Quick commands (jog, exposure, intensity) run on the caller's thread and return at once.
Sequences that must wait for motion (channel change, objective change, snap, acquisition) run on
one worker thread, so two sequences never interleave, and they return a Future.

STOP (`stop()`) goes straight to `CoreGateway.stop_all()` → `CommandRouter.stop_all()`; it does
not queue behind the worker and does not depend on anything remote.
"""  # noqa: E501

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from openevos.core.acquisition import AcquiredFrame, AcquisitionEngine, AcquisitionPlan, Progress
from openevos.core.channels import FLUORESCENCE, ChannelPreset, ChannelSettings, build_presets
from openevos.core.command_language import Actor, CommandRejected
from openevos.core.core_gateway import CoreGateway, Issuer, StopReport
from openevos.hal.types import Frame, IncubatorTelemetry, ObjectiveInfo, Role

INCUBATOR_SETPOINT_COMMANDS = {
    "temperature": ("incubator.set_temperature", "temperature_c"),
    "co2": ("incubator.set_co2", "co2_pct"),
    "o2": ("incubator.set_o2", "o2_pct"),
}

Listener = Callable[[str, Any], None]
log = logging.getLogger(__name__)


class MicroscopeController:
    def __init__(self, gateway: CoreGateway):
        self.gw = gateway
        self.presets: list[ChannelPreset] = build_presets(gateway)
        self._settings = {p.name: ChannelSettings(p.default_exposure_ms, p.default_intensity_pct,
                                                  p.default_gain_db) for p in self.presets}
        self._active: ChannelPreset | None = None
        self._light_on = True
        self._live_wanted = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openevos-seq")
        self._task: str | None = None
        self._task_lock = threading.Lock()
        self._listeners: list[Listener] = []
        self.last_snap: Frame | None = None
        self._engine: AcquisitionEngine | None = None

    # ------------------------------------------------------------------------------ events
    def subscribe(self, cb: Listener) -> None:
        self._listeners.append(cb)

    def _emit(self, kind: str, payload: Any = None) -> None:
        for cb in list(self._listeners):
            try:
                cb(kind, payload)
            except Exception:
                log.exception("controller listener failed")

    def submit(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        def run() -> Any:
            with self._task_lock:
                self._task = name
            self._emit("task", name)
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                self._emit("error", f"{name}: {exc}")
                raise
            finally:
                with self._task_lock:
                    self._task = None
                self._emit("task", None)
        return self._worker.submit(run)

    @property
    def current_task(self) -> str | None:
        return self._task

    # ------------------------------------------------------------------------------ reads
    def preset(self, name: str) -> ChannelPreset:
        for p in self.presets:
            if p.name == name:
                return p
        raise KeyError(name)

    def settings(self, name: str | None = None) -> ChannelSettings:
        name = name or (self._active.name if self._active else self.presets[0].name)
        return self._settings[name]

    @property
    def active_preset(self) -> ChannelPreset | None:
        return self._active

    @property
    def active_camera(self) -> Role:
        if self._active is not None:
            return self._active.camera
        return Role.CAMERA_MONO if self.gw.has(Role.CAMERA_MONO) else Role.CAMERA_COLOR

    @property
    def light_on(self) -> bool:
        return self._light_on

    @property
    def live_wanted(self) -> bool:
        return self._live_wanted

    def is_live(self) -> bool:
        return self._is_live(self.active_camera)

    def _is_live(self, role: Role) -> bool:
        return self.gw.has(role) and bool(self.gw.read(role).is_live())

    def latest_frame(self) -> Frame | None:
        if not self.gw.has(self.active_camera):
            return None
        return self.gw.read(self.active_camera).get_latest_frame()

    def xy_um(self) -> tuple[float, float]:
        return self.gw.read(Role.XY_STAGE).get_position_um()

    def xy_limits_um(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return self.gw.read(Role.XY_STAGE).get_limits_um()

    def z_um(self) -> float:
        return self.gw.read(Role.FOCUS).get_position_um()

    def z_limits_um(self) -> tuple[float, float]:
        return self.gw.read(Role.FOCUS).get_limits_um()

    def objectives(self) -> list[ObjectiveInfo]:
        return self.gw.read(Role.OBJECTIVE_TURRET).get_objectives()

    def objective(self) -> ObjectiveInfo:
        return self.gw.read(Role.OBJECTIVE_TURRET).current_objective()

    def pixel_size_um(self, role: Role | None = None) -> float:
        caps = self.gw.read(role or self.active_camera).get_capabilities()
        return caps.pixel_size_um / self.objective().magnification

    def field_of_view_um(self, role: Role | None = None) -> tuple[float, float]:
        role = role or self.active_camera
        caps = self.gw.read(role).get_capabilities()
        px = self.pixel_size_um(role)
        return caps.width * px, caps.height * px

    def incubator_telemetry(self) -> IncubatorTelemetry | None:
        if not self.gw.has(Role.INCUBATOR):
            return None
        try:
            return self.gw.read(Role.INCUBATOR).read_telemetry()
        except Exception:  # noqa: BLE001 - a disconnected incubator simply has no telemetry
            return None

    # ------------------------------------------------------------------------------ channel
    def select_channel(self, name: str) -> Future:
        preset = self.preset(name)
        return self.submit(f"channel {name}", self._apply_channel, preset, Actor.OPERATOR, None,
                           self._light_on)

    def _apply_channel(self, preset: ChannelPreset, actor: Actor, owner: str | None,
                       light_on: bool, settings: ChannelSettings | None = None,
                       start_live: bool | None = None) -> None:
        gw = self.gw
        s = settings or self._settings[preset.name]
        kw: Issuer = {"actor": actor, "owner": owner}
        prev = self._active
        if prev is not None and prev.camera != preset.camera and self._is_live(prev.camera):
            gw.execute("camera.stop_live", camera=prev.camera.value, **kw)
        gw.execute("led.all_off", **kw)
        if gw.read(Role.ILLUMINATION).get_carriage_position() != preset.carriage_position:
            gw.execute("carriage.select", position=preset.carriage_position, wait=True, **kw)
        self._apply_phase(preset, actor, owner)
        cam = preset.camera.value
        gw.execute("camera.set_exposure", camera=cam, exposure_ms=s.exposure_ms, **kw)
        gw.execute("camera.set_gain", camera=cam, gain_db=s.gain_db, **kw)
        if light_on:
            gw.execute("led.set", channel_id=preset.led_id, enabled=True,
                       intensity_pct=s.intensity_pct, **kw)
        self._active = preset
        want_live = self._live_wanted if start_live is None else start_live
        if want_live and not self._is_live(preset.camera):
            gw.execute("camera.start_live", camera=cam, **kw)
        self._emit("channel", preset)

    def _apply_phase(self, preset: ChannelPreset, actor: Actor, owner: str | None) -> None:
        if preset.phase_mode == "keep" or not self.gw.has(Role.PHASE_TURRET):
            return
        phase = self.gw.read(Role.PHASE_TURRET)
        positions = phase.get_positions()
        open_pos = next((p.index for p in positions if p.label.lower() == "open"), positions[0].index)
        target = open_pos
        if preset.phase_mode == "matched":
            ring = self.objective().phase_ring
            match = next((p.index for p in positions if ring and p.label == ring), None)
            if match is None:
                self._emit("warning", f"no phase annulus for {self.objective().label}; using Open")
            else:
                target = match
        if phase.get_position() != target:
            self.gw.execute("phase.select", position=target, wait=True, actor=actor, owner=owner)

    def set_exposure(self, exposure_ms: float) -> None:
        s = self.settings()
        self.gw.execute("camera.set_exposure", camera=self.active_camera.value, exposure_ms=exposure_ms)
        s.exposure_ms = exposure_ms

    def set_gain(self, gain_db: float) -> None:
        s = self.settings()
        self.gw.execute("camera.set_gain", camera=self.active_camera.value, gain_db=gain_db)
        s.gain_db = gain_db

    def set_intensity(self, intensity_pct: float) -> None:
        s = self.settings()
        if self._active is not None and self._light_on:
            self.gw.execute("led.set", channel_id=self._active.led_id, enabled=True,
                            intensity_pct=intensity_pct)
        s.intensity_pct = intensity_pct

    def set_light(self, on: bool) -> None:
        if self._active is not None:
            s = self.settings()
            self.gw.execute("led.set", channel_id=self._active.led_id, enabled=on,
                            intensity_pct=s.intensity_pct)
        self._light_on = on
        self._emit("light", on)

    # ------------------------------------------------------------------------------ motion
    def jog_xy(self, dx_um: float, dy_um: float) -> None:
        self.gw.execute("xy.move_relative", dx_um=dx_um, dy_um=dy_um)

    def move_xy(self, x_um: float, y_um: float) -> None:
        self.gw.execute("xy.move_absolute", x_um=x_um, y_um=y_um)

    def jog_z(self, dz_um: float) -> None:
        self.gw.execute("focus.move_relative", dz_um=dz_um)

    def move_z(self, z_um: float) -> None:
        self.gw.execute("focus.move_absolute", z_um=z_um)

    def center_on_pixel(self, col: float, row: float, width: int, height: int) -> None:
        """Move the stage so that image pixel (col, row) becomes the centre of the field."""
        px = self.pixel_size_um()
        self.jog_xy((col - width / 2) * px, (row - height / 2) * px)

    def select_objective(self, position: int) -> Future:
        return self.submit(f"objective {position}", self._change_objective, position,
                           Actor.OPERATOR, None)

    def _change_objective(self, position: int, actor: Actor, owner: str | None) -> None:
        gw = self.gw
        kw: Issuer = {"actor": actor, "owner": owner}
        current = self.objective()
        if position == current.position:
            return
        new = next(o for o in self.objectives() if o.position == position)
        z = self.z_um()
        lo, hi = self.z_limits_um()
        target_z = min(hi, max(lo, z + new.parfocal_offset_um - current.parfocal_offset_um))
        safe = gw.policy.limits["turret_safe_z_um"]
        if z > safe:
            gw.execute("focus.move_absolute", z_um=safe, wait=True, **kw)
        gw.execute("objective.select", position=position, wait=True, **kw)
        if self._active is not None and self._active.phase_mode == "matched":
            self._apply_phase(self._active, actor, owner)
        gw.execute("focus.move_absolute", z_um=target_z, wait=True, **kw)
        self._emit("objective", new)

    # ------------------------------------------------------------------------------ camera
    def set_live(self, on: bool) -> None:
        cam = self.active_camera.value
        if on:
            self.gw.execute("camera.start_live", camera=cam)
        else:
            self.gw.execute("camera.stop_live", camera=cam)
        self._live_wanted = on
        self._emit("live", on)

    def snap(self) -> Future:
        return self.submit("snap", self._snap, Actor.OPERATOR, None)

    def _snap(self, actor: Actor, owner: str | None) -> Frame:
        cam = self.active_camera.value
        kw: Issuer = {"actor": actor, "owner": owner}
        was_live = self._is_live(self.active_camera)
        if was_live:
            self.gw.execute("camera.stop_live", camera=cam, **kw)
        try:
            frame = self.gw.execute("camera.snap", camera=cam, **kw).value
        finally:
            if was_live and not self.gw.stop_latched:
                self.gw.execute("camera.start_live", camera=cam, **kw)
        self.last_snap = frame
        self._emit("snap", frame)
        return frame

    # ------------------------------------------------------------------------------ acquisition
    def run_acquisition(self, plan: AcquisitionPlan, *,
                        on_progress: Callable[[Progress], None] | None = None,
                        on_frame: Callable[[AcquiredFrame], None] | None = None,
                        save_dir: Path | None = None) -> Future:
        self._live_wanted = False
        self._engine = AcquisitionEngine(self)
        return self.submit("acquisition", self._engine.run, plan, on_progress=on_progress,
                           on_frame=on_frame, save_dir=save_dir)

    def cancel_acquisition(self) -> None:
        if self._engine is not None:
            self._engine.cancel()

    # ------------------------------------------------------------------------------ incubator
    def incubator_set(self, control: str, value: float, *, confirmed: bool) -> None:
        cmd, param = INCUBATOR_SETPOINT_COMMANDS[control]
        params: dict[str, Any] = {param: value}
        self.gw.execute(cmd, confirmed=confirmed, **params)

    def incubator_enable(self, control: str, enabled: bool, *, confirmed: bool) -> None:
        self.gw.execute("incubator.set_enabled", control=control, enabled=enabled,
                        confirmed=confirmed)

    # ------------------------------------------------------------------------------ STOP
    def stop(self, reason: str = "operator STOP") -> StopReport:
        self._live_wanted = False
        self._light_on = False  # STOP switches every LED off; the operator switches light back on
        report = self.gw.stop_all(actor=Actor.OPERATOR, reason=reason)
        self._emit("stop", report)
        return report

    def clear_stop(self) -> None:
        self.gw.clear_stop(actor=Actor.OPERATOR)
        self._emit("stop_cleared", None)

    # ------------------------------------------------------------------------------ lifecycle
    def initialize(self, channel: str | None = None, *, live: bool = False) -> None:
        """Blocking start-up: select the initial channel (system actor)."""
        self.gw.note("instrument.initialize", details={"channel": channel or "", "live": live})
        if not self.presets:
            return
        name = channel or next((p.name for p in self.presets if p.name == "Phase contrast"),
                               self.presets[0].name)
        self._live_wanted = live
        self._apply_channel(self.preset(name), Actor.SYSTEM, None, True)

    def shutdown(self) -> None:
        self._live_wanted = False
        for role in (Role.CAMERA_MONO, Role.CAMERA_COLOR):
            if self.gw.has(role):
                try:
                    self.gw.execute("camera.stop_live", camera=role.value, actor=Actor.SYSTEM)
                except CommandRejected:
                    pass
        if self.gw.has(Role.ILLUMINATION):
            try:
                self.gw.execute("led.all_off", actor=Actor.SYSTEM)
            except CommandRejected:
                pass
        self._worker.shutdown(wait=False, cancel_futures=True)
        self.gw.disconnect_all()
        self.gw.note("instrument.shutdown")

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until queued sequences have run (used by tests and the screenshot tool)."""
        fut = self._worker.submit(lambda: None)
        try:
            fut.result(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def is_fluorescence(self) -> bool:
        return self._active is not None and self._active.kind == FLUORESCENCE
