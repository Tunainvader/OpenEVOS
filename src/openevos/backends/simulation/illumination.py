"""Simulated illumination: LED channels plus the epi carriage (4 cube slots + mirror)."""

from __future__ import annotations

from typing import Any

from openevos.backends.simulation.base import SimulatedBase
from openevos.backends.simulation.clock import SimClock
from openevos.backends.simulation.profile import MIRROR_LABEL, CubeModel, led_channels
from openevos.hal.interfaces import DeviceError, IlluminationDevice
from openevos.hal.types import LedChannel, LedState, StatePosition


class SimulatedIllumination(SimulatedBase, IlluminationDevice):
    def __init__(self, clock: SimClock, cubes: tuple[CubeModel, ...], start_carriage: int | None = None):
        self._init_sim(device_type="illumination", display_name="LEDs + epi carriage", clock=clock)
        self.cubes = cubes
        self._channels = led_channels(cubes)
        self._leds = {c.channel_id: LedState(c.channel_id, False, 30.0) for c in self._channels}
        self._carriage = [StatePosition(c.slot, f"{c.name} ({c.excitation_nm} / {c.emission_nm})",
                                        {"cube": c.name, "fluor": c.fluor}) for c in cubes]
        self.mirror_position = len(cubes) + 1
        self._carriage.append(StatePosition(self.mirror_position, MIRROR_LABEL, {"cube": None}))
        start = start_carriage or self.mirror_position
        self._c_current = start
        self._c_target = start
        self._c_done = 0.0

    # -- LEDs ---------------------------------------------------------------------------
    def get_channels(self) -> list[LedChannel]:
        return list(self._channels)

    def get_led_states(self) -> list[LedState]:
        with self._lock:
            return list(self._leds.values())

    def set_led(self, channel_id: str, enabled: bool, intensity_pct: float) -> None:
        with self._lock:
            self._require_ready()
            if channel_id not in self._leds:
                raise DeviceError(f"no LED channel {channel_id}")
            if not 0.0 <= intensity_pct <= 100.0:
                raise DeviceError("intensity outside 0..100 %")
            self._leds[channel_id] = LedState(channel_id, bool(enabled), float(intensity_pct))

    def all_leds_off(self) -> None:
        with self._lock:  # allowed even when faulted or disconnected: switching off is always safe
            for cid, st in self._leds.items():
                self._leds[cid] = LedState(cid, False, st.intensity_pct)

    # -- carriage -----------------------------------------------------------------------
    def _settle(self) -> None:
        if self._c_target != self._c_current and self._clock.now() >= self._c_done:
            self._c_current = self._c_target

    def get_carriage_positions(self) -> list[StatePosition]:
        return list(self._carriage)

    def get_carriage_position(self) -> int:
        with self._lock:
            self._settle()
            return self._c_current

    def select_carriage_position(self, position: int) -> None:
        with self._lock:
            self._require_ready()
            if position not in {p.index for p in self._carriage}:
                raise DeviceError(f"no carriage position {position}")
            self._settle()
            if self._c_target != self._c_current:
                raise DeviceError("carriage still moving")
            steps = abs(position - self._c_current)
            if steps == 0:
                return
            self._c_target = position
            self._c_done = self._clock.now() + self._clock.duration(0.1 + 0.15 * steps)
            self._settle()

    def is_busy(self) -> bool:
        with self._lock:
            self._settle()
            return self._c_target != self._c_current

    def halt(self) -> None:
        self.all_leds_off()

    def _busy_message(self) -> str:
        return f"carriage moving to position {self._c_target}"

    def _details(self) -> dict[str, Any]:
        self._settle()
        on = [f"{s.channel_id}@{s.intensity_pct:g}%" for s in self._leds.values() if s.enabled]
        return {"carriage": self._c_current, "leds_on": on}

    # -- used by the simulated optics ---------------------------------------------------
    def cube_at(self, position: int) -> CubeModel | None:
        return next((c for c in self.cubes if c.slot == position), None)
