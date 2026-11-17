"""The simulated instrument profile.

The *shape* follows what the FL Auto 1.6 vendor software expects (docs/evidence/
vendor_software_analysis.md): a mono 1296×964 CCD camera, a colour camera, six motor axes
(X, Y, Z, epi carriage with four cube slots plus a mirror, a 5-position objective turret and a
phase turret), LED channels and a separate incubator. None of that is verified on our unit.

Every **number** below (travel, speeds, objectives, phase rings, exposure ranges, LED wavelengths,
incubator ranges) is a simulation default chosen to look plausible. None of it is instrument
data or calibration, and none of it may be copied into a real backend profile.
"""

from __future__ import annotations

from dataclasses import dataclass

from openevos.hal.types import LedChannel, ObjectiveInfo


@dataclass(frozen=True)
class CameraModel:
    role: str
    display_name: str
    sensor: str
    width: int
    height: int
    pixel_size_um: float
    bit_depth: int
    color: bool
    exposure_range_ms: tuple[float, float]
    gain_range_db: tuple[float, float]
    max_fps: float
    e_per_adu: float  # electrons per digital number at 0 dB
    read_noise_e: float
    offset_adu: float
    default_exposure_ms: float
    notes: str = ""
    #: the optics render the noise-free image at 1/render_binning resolution and upsample it,
    #: which keeps live view responsive; noise is added at full resolution for mono cameras
    render_binning: int = 2


MONO_CAMERA = CameraModel(
    role="camera.mono", display_name="Mono camera", sensor="simulated 1/3\" interline CCD",
    width=1296, height=964, pixel_size_um=3.75, bit_depth=12, color=False,
    exposure_range_ms=(0.05, 5000.0), gain_range_db=(0.0, 24.0), max_fps=15.0,
    e_per_adu=2.5, read_noise_e=10.0, offset_adu=60.0, default_exposure_ms=20.0,
    notes="Geometry mirrors the vendor software's expected 1296×964 CCD; values are simulated.",
)


def color_camera(width: int = 2048, height: int = 1536) -> CameraModel:
    if not (64 <= width <= 8192 and 64 <= height <= 8192):
        raise ValueError("colour camera resolution must be within 64..8192 px per side")
    return CameraModel(
        role="camera.color", display_name="Colour camera", sensor="simulated colour CMOS",
        width=width, height=height, pixel_size_um=3.2, bit_depth=8, color=True,
        exposure_range_ms=(0.05, 2000.0), gain_range_db=(0.0, 12.0), max_fps=12.0,
        e_per_adu=40.0, read_noise_e=12.0, offset_adu=2.0, default_exposure_ms=15.0,
        notes="Resolution is configurable; the real colour camera's resolution is unverified.",
    )


DEFAULT_OBJECTIVES: tuple[ObjectiveInfo, ...] = (
    ObjectiveInfo(1, "2× / 0.06", 2.0, 0.06, None, -40.0),
    ObjectiveInfo(2, "4× / 0.13 Ph0", 4.0, 0.13, "Ph0", -18.0),
    ObjectiveInfo(3, "10× / 0.30 Ph1", 10.0, 0.30, "Ph1", 0.0),
    ObjectiveInfo(4, "20× / 0.45 Ph1", 20.0, 0.45, "Ph1", 6.0),
    ObjectiveInfo(5, "40× / 0.65 Ph2", 40.0, 0.65, "Ph2", 11.0),
)

DEFAULT_PHASE_POSITIONS: tuple[str, ...] = ("Open", "Ph0", "Ph1", "Ph2")


@dataclass(frozen=True)
class CubeModel:
    slot: int
    name: str
    excitation_nm: str
    emission_nm: str
    fluor: str  # which simulated label it excites: "dapi" | "gfp" | "rfp" | "cy5"
    display_rgb: tuple[float, float, float]


DEFAULT_CUBES: tuple[CubeModel, ...] = (
    CubeModel(1, "DAPI", "357/44", "447/60", "dapi", (0.25, 0.4, 1.0)),
    CubeModel(2, "GFP", "470/22", "510/42", "gfp", (0.2, 1.0, 0.3)),
    CubeModel(3, "RFP", "531/40", "593/40", "rfp", (1.0, 0.35, 0.15)),
    CubeModel(4, "Cy5", "628/40", "692/40", "cy5", (0.75, 0.1, 0.35)),
)

MIRROR_LABEL = "Mirror"
TRANSMITTED_LED = "TL"


def led_channels(cubes: tuple[CubeModel, ...]) -> list[LedChannel]:
    chans = [LedChannel(TRANSMITTED_LED, "Transmitted (white)", None)]
    for c in cubes:
        chans.append(LedChannel(f"CUBE{c.slot}", f"{c.name} LED", c.slot, c.excitation_nm,
                                c.emission_nm))
    return chans


@dataclass(frozen=True)
class SimulationConfig:
    seed: int = 2026
    time_scale: float = 1.0  # 1.0 = real time, 0.0 = instant motion/exposure
    color_resolution: tuple[int, int] = (2048, 1536)
    start_homed: bool = True
    # XY stage
    x_limits_um: tuple[float, float] = (0.0, 110000.0)
    y_limits_um: tuple[float, float] = (0.0, 75000.0)
    xy_start_um: tuple[float, float] = (55000.0, 37500.0)
    xy_speed_um_s: float = 20000.0
    # focus
    z_limits_um: tuple[float, float] = (0.0, 9000.0)
    z_speed_um_s: float = 3000.0
    specimen_z_um: float = 4200.0  # Z at which the 10× objective is in focus
    # turrets / carriage
    objectives: tuple[ObjectiveInfo, ...] = DEFAULT_OBJECTIVES
    start_objective: int = 3
    phase_positions: tuple[str, ...] = DEFAULT_PHASE_POSITIONS
    cubes: tuple[CubeModel, ...] = DEFAULT_CUBES
    # specimen
    cell_density_per_mm2: float = 300.0
    # incubator
    incubator_time_acceleration: float = 1.0  # >1 makes temperature/gas dynamics faster
    ambient_temperature_c: float = 23.5

    @property
    def start_z_um(self) -> float:
        obj = next(o for o in self.objectives if o.position == self.start_objective)
        return self.specimen_z_um + obj.parfocal_offset_um
