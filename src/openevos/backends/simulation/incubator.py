"""Simulated onstage incubator: chamber temperature, CO₂ and O₂ with setpoints.

First-order dynamics towards the setpoint (when the loop is enabled) or towards ambient (when it
is not). Every reading is labelled `source="simulated"`: per plan §4.5 these values must never be
presented as measured.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from openevos.backends.simulation.base import SimulatedBase
from openevos.backends.simulation.clock import SimClock
from openevos.hal.interfaces import DeviceError, IncubatorDevice
from openevos.hal.types import ControlSpec, IncubatorTelemetry, Reading

AMBIENT_CO2_PCT = 0.04
AMBIENT_O2_PCT = 20.9

CONTROLS: dict[str, ControlSpec] = {
    "temperature": ControlSpec("temperature", "Chamber temperature", "degC", 25.0, 40.0, 37.0),
    "co2": ControlSpec("co2", "CO₂", "%", 0.0, 20.0, 5.0),
    "o2": ControlSpec("o2", "O₂", "%", 1.0, 20.9, 20.9),
}

# time constants in simulated seconds
TAU_S = {"temperature": 600.0, "co2": 180.0, "o2": 300.0}
NOISE = {"temperature": 0.02, "co2": 0.03, "o2": 0.05}


class SimulatedIncubator(SimulatedBase, IncubatorDevice):
    def __init__(self, clock: SimClock, ambient_c: float = 23.5, acceleration: float = 1.0,
                 seed: int = 0):
        self._init_sim(device_type="incubator", display_name="Onstage incubator", clock=clock)
        self.ambient_c = ambient_c
        self.acceleration = float(acceleration)
        self._values = {"temperature": ambient_c, "co2": AMBIENT_CO2_PCT, "o2": AMBIENT_O2_PCT}
        self._setpoints = {k: c.default for k, c in CONTROLS.items()}
        self._enabled = {k: False for k in CONTROLS}
        self._t_last = clock.now()
        self._rng = np.random.default_rng([seed & 0xFFFFFFFF, 58])

    def _ambient(self, name: str) -> float:
        return {"temperature": self.ambient_c, "co2": AMBIENT_CO2_PCT, "o2": AMBIENT_O2_PCT}[name]

    def _advance(self) -> None:
        now = self._clock.now()
        dt = max(0.0, now - self._t_last) * self.acceleration
        self._t_last = now
        if dt <= 0:
            return
        for name, tau in TAU_S.items():
            target = self._setpoints[name] if self._enabled[name] else self._ambient(name)
            if name == "temperature" and self._enabled[name]:
                target = max(target, self.ambient_c)  # heater only: cannot cool below ambient
            k = 1.0 - math.exp(-dt / tau)
            self._values[name] += (target - self._values[name]) * k

    # -- IncubatorDevice -------------------------------------------------------------------
    def read_telemetry(self) -> IncubatorTelemetry:
        with self._lock:
            self._require_ready()
            self._advance()
            r: dict[str, Reading] = {}
            for name, unit in (("temperature", "degC"), ("co2", "%"), ("o2", "%")):
                v = self._values[name] + float(self._rng.normal(0.0, NOISE[name]))
                r[name] = Reading(round(v, 3), unit, "simulated")
            heat = 0.0
            if self._enabled["temperature"]:
                err = self._setpoints["temperature"] - self._values["temperature"]
                heat = max(0.0, min(100.0, 18.0 + 60.0 * err))
            r["heater_power"] = Reading(round(heat, 1), "%", "simulated")
            r["co2_valve_open"] = Reading(
                self._enabled["co2"] and self._values["co2"] < self._setpoints["co2"] - 0.05,
                "bool", "simulated")
            r["n2_valve_open"] = Reading(
                self._enabled["o2"] and self._values["o2"] > self._setpoints["o2"] + 0.05,
                "bool", "simulated")
            return IncubatorTelemetry(timestamp=self._clock.now(), readings=r, simulated=True)

    def get_supported_controls(self) -> dict[str, ControlSpec]:
        return dict(CONTROLS)

    def get_setpoints(self) -> dict[str, float]:
        with self._lock:
            return dict(self._setpoints)

    def set_setpoint(self, control: str, value: float) -> None:
        with self._lock:
            self._require_ready()
            spec = CONTROLS.get(control)
            if spec is None:
                raise DeviceError(f"unsupported control {control}")
            if not spec.minimum <= value <= spec.maximum:
                raise DeviceError(f"{control} setpoint {value} outside [{spec.minimum}, {spec.maximum}]")
            self._advance()
            self._setpoints[control] = float(value)

    def get_enabled(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._enabled)

    def set_enabled(self, control: str, enabled: bool) -> None:
        with self._lock:
            self._require_ready()
            if control not in CONTROLS:
                raise DeviceError(f"unsupported control {control}")
            self._advance()
            self._enabled[control] = bool(enabled)

    def _details(self) -> dict[str, Any]:
        return {"enabled": dict(self._enabled), "setpoints": dict(self._setpoints)}
