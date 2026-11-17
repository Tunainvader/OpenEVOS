"""Simulation backend: realistic application-level behaviour without touching any hardware.

Every value here (limits, speeds, image size) is a SIMULATOR value, not an EVOS specification.
Simulated capability records are typed `CapabilitySource.SIMULATED` and cannot be attached to a
physical backend (enforced by `DeviceCapabilities`).
"""

from openevos.backends.simulation.minimal.camera import SimCamera
from openevos.backends.simulation.minimal.motion import SimMotionDevice, sim_focus, sim_xy_stage

__all__ = ["SimCamera", "SimMotionDevice", "sim_focus", "sim_xy_stage"]
