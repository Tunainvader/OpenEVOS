"""Simulation backend: a complete simulated EVOS-shaped instrument.

Every device identifies as `simulated=True`, with backend "simulation" and "(simulated)" in its
display name. The simulator is shaped after what the FL Auto 1.6 vendor software expects
(docs/evidence/vendor_software_analysis.md), and all its numbers are simulation defaults
(see `profile.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from openevos.backends.simulation.camera import SimulatedCamera
from openevos.backends.simulation.clock import FakeClock, SimClock
from openevos.backends.simulation.illumination import SimulatedIllumination
from openevos.backends.simulation.incubator import SimulatedIncubator
from openevos.backends.simulation.motion import (
    SimulatedFocus,
    SimulatedObjectiveTurret,
    SimulatedPhaseTurret,
    SimulatedXYStage,
)
from openevos.backends.simulation.optics import SimulatedOptics
from openevos.backends.simulation.profile import MONO_CAMERA, SimulationConfig, color_camera
from openevos.backends.simulation.specimen import Specimen
from openevos.hal.device_registry import DeviceRegistry
from openevos.hal.types import Role

__all__ = [
    "FakeClock",
    "SimClock",
    "SimulatedInstrument",
    "SimulationConfig",
    "build_simulated_instrument",
    "create_simulated_instrument",
]


@dataclass
class SimulatedInstrument:
    registry: DeviceRegistry
    config: SimulationConfig
    clock: SimClock
    optics: SimulatedOptics
    stage: SimulatedXYStage
    focus: SimulatedFocus
    turret: SimulatedObjectiveTurret
    phase: SimulatedPhaseTurret
    illumination: SimulatedIllumination
    mono: SimulatedCamera
    color: SimulatedCamera
    incubator: SimulatedIncubator


def build_simulated_instrument(config: SimulationConfig | None = None,
                               clock: SimClock | None = None) -> SimulatedInstrument:
    cfg = config or SimulationConfig()
    clk = clock or SimClock(cfg.time_scale)
    stage = SimulatedXYStage(clk, (cfg.x_limits_um, cfg.y_limits_um), cfg.xy_start_um,
                             cfg.xy_speed_um_s, homed=cfg.start_homed)
    focus = SimulatedFocus(clk, cfg.z_limits_um, cfg.start_z_um, cfg.z_speed_um_s,
                           homed=cfg.start_homed)
    turret = SimulatedObjectiveTurret(clk, cfg.objectives, cfg.start_objective)
    phase = SimulatedPhaseTurret(clk, cfg.phase_positions, start=1)
    illum = SimulatedIllumination(clk, cfg.cubes)
    specimen = Specimen(cfg.seed, cfg.cell_density_per_mm2)
    optics = SimulatedOptics(specimen, cfg.specimen_z_um, stage, focus, turret, phase, illum)
    mono = SimulatedCamera(MONO_CAMERA, optics, clk, cfg.seed)
    color = SimulatedCamera(color_camera(*cfg.color_resolution), optics, clk, cfg.seed)
    incubator = SimulatedIncubator(clk, cfg.ambient_temperature_c, cfg.incubator_time_acceleration,
                                   cfg.seed)
    reg = DeviceRegistry()
    reg.register(Role.CAMERA_MONO, mono)
    reg.register(Role.CAMERA_COLOR, color)
    reg.register(Role.XY_STAGE, stage)
    reg.register(Role.FOCUS, focus)
    reg.register(Role.OBJECTIVE_TURRET, turret)
    reg.register(Role.PHASE_TURRET, phase)
    reg.register(Role.ILLUMINATION, illum)
    reg.register(Role.INCUBATOR, incubator)
    return SimulatedInstrument(reg, cfg, clk, optics, stage, focus, turret, phase, illum, mono,
                               color, incubator)


def create_simulated_instrument(options: dict | None = None) -> DeviceRegistry:
    """Backend factory used by `openevos.core.device_manager` (configuration-driven selection)."""
    opts = dict(options or {})
    cfg = SimulationConfig(**opts)
    return build_simulated_instrument(cfg).registry
