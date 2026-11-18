"""Simulated motion devices (XY stage, focus). Latency via the injected clock; never sleeps."""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass

from openevos.core.clock import Clock, TimerHandle
from openevos.core.models import (
    Activity,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    Health,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    Position,
)
from openevos.hal.backend import ExecutionContext, Reporter
from openevos.safety.capabilities import (
    AxisCapability,
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
)


@dataclass
class _Move:
    command_id: str
    ctx: ExecutionContext
    start: dict[str, float]
    target: dict[str, float]
    t0: float
    duration: float
    timer: TimerHandle | None = None


class SimMotionDevice:
    backend_kind = BackendKind.SIMULATED

    def __init__(self, device_id: str, kind: DeviceKind, axes: tuple[str, ...], clock: Clock, *,
                 command_type: type, speed_um_s: float = 2000.0, unit: str = "um", frame: str = "sim-stage",
                 min_move_s: float = 0.05):
        self.device_id, self.kind, self.axes, self.clock = device_id, kind, axes, clock
        self.physical_id = f"sim:{device_id}"
        self.unit, self.frame = unit, frame
        self._command_type = command_type
        self._speed, self._min_move_s = speed_um_s, min_move_s
        self._pos = {a: 0.0 for a in axes}
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._report: Reporter | None = None
        self._move: _Move | None = None
        # --- fault injection (tests / demos) ---
        self.fail_next_move: str | None = None
        self.stall_next_move = False
        self.readback_offset = 0.0
        self.start_raises: str | None = None

    # ------------------------------------------------------------------ helpers
    def _obs(self, **fields) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), **fields)

    def _position(self, values: dict[str, float] | None = None, offset: float = 0.0) -> Position:
        v = values if values is not None else self._pos
        return Position(axes=self.axes, values=tuple(v[a] + offset for a in self.axes), unit=self.unit, frame=self.frame)  # noqa: E501

    def _interpolate(self, m: _Move) -> dict[str, float]:
        frac = 1.0 if m.duration <= 0 else min(1.0, max(0.0, (self.clock.monotonic() - m.t0) / m.duration))
        return {a: m.start[a] + (m.target[a] - m.start[a]) * frac for a in self.axes}

    # ------------------------------------------------------------------ DeviceBackend
    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def connect(self) -> None:
        self._obs(connection=Connection.CONNECTING, activity=Activity.INITIALIZING, reason="simulator connecting")
        with self._lock:
            pos = self._position()
        self._obs(connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE, position=pos,
                  reason="simulator connected")

    def disconnect(self) -> None:
        with self._lock:
            m, self._move = self._move, None
            if m and m.timer:
                m.timer.cancel()
        self._obs(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN, reason="disconnected")

    def simulate_unexpected_disconnect(self) -> None:
        with self._lock:
            m, self._move = self._move, None
            if m and m.timer:
                m.timer.cancel()
        self._obs(connection=Connection.DISCONNECTED, health=Health.FAULT, activity=Activity.UNKNOWN,
                  reason="simulated cable pull", fault="simulated unexpected disconnect")
        if m:
            m.ctx.fail("device disconnected during motion", "DISCONNECTED")

    def start(self, command: Command, ctx: ExecutionContext) -> None:
        if self.start_raises:
            msg, self.start_raises = self.start_raises, None
            raise RuntimeError(msg)
        if not isinstance(command, self._command_type):
            ctx.fail(f"{self.device_id} cannot execute {command.kind}", "UNSUPPORTED")
            return
        if ctx.cancel_requested:
            with self._lock:
                pos = self._position()
            ctx.mark_cancelled(pos)
            return
        target = command.target().as_dict()
        with self._lock:
            if self._move is not None:
                ctx.fail("simulator busy", "BUSY")
                return
            dist = max(abs(target[a] - self._pos[a]) for a in self.axes)
            m = _Move(command.command_id, ctx, dict(self._pos), target, self.clock.monotonic(),
                      max(self._min_move_s, dist / self._speed))
            self._move = m
            fail_msg, self.fail_next_move = self.fail_next_move, None
            stall, self.stall_next_move = self.stall_next_move, False
        self._obs(activity=Activity.MOVING, reason=f"moving to {target}")
        ctx.mark_started()  # only now has motion "started"
        with self._lock:
            if self._move is not m:
                return  # cancelled/disconnected meanwhile
            if fail_msg:
                m.timer = self.clock.call_later(m.duration / 2, lambda: self._fault(m, fail_msg))
            elif not stall:
                m.timer = self.clock.call_later(m.duration, lambda: self._arrive(m))

    def _arrive(self, m: _Move) -> None:
        with self._lock:
            if self._move is not m:
                return
            self._pos = dict(m.target)
            self._move = None
            pos = self._position()
        self._obs(activity=Activity.IDLE, position=pos, reason="arrived")
        m.ctx.complete(pos)

    def _fault(self, m: _Move, message: str) -> None:
        with self._lock:
            if self._move is not m:
                return
            self._pos = self._interpolate(m)
            self._move = None
            pos = self._position()
        self._obs(activity=Activity.IDLE, health=Health.FAULT, position=pos, reason=message, fault=message)
        m.ctx.fail(message, "SIM_FAULT")

    def cancel(self, command_id: str) -> None:
        with self._lock:
            m = self._move
            if m is None or m.command_id != command_id:
                return
            if m.timer:
                m.timer.cancel()
            self._pos = self._interpolate(m)
            self._move = None
            pos = self._position()
        self._obs(activity=Activity.IDLE, position=pos, reason="motion cancelled")
        m.ctx.mark_cancelled(pos)

    def halt(self) -> None:
        """Router STOP: stop any move now (the router has already cancelled its command)."""
        with self._lock:
            m = self._move
        if m is not None:
            self.cancel(m.command_id)

    def read_position(self) -> Position | None:
        with self._lock:
            return self._position(offset=self.readback_offset)

    def recover(self) -> None:
        with self._lock:
            m, self._move = self._move, None
            if m is not None:
                if m.timer:
                    m.timer.cancel()
                self._pos = self._interpolate(m)
            self.readback_offset = 0.0
            pos = self._position()
        self._obs(health=Health.NORMAL, activity=Activity.IDLE, position=pos, reason="recovered: stopped and re-read")

    # ------------------------------------------------------------------ capabilities
    def capabilities(self, limits: dict[str, tuple[float, float]], tolerance: float = 0.5) -> DeviceCapabilities:
        kind_cmd = {MoveStageAbsolute: "stage.move_absolute", MoveFocusAbsolute: "focus.move_absolute"}[self._command_type]  # noqa: E501
        return DeviceCapabilities(
            device_id=self.device_id, backend_kind=BackendKind.SIMULATED, source=CapabilitySource.SIMULATED,
            supported_commands=frozenset({kind_cmd}),
            axes=tuple(AxisCapability(name=a, limits=AxisLimits(minimum=lo, maximum=hi, unit=self.unit),
                                      coordinate_frame=self.frame, calibration_valid=True,
                                      readback_tolerance=tolerance)
                       for a, (lo, hi) in limits.items()),
            note="Simulator values only; not EVOS specifications.",
        )


# Deliberately synthetic, origin-centred ranges: they must never be mistaken for EVOS travel limits.
SIM_XY_LIMITS = {"x": (-50_000.0, 50_000.0), "y": (-50_000.0, 50_000.0)}
SIM_Z_LIMITS = {"z": (0.0, 10_000.0)}


def sim_xy_stage(device_id: str, clock: Clock, **kw) -> SimMotionDevice:
    return SimMotionDevice(device_id, DeviceKind.XY_STAGE, ("x", "y"), clock, command_type=MoveStageAbsolute, **kw)


def sim_focus(device_id: str, clock: Clock, **kw) -> SimMotionDevice:
    kw.setdefault("speed_um_s", 500.0)
    kw.setdefault("frame", "sim-focus")
    return SimMotionDevice(device_id, DeviceKind.FOCUS, ("z",), clock, command_type=MoveFocusAbsolute, **kw)
