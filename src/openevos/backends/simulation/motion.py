"""Simulated motion: XY stage, focus drive, objective and phase turrets.

Motion is computed from the clock rather than by a background thread: a move records start,
target, start time and duration, and the position is interpolated whenever it is read. That
keeps the simulator deterministic under `FakeClock` and makes `stop()` instantaneous.
"""

from __future__ import annotations

from typing import Any

from openevos.backends.simulation.base import SimulatedBase
from openevos.backends.simulation.clock import SimClock
from openevos.hal.interfaces import (
    DeviceError,
    FocusDevice,
    ObjectiveTurretDevice,
    PhaseTurretDevice,
    XYStageDevice,
)
from openevos.hal.types import ObjectiveInfo, StatePosition

SETTLE_S = 0.02


class _Trajectory:
    """Straight-line move of an N-dimensional point at constant speed."""

    def __init__(self, position: tuple[float, ...]):
        self.start = position
        self.target = position
        self.t0 = 0.0
        self.duration = 0.0

    def position(self, now: float) -> tuple[float, ...]:
        if self.duration <= 0 or now >= self.t0 + self.duration:
            return self.target
        f = max(0.0, (now - self.t0) / self.duration)
        return tuple(a + (b - a) * f for a, b in zip(self.start, self.target))

    def busy(self, now: float) -> bool:
        return self.duration > 0 and now < self.t0 + self.duration

    def begin(self, now: float, target: tuple[float, ...], duration: float) -> None:
        self.start = self.position(now)
        self.target = target
        self.t0 = now
        self.duration = duration

    def halt(self, now: float) -> None:
        here = self.position(now)
        self.start = self.target = here
        self.duration = 0.0


def _check_limits(name: str, value: float, lo: float, hi: float) -> None:
    # Backends enforce their own limits as a second line of defence behind the command policy.
    if not lo <= value <= hi:
        raise DeviceError(f"{name} target {value:.2f} outside [{lo:g}, {hi:g}]")


class SimulatedXYStage(SimulatedBase, XYStageDevice):
    def __init__(self, clock: SimClock, limits: tuple[tuple[float, float], tuple[float, float]],
                 start: tuple[float, float], speed_um_s: float, homed: bool = True):
        self._init_sim(device_type="xy_stage", display_name="XY stage", clock=clock)
        self._limits = limits
        self._speed = float(speed_um_s)
        self._traj = _Trajectory(tuple(start))
        self._homed = homed

    def get_position_um(self) -> tuple[float, float]:
        with self._lock:
            x, y = self._traj.position(self._clock.now())
            return float(x), float(y)

    def get_limits_um(self):
        return self._limits

    def is_busy(self) -> bool:
        with self._lock:
            return self._traj.busy(self._clock.now())

    def is_homed(self) -> bool:
        return self._homed

    def move_absolute_um(self, x_um: float, y_um: float) -> None:
        with self._lock:
            self._require_ready()
            if not self._homed:
                raise DeviceError("XY stage is not homed")
            (x0, x1), (y0, y1) = self._limits
            _check_limits("X", x_um, x0, x1)
            _check_limits("Y", y_um, y0, y1)
            now = self._clock.now()
            cx, cy = self._traj.position(now)
            dist = max(abs(x_um - cx), abs(y_um - cy))
            dur = self._clock.duration(dist / self._speed + (SETTLE_S if dist > 0 else 0.0))
            self._traj.begin(now, (float(x_um), float(y_um)), dur)

    def stop(self) -> None:
        with self._lock:
            self._traj.halt(self._clock.now())

    def home(self) -> None:
        with self._lock:
            self._require_ready()
            now = self._clock.now()
            cx, cy = self._traj.position(now)
            (x0, _), (y0, _) = self._limits
            dist = max(abs(cx - x0), abs(cy - y0))
            self._traj.begin(now, (x0, y0), self._clock.duration(dist / self._speed + 0.5))
            self._homed = True

    def _details(self) -> dict[str, Any]:
        x, y = self._traj.position(self._clock.now())
        return {"x_um": round(x, 2), "y_um": round(y, 2), "homed": self._homed}


class SimulatedFocus(SimulatedBase, FocusDevice):
    def __init__(self, clock: SimClock, limits: tuple[float, float], start_um: float,
                 speed_um_s: float, homed: bool = True):
        self._init_sim(device_type="focus", display_name="Focus drive", clock=clock)
        self._limits = limits
        self._speed = float(speed_um_s)
        self._traj = _Trajectory((float(start_um),))
        self._homed = homed

    def get_position_um(self) -> float:
        with self._lock:
            return float(self._traj.position(self._clock.now())[0])

    def get_limits_um(self) -> tuple[float, float]:
        return self._limits

    def is_busy(self) -> bool:
        with self._lock:
            return self._traj.busy(self._clock.now())

    def is_homed(self) -> bool:
        return self._homed

    def move_absolute_um(self, z_um: float) -> None:
        with self._lock:
            self._require_ready()
            if not self._homed:
                raise DeviceError("focus is not homed")
            _check_limits("Z", z_um, *self._limits)
            now = self._clock.now()
            dist = abs(z_um - self._traj.position(now)[0])
            dur = self._clock.duration(dist / self._speed + (SETTLE_S if dist > 0 else 0.0))
            self._traj.begin(now, (float(z_um),), dur)

    def stop(self) -> None:
        with self._lock:
            self._traj.halt(self._clock.now())

    def home(self) -> None:
        with self._lock:
            self._require_ready()
            now = self._clock.now()
            dist = abs(self._traj.position(now)[0] - self._limits[0])
            self._traj.begin(now, (self._limits[0],), self._clock.duration(dist / self._speed + 0.3))
            self._homed = True

    def _details(self) -> dict[str, Any]:
        return {"z_um": round(self._traj.position(self._clock.now())[0], 2), "homed": self._homed}


class _SimulatedState(SimulatedBase):
    """Discrete positions. Reports the previous position until a move completes."""

    def _init_state(self, positions: list[StatePosition], start: int, *, circular: bool,
                    s_per_step: float, s_base: float) -> None:
        self._positions = positions
        self._current = start
        self._target = start
        self._t_done = 0.0
        self._circular = circular
        self._s_per_step = s_per_step
        self._s_base = s_base

    def _settle(self) -> None:
        if self._target != self._current and self._clock.now() >= self._t_done:
            self._current = self._target

    def get_positions(self) -> list[StatePosition]:
        return list(self._positions)

    def get_position(self) -> int:
        with self._lock:
            self._settle()
            return self._current

    def is_busy(self) -> bool:
        with self._lock:
            self._settle()
            return self._target != self._current

    def select_position(self, position: int) -> None:
        with self._lock:
            self._require_ready()
            if position not in {p.index for p in self._positions}:
                raise DeviceError(f"no position {position}")
            self._settle()
            if self._target != self._current:
                raise DeviceError("still moving")
            n = len(self._positions)
            steps = abs(position - self._current)
            if self._circular:
                steps = min(steps, n - steps)
            if steps == 0:
                return
            self._target = position
            self._t_done = self._clock.now() + self._clock.duration(self._s_base + steps * self._s_per_step)
            self._settle()

    def stop(self) -> None:
        # A detent device cannot stop between positions; complete the current move (simulated).
        with self._lock:
            self._settle()

    def halt(self) -> None:
        self.stop()

    def _busy_message(self) -> str:
        return f"moving to position {self._target}"

    def _details(self) -> dict[str, Any]:
        self._settle()
        label = next((p.label for p in self._positions if p.index == self._current), "?")
        return {"position": self._current, "label": label}


class SimulatedObjectiveTurret(_SimulatedState, ObjectiveTurretDevice):
    def __init__(self, clock: SimClock, objectives: tuple[ObjectiveInfo, ...], start: int):
        self._init_sim(device_type="objective_turret", display_name="Objective turret", clock=clock)
        self._objectives = list(objectives)
        positions = [StatePosition(o.position, o.label,
                                   {"magnification": o.magnification, "na": o.numerical_aperture,
                                    "phase_ring": o.phase_ring}) for o in objectives]
        self._init_state(positions, start, circular=True, s_per_step=0.25, s_base=0.2)

    def get_objectives(self) -> list[ObjectiveInfo]:
        return list(self._objectives)


class SimulatedPhaseTurret(_SimulatedState, PhaseTurretDevice):
    def __init__(self, clock: SimClock, labels: tuple[str, ...], start: int = 1):
        self._init_sim(device_type="phase_turret", display_name="Phase turret", clock=clock)
        positions = [StatePosition(i + 1, label) for i, label in enumerate(labels)]
        self._init_state(positions, start, circular=True, s_per_step=0.2, s_base=0.15)
