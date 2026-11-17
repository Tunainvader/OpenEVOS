"""Optics-aware simulated instrument (rig) built from an InstrumentProfile.

    core = build_core(ThreadClock())
    rig = attach_optical_simulator(core, load_profile(), vessel)

Registers XY stage, focus, turret, illumination (LED + cube carriage) and one camera per profile
role. Cameras render from `SampleWorld` using the *device states* (stage/focus/turret/LED), so
the live image responds to commands exactly as a real instrument would — only through the
command router.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from openevos.backends.rig.camera import SimCamera
from openevos.backends.rig.devices import SimIllumination, SimTurret
from openevos.backends.rig.motion import SimMotionDevice, sim_focus, sim_xy_stage
from openevos.backends.rig.optics import SampleWorld, View
from openevos.core.application import CoreServices
from openevos.instrument.profile import CameraProfile, CameraRole, InstrumentProfile
from openevos.instrument.vessels import Vessel, VesselStageMap

#: Synthetic simulator limits (vessel footprint centre = 0). Never EVOS travel limits.
RIG_XY_LIMITS = {"x": (-50_000.0, 50_000.0), "y": (-50_000.0, 50_000.0)}
RIG_Z_LIMITS = {"z": (0.0, 8_000.0)}
PARFOCAL_UM = {1: 0.0, 2: 3.0, 3: 5.0, 4: 7.5, 5: 9.0}  # synthetic parfocality per slot
PARCENTRIC_UM = {1: (0.0, 0.0), 2: (4.0, -3.0), 3: (-6.0, 5.0), 4: (7.0, 4.0), 5: (0.0, 0.0)}  # synthetic
#: Simulated "true" vessel placement error: the real vessel sits this far from the nominal position, so the
#: vessel-alignment calibration has something to find. Never an EVOS value.
TRUE_VESSEL_OFFSET_UM = (180.0, -120.0)
N_HOT_PIXELS = 36


class SimOpticalCamera(SimCamera):
    def __init__(self, rig: Rig, cam: CameraProfile, **kw):
        super().__init__(cam.device_id, rig.core.clock, rig.core.transport,
                         width=cam.width or 640, height=cam.height or 480, **kw)
        self.rig, self.profile = rig, cam
        self.exposure_ms = 20.0
        self._t0 = rig.core.clock.monotonic()
        drng = np.random.default_rng(4242 + (cam.width or 0))
        h, w = self.height, self.width
        self.hot_pixels = np.column_stack([drng.integers(2, h - 2, N_HOT_PIXELS), drng.integers(2, w - 2, N_HOT_PIXELS)]) \
            if cam.role is CameraRole.MONO else np.empty((0, 2), int)

    def frame_channel(self) -> str | None:
        return self.rig.light.channel

    def frame_extra(self) -> tuple[tuple[str, str], ...]:
        r = self.rig
        x, y = r.stage_xy()
        return (("gain", f"{self.gain:g}"), ("objective_slot", str(r.turret.position)),
                ("x_um", f"{x:.2f}"), ("y_um", f"{y:.2f}"), ("z_um", f"{r.focus_z():.2f}"),
                ("led_percent", f"{r.light.intensity_percent:g}"), ("light_on", str(r.light.emitting)),
                ("camera_role", self.profile.role.value), ("simulator", "openevos.optics"))

    def current_view(self) -> View:
        r = self.rig
        obj = r.profile.objective(r.turret.position)
        mag = obj.magnification if obj and obj.installed and obj.magnification else 1.0
        na = obj.numerical_aperture if obj and obj.numerical_aperture else 0.05
        x, y = r.stage_xy()
        pdx, pdy = PARCENTRIC_UM.get(r.turret.position, (0.0, 0.0))
        return View(x_um=x + pdx, y_um=y + pdy, z_um=r.focus_z(), magnification=mag, numerical_aperture=na,
                    sensor_pixel_um=self.profile.pixel_size_um or 4.0, width=self.width, height=self.height,
                    channel=r.light.channel, led_percent=r.light.intensity_percent, exposure_ms=self.exposure_ms,
                    gain=self.gain, light_on=r.light.emitting and obj is not None and obj.installed,
                    color=self.profile.role is CameraRole.COLOR,
                    max_value=(1 << (self.profile.native_bit_depth or 12)) - 1,
                    parfocal_offset_um=PARFOCAL_UM.get(r.turret.position, 0.0),
                    time_s=r.core.clock.monotonic() - self._t0)

    def render(self) -> np.ndarray:
        img = self.rig.world.render(self.current_view(), self._rng)
        if len(self.hot_pixels) and img.ndim == 2:  # dark-current defects grow with exposure
            hp = self.hot_pixels
            img[hp[:, 0], hp[:, 1]] = np.minimum(img[hp[:, 0], hp[:, 1]].astype(np.int64)
                                                 + int(900 + 25 * self.exposure_ms), 4095).astype(img.dtype)
        return img


@dataclass
class Rig:
    core: CoreServices
    profile: InstrumentProfile
    world: SampleWorld
    stage: SimMotionDevice = field(init=False)
    focus: SimMotionDevice = field(init=False)
    turret: SimTurret = field(init=False)
    light: SimIllumination = field(init=False)
    cameras: dict[CameraRole, SimOpticalCamera] = field(default_factory=dict)
    stage_map: VesselStageMap = field(default_factory=VesselStageMap)

    def stage_xy(self) -> tuple[float, float]:
        p = self.stage.read_position()
        return (p.get("x"), p.get("y")) if p else (0.0, 0.0)

    def focus_z(self) -> float:
        p = self.focus.read_position()
        return p.get("z") if p else 0.0

    def camera(self, role: CameraRole) -> SimOpticalCamera:
        return self.cameras[role]

    @property
    def device_ids(self) -> list[str]:
        return [self.stage.device_id, self.focus.device_id, self.turret.device_id, self.light.device_id,
                *(c.device_id for c in self.cameras.values())]


def attach_optical_simulator(core: CoreServices, profile: InstrumentProfile, vessel: Vessel, *,
                             fps: float = 12.0, fast: bool = False, connect: bool = True,
                             camera_factories: dict | None = None, illumination_factory=None) -> Rig:
    """`fast=True` shortens simulated latencies (tests). `camera_factories` maps a CameraRole to
    `factory(rig, camera_profile) -> backend`, replacing that role's simulated camera with a real one
    (ADR-0037); every other device stays simulated."""
    if profile.backend != "simulation":
        raise ValueError("attach_optical_simulator only accepts a simulation profile")
    rig = Rig(core, profile, SampleWorld(vessel, VesselStageMap(offset_x=TRUE_VESSEL_OFFSET_UM[0],
                                                               offset_y=TRUE_VESSEL_OFFSET_UM[1])))
    k = 0.1 if fast else 1.0
    rig.stage = sim_xy_stage(profile.stage_device, core.clock, speed_um_s=20_000.0 / k, min_move_s=0.08 * k)
    rig.focus = sim_focus(profile.focus_device, core.clock, speed_um_s=2_000.0 / k, min_move_s=0.03 * k)
    installed = [o.slot for o in profile.objectives if o.installed]
    rig.turret = SimTurret(profile.turret_device or "sim-turret", core.clock, seconds_per_slot=0.25 * k,
                           start=installed[0] if installed else 1)
    chans = [c.slot for c in profile.channels if c.installed]
    start_channel = "trans" if "trans" in chans else chans[0]
    if illumination_factory is not None:  # the real LED board (ADR-0048); same surface as SimIllumination
        rig.light = illumination_factory(core, profile, chans, start_channel)
    else:
        rig.light = SimIllumination(profile.illumination_device or "sim-light", core.clock, chans,
                                    carriage_s=0.45 * k, start_channel=start_channel)
    core.devices.register(rig.stage, rig.stage.capabilities(RIG_XY_LIMITS))
    core.devices.register(rig.focus, rig.focus.capabilities(RIG_Z_LIMITS))
    core.devices.register(rig.turret, rig.turret.capabilities(installed))
    core.devices.register(rig.light, rig.light.capabilities())
    for i, cam in enumerate(profile.cameras):
        factory = (camera_factories or {}).get(cam.role)
        dev = factory(rig, cam) if factory else SimOpticalCamera(rig, cam, fps=fps, seed=101 + i)
        rig.cameras[cam.role] = dev
        core.devices.register(dev, dev.capabilities())
    # start in focus-ish for a friendly first image: Z at the vessel focus nominal
    rig.focus.set_initial_position(z=vessel.focus_nominal_um + 15.0)  # simulator power-on state
    if vessel.regions:  # park over the region nearest the vessel centre so the first live image shows cells
        c = min(vessel.regions, key=lambda r: (r.cx - vessel.width_mm / 2) ** 2 + (r.cy - vessel.height_mm / 2) ** 2)
        rig.stage.set_initial_position(**dict(zip(("x", "y"), rig.stage_map.to_stage(c.cx, c.cy))))
    if connect:
        for d in rig.device_ids:
            core.devices.connect(d)
    return rig
