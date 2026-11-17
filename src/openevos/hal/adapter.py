"""Run a gateway-era HAL device (`hal.interfaces.HardwareDevice`) as a core `DeviceBackend`.

This lets the command core (router → SafetyManager → resources → worker) drive the rich
simulated instrument the desktop UI was built on, so the UI has one execution path.

Rules:
* **Simulated devices only.** A physical backend must implement `DeviceBackend` itself, because
  it has to report `firmware_mode` from the device (ADR-0009). The adapter refuses anything that
  does not identify as simulated.
* Observations (connection, health, activity, position, attributes) are read from the device
  after every operation and reported through the core reporter; interlocks then read the store.
* `start()` calls the device, marks the command RUNNING only after the device accepted it, then
  polls `is_busy()` on the injected clock. It never sleeps. Snapping a frame blocks the camera's
  own worker for the exposure; STOP still works, because `halt()` is called directly.
* Every outcome is verified by readback (position, turret slot, LED state, exposure/gain echo,
  setpoint echo) before the command may succeed.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass

from openevos.core.clock import Clock, TimerHandle
from openevos.core.command_language import POSITION_TOLERANCE_UM
from openevos.core.models import (
    Activity,
    AllIlluminationOff,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    Health,
    HomeFocus,
    HomeStage,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    Position,
    SelectCarriagePosition,
    SetCameraExposure,
    SetCameraGain,
    SetCameraLive,
    SetIllumination,
    SetIncubatorControlEnabled,
    SetIncubatorSetpoint,
    SetTurretPosition,
    SnapImage,
)
from openevos.hal.backend import ExecutionContext, Reporter
from openevos.hal.interfaces import (
    CameraDevice,
    FocusDevice,
    HardwareDevice,
    IlluminationDevice,
    IncubatorDevice,
    StateDevice,
    XYStageDevice,
)
from openevos.hal.status_model import RuntimeState
from openevos.safety.capabilities import (
    AxisCapability,
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
    ParameterRange,
)

UNIT = "um"
STAGE_FRAME = "stage"
FOCUS_FRAME = "focus"

_KIND_COMMANDS: dict[DeviceKind, frozenset[str]] = {
    DeviceKind.XY_STAGE: frozenset({"stage.move_absolute", "stage.home"}),
    DeviceKind.FOCUS: frozenset({"focus.move_absolute", "focus.home"}),
    DeviceKind.TURRET: frozenset({"turret.set_position"}),
    DeviceKind.ILLUMINATION: frozenset({"illumination.set", "illumination.select_carriage", "illumination.all_off"}),
    DeviceKind.CAMERA: frozenset({"camera.set_exposure", "camera.set_gain", "camera.set_live", "camera.snap"}),
    DeviceKind.INCUBATOR: frozenset({"incubator.set_setpoint", "incubator.set_enabled"}),
}


def kind_of(device: HardwareDevice) -> DeviceKind:
    for iface, kind in ((CameraDevice, DeviceKind.CAMERA), (XYStageDevice, DeviceKind.XY_STAGE),
                        (FocusDevice, DeviceKind.FOCUS), (StateDevice, DeviceKind.TURRET),
                        (IlluminationDevice, DeviceKind.ILLUMINATION), (IncubatorDevice, DeviceKind.INCUBATOR)):
        if isinstance(device, iface):
            return kind
    raise TypeError(f"no core device kind for {type(device).__name__}")


@dataclass
class _Op:
    command: Command
    ctx: ExecutionContext
    timer: TimerHandle | None = None


class HalDeviceBackend:
    backend_kind = BackendKind.SIMULATED

    def __init__(self, device_id: str, device: HardwareDevice, clock: Clock, *, poll_s: float = 0.005):
        ident = device.identify()
        if not ident.simulated:
            raise ValueError(f"{ident.display_name} is not simulated; a physical device needs a native "
                             "DeviceBackend that reports its firmware mode (ADR-0009)")
        self.device_id, self.device, self.clock = device_id, device, clock
        self.kind = kind_of(device)
        self.physical_id = f"sim:{device_id}:{id(device):x}"
        self._poll_s = poll_s
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._report: Reporter | None = None
        self._op: _Op | None = None

    # ------------------------------------------------------------------ observation
    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def observe(self, *, activity: Activity | None = None, reason: str | None = None, fault: str | None = None) -> None:
        """Read the device and report what it is doing now. Also used for periodic refresh."""
        if self._report is None:
            return
        d = self.device
        try:
            status = d.get_status()
        except Exception as exc:  # noqa: BLE001
            self._report(seq=next(self._seq), health=Health.UNKNOWN, reason=f"status read failed: {exc}")
            return
        connection = Connection.DISCONNECTED if status.state is RuntimeState.DISCONNECTED else Connection.CONNECTED
        health = Health.FAULT if status.state is RuntimeState.FAULTED else Health.NORMAL
        if activity is None:
            if connection is Connection.DISCONNECTED:
                activity = Activity.UNKNOWN
            elif isinstance(d, CameraDevice) and d.is_live():
                activity = Activity.ACQUIRING
            elif d.is_busy():
                activity = Activity.MOVING
            else:
                activity = Activity.IDLE
        fields: dict = {"connection": connection, "health": health, "activity": activity,
                        "attributes": self._attributes()}
        pos = self.read_position()
        if pos is not None:
            fields["position"] = pos
        self._report(seq=next(self._seq), reason=reason or status.message or None, fault=fault, **fields)

    def _attributes(self) -> dict:
        d = self.device
        if isinstance(d, (XYStageDevice, FocusDevice)):
            return {"homed": bool(d.is_homed())}
        if isinstance(d, StateDevice):
            return {"position": int(d.get_position())}
        if isinstance(d, IlluminationDevice):
            on = tuple(sorted(s.channel_id for s in d.get_led_states() if s.enabled))
            return {"carriage_position": int(d.get_carriage_position()), "leds_on": on}
        if isinstance(d, CameraDevice):
            return {"live": bool(d.is_live()), "exposure_ms": float(d.get_exposure_ms()),
                    "gain_db": float(d.get_gain_db())}
        return {}

    def read_position(self) -> Position | None:
        d = self.device
        if isinstance(d, XYStageDevice):
            x, y = d.get_position_um()
            return Position(axes=("x", "y"), values=(x, y), unit=UNIT, frame=STAGE_FRAME)
        if isinstance(d, FocusDevice):
            return Position(axes=("z",), values=(d.get_position_um(),), unit=UNIT, frame=FOCUS_FRAME)
        return None

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), connection=Connection.CONNECTING, activity=Activity.INITIALIZING,
                         reason="connecting (simulated)")
        self.device.connect()
        self.observe(reason="connected (simulated)")

    def disconnect(self) -> None:
        self.halt()
        self.device.disconnect()
        self.observe(reason="disconnected")

    def recover(self) -> None:
        """Operator recovery: stop, re-read, report what the device now says."""
        self.halt()
        self.observe(reason="recovered: halted and re-read")

    def halt(self) -> None:
        with self._lock:
            op, self._op = self._op, None
            if op is not None and op.timer is not None:
                op.timer.cancel()
        try:
            self.device.halt()
        finally:
            self.observe(reason="halted")
            if op is not None:
                op.ctx.mark_cancelled(self.read_position())

    def cancel(self, command_id: str) -> None:
        with self._lock:
            op = self._op
            if op is None or op.command.command_id != command_id:
                return
            self._op = None
            if op.timer is not None:
                op.timer.cancel()
        d = self.device
        stop = getattr(d, "stop", None)
        try:
            (stop or d.halt)()
        finally:
            self.observe(reason="cancelled")
            op.ctx.mark_cancelled(self.read_position())

    # ------------------------------------------------------------------ execution
    def start(self, command: Command, ctx: ExecutionContext) -> None:
        with self._lock:
            if self._op is not None:
                ctx.fail(f"{self.device_id} is still executing {self._op.command.kind}", "BUSY")
                return
        if ctx.cancel_requested:
            ctx.mark_cancelled(self.read_position())
            return
        if isinstance(command, SnapImage):
            self._snap(command, ctx)
            return
        try:
            waits = self._apply(command)
        except Exception as exc:  # noqa: BLE001 - a device error fails the command
            self.observe(reason=f"device error: {exc}")
            ctx.fail(f"device error: {exc}", "DEVICE_ERROR")
            return
        if ctx.cancel_requested:
            # STOP (or a cancel) arrived while the device call was in flight. The router's halt
            # may already have run, so undo what this call started: halt the device again.
            try:
                self.device.halt()
            finally:
                self.observe(reason="cancelled during start; device halted")
                ctx.mark_cancelled(self.read_position())
            return
        if not waits:
            ctx.mark_started()
            self.observe(reason=f"{command.kind} applied")
            self._finish(_Op(command, ctx))
            return
        op = _Op(command, ctx)
        with self._lock:
            self._op = op
        self.observe(activity=Activity.MOVING, reason=f"{command.kind} started")
        ctx.mark_started()
        self._poll(op)

    def _snap(self, command: SnapImage, ctx: ExecutionContext) -> None:
        """Blocks this camera's worker for the exposure; STOP still reaches `halt()` directly."""
        if self.device.is_live():  # type: ignore[attr-defined]
            ctx.fail("camera is streaming; stop live view first", "DEVICE_STATE")
            return
        self.observe(activity=Activity.ACQUIRING, reason="exposing")
        ctx.mark_started()
        try:
            frame = self.device.snap()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.observe(reason=f"snap failed: {exc}")
            ctx.fail(f"device error: {exc}", "DEVICE_ERROR")
            return
        self.observe(reason="frame acquired")
        ctx.complete(None, payload=frame)

    def _apply(self, c: Command) -> bool:
        """Call the device. Returns True if completion must be awaited (motion)."""
        d = self.device
        if isinstance(c, MoveStageAbsolute):
            d.move_absolute_um(c.x, c.y)  # type: ignore[attr-defined]
            return True
        if isinstance(c, MoveFocusAbsolute):
            d.move_absolute_um(c.z)  # type: ignore[attr-defined]
            return True
        if isinstance(c, (HomeStage, HomeFocus)):
            d.home()  # type: ignore[attr-defined]
            return True
        if isinstance(c, SetTurretPosition):
            d.select_position(c.position)  # type: ignore[attr-defined]
            return True
        if isinstance(c, SelectCarriagePosition):
            d.select_carriage_position(c.position)  # type: ignore[attr-defined]
            return True
        if isinstance(c, SetIllumination):
            d.set_led(c.channel, c.enabled, c.intensity_percent)  # type: ignore[attr-defined]
            return False
        if isinstance(c, AllIlluminationOff):
            d.all_leds_off()  # type: ignore[attr-defined]
            return False
        if isinstance(c, SetCameraExposure):
            d.set_exposure_ms(c.exposure_ms)  # type: ignore[attr-defined]
            return False
        if isinstance(c, SetCameraGain):
            d.set_gain_db(c.gain_db)  # type: ignore[attr-defined]
            return False
        if isinstance(c, SetCameraLive):
            (d.start_live if c.live else d.stop_live)()  # type: ignore[attr-defined]
            return False
        if isinstance(c, SetIncubatorSetpoint):
            d.set_setpoint(c.control, c.value)  # type: ignore[attr-defined]
            return False
        if isinstance(c, SetIncubatorControlEnabled):
            d.set_enabled(c.control, c.enabled)  # type: ignore[attr-defined]
            return False
        raise NotImplementedError(f"{self.device_id} cannot execute {c.kind}")

    def _poll(self, op: _Op) -> None:
        if op.ctx.cancel_requested:  # a cancel that raced the start: stop now, never "succeed"
            self.cancel(op.command.command_id)
            return
        with self._lock:
            if self._op is not op:
                return
            busy = self.device.is_busy()
            if busy:
                op.timer = self.clock.call_later(self._poll_s, lambda: self._poll(op))
                return
            self._op = None
        self.observe(reason=f"{op.command.kind} done")
        self._finish(op)

    def _finish(self, op: _Op) -> None:
        problem = self._verify(op.command)
        if problem:
            op.ctx.fail(f"readback: {problem}", "READBACK_MISMATCH")
        else:
            op.ctx.complete(self.read_position())

    def _verify(self, c: Command) -> str | None:
        """Readback for non-motion outcomes; motion is verified by the router against tolerances."""
        d = self.device
        if isinstance(c, (HomeStage, HomeFocus)):
            return None if d.is_homed() else "device did not report homed"  # type: ignore[attr-defined]
        if isinstance(c, SetTurretPosition):
            pos = d.get_position()  # type: ignore[attr-defined]
            return None if pos == c.position else f"turret at {pos}, target {c.position}"
        if isinstance(c, SelectCarriagePosition):
            pos = d.get_carriage_position()  # type: ignore[attr-defined]
            return None if pos == c.position else f"carriage at {pos}, target {c.position}"
        if isinstance(c, SetIllumination):
            st = next((s for s in d.get_led_states() if s.channel_id == c.channel), None)  # type: ignore[attr-defined]
            if st is None or st.enabled != c.enabled or abs(st.intensity_pct - c.intensity_percent) > 1e-6:
                return "LED state readback mismatch"
        if isinstance(c, AllIlluminationOff):
            on = [s.channel_id for s in d.get_led_states() if s.enabled]  # type: ignore[attr-defined]
            return f"LED(s) still on: {on}" if on else None
        if isinstance(c, SetCameraExposure) and abs(d.get_exposure_ms() - c.exposure_ms) > 1e-3:  # type: ignore[attr-defined]
            return "exposure readback mismatch"
        if isinstance(c, SetCameraGain) and abs(d.get_gain_db() - c.gain_db) > 1e-3:  # type: ignore[attr-defined]
            return "gain readback mismatch"
        if isinstance(c, SetCameraLive) and d.is_live() != c.live:  # type: ignore[attr-defined]
            return f"live state readback is {d.is_live()}"  # type: ignore[attr-defined]
        if isinstance(c, SetIncubatorSetpoint) and abs(d.get_setpoints()[c.control] - c.value) > 1e-6:  # type: ignore[attr-defined]
            return "setpoint readback mismatch"
        if isinstance(c, SetIncubatorControlEnabled) and d.get_enabled()[c.control] != c.enabled:  # type: ignore[attr-defined]
            return "enable flag readback mismatch"
        return None

    # ------------------------------------------------------------------ capabilities
    def capabilities(self) -> DeviceCapabilities:
        """Capability record built from the simulator's own declared ranges (SIMULATED source)."""
        d = self.device
        kw: dict = {}
        if isinstance(d, XYStageDevice):
            (x0, x1), (y0, y1) = d.get_limits_um()
            kw["axes"] = tuple(AxisCapability(name=a, limits=AxisLimits(minimum=lo, maximum=hi, unit=UNIT),
                                              coordinate_frame=STAGE_FRAME, calibration_valid=True,
                                              readback_tolerance=POSITION_TOLERANCE_UM)
                               for a, (lo, hi) in (("x", (x0, x1)), ("y", (y0, y1))))
        elif isinstance(d, FocusDevice):
            lo, hi = d.get_limits_um()
            kw["axes"] = (AxisCapability(name="z", limits=AxisLimits(minimum=lo, maximum=hi, unit=UNIT),
                                         coordinate_frame=FOCUS_FRAME, calibration_valid=True,
                                         readback_tolerance=POSITION_TOLERANCE_UM),)
        elif isinstance(d, StateDevice):
            kw["turret_positions"] = tuple(p.index for p in d.get_positions())
        elif isinstance(d, IlluminationDevice):
            chans = d.get_channels()
            kw["illumination_channels"] = tuple(c.channel_id for c in chans)
            kw["carriage_positions"] = tuple(p.index for p in d.get_carriage_positions())
            kw["ranges"] = tuple(ParameterRange(name=f"intensity:{c.channel_id}",
                                                limits=AxisLimits(minimum=0.0, maximum=c.max_intensity_pct, unit="%"))
                                 for c in chans)
        elif isinstance(d, CameraDevice):
            caps = d.get_capabilities()
            kw["ranges"] = (
                ParameterRange(name="exposure_ms", limits=AxisLimits(minimum=caps.exposure_range_ms[0],
                                                                     maximum=caps.exposure_range_ms[1], unit="ms")),
                ParameterRange(name="gain_db", limits=AxisLimits(minimum=caps.gain_range_db[0],
                                                                 maximum=caps.gain_range_db[1], unit="dB")),
            )
        elif isinstance(d, IncubatorDevice):
            controls = d.get_supported_controls()
            kw["incubator_controls"] = tuple(controls)
            kw["ranges"] = tuple(ParameterRange(name=f"setpoint:{n}",
                                                limits=AxisLimits(minimum=s.minimum, maximum=s.maximum, unit=s.unit))
                                 for n, s in controls.items())
        return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.SIMULATED,
                                  source=CapabilitySource.SIMULATED, supported_commands=_KIND_COMMANDS[self.kind],
                                  note="Simulator values only; not EVOS specifications.", **kw)
