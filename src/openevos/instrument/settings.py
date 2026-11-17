"""Operator settings (Image > Settings Mono/Color, System > Basic, Quick Save, Hot buttons).

Manual basis: Mono settings p.19 (hot buttons, custom channel pseudo colours, highlight
saturated pixels, block condenser in fluorescence, underlying channel save, Capture All
selection, Quick Save); Color settings p.20 (hot buttons, brightness/contrast/saturation/hue,
reset, highlight saturated, Quick Save); hot-button actions p.21; System > Basic p.40
(live gesture, scale bar, getting-started button, time-lapse retract objective, Auto Fine
range, temperature offset, gas connections, O2 calibration).

The Light slider mapping below is an OpenEVOS design (one control that walks LED intensity, then
exposure, then gain). It is not the vendor algorithm; Actual Mode exposes the three separately.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openevos.instrument.profile import CameraRole


class HotButtonAction(str, Enum):
    NONE = "none"
    CAPTURE = "capture"
    CAPTURE_ALL = "capture_all"
    TRANSFECTION = "transfection"
    SAVE = "save"
    ZSTACK = "zstack"
    CAPTURE_FOCUS_NOMINAL = "capture_focus_nominal"
    QUICK_SAVE = "quick_save"

    @property
    def label(self) -> str:
        return {
            "none": "—", "capture": "Capture", "capture_all": "Capture All", "transfection": "Transfection",
            "save": "Save", "zstack": "Z-Stack", "capture_focus_nominal": "Capture Focus Nominal",
            "quick_save": "Quick Save",
        }[self.value]


class ImageFormat(str, Enum):
    TIFF = "tiff"
    PNG = "png"
    JPEG = "jpeg"
    BMP = "bmp"

    @property
    def suffix(self) -> str:
        return {"tiff": ".tif", "png": ".png", "jpeg": ".jpg", "bmp": ".bmp"}[self.value]


class QuickSaveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_name: str = "Image"
    folder: str = ""  # empty => <user documents>/OpenEVOS/Captures
    image_format: ImageFormat = ImageFormat.TIFF
    save_as_color: bool = True  # composite/colour 8-bit vs raw monochrome
    save_underlying: bool = False
    include_annotations: bool = True

    @field_validator("base_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c in v for c in '\\/:*?"<>|'):
            raise ValueError("base name must be non-empty and contain no path characters")
        return v


class ChannelSettings(BaseModel):
    """Per-channel light/camera state (Actual Mode values) plus the combined Light slider."""

    model_config = ConfigDict(extra="forbid")

    light_slider: float = Field(default=40.0, ge=0, le=100)
    led_percent: float = Field(default=40.0, ge=0, le=100)
    exposure_ms: float = Field(default=50.0, gt=0, le=60_000)
    gain: float = Field(default=1.0, ge=1, le=64)
    pseudocolor: str | None = None  # None => profile default

    def apply_slider(self, value: float, fluorescence: bool) -> ChannelSettings:
        led, exp, gain = light_slider_split(value, fluorescence)
        return self.model_copy(update={"light_slider": value, "led_percent": led, "exposure_ms": exp, "gain": gain})


def light_slider_split(value: float, fluorescence: bool) -> tuple[float, float, float]:
    """0..100 -> (LED %, exposure ms, gain). 0-60: LED 0..100 % at base exposure (0 = zero intensity: the light
    stays switched on but dark, so sliding up brightens it again); 60-90: exposure to its ceiling; 90-100: gain to
    8x. Fluorescence starts at a longer base exposure."""
    v = min(100.0, max(0.0, float(value)))
    base, ceiling = (40.0, 1000.0) if fluorescence else (5.0, 100.0)
    if v <= 60:
        return round(100 * v / 60, 2), base, 1.0
    if v <= 90:
        f = (v - 60) / 30
        return 100.0, round(base * (ceiling / base) ** f, 2), 1.0
    return 100.0, ceiling, round(1 + 7 * (v - 90) / 10, 2)


def _pad_hot_buttons(v: list[HotButtonAction]) -> list[HotButtonAction]:
    if len(v) > 4:
        raise ValueError("at most four hot buttons (manual p.21)")
    return list(v) + [HotButtonAction.NONE] * (4 - len(v))


class MonoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hot_buttons: list[HotButtonAction] = Field(default_factory=lambda: [
        HotButtonAction.CAPTURE, HotButtonAction.CAPTURE_ALL, HotButtonAction.ZSTACK, HotButtonAction.SAVE])
    channels: dict[str, ChannelSettings] = Field(default_factory=dict)  # by slot
    pseudocolor_enabled: bool = True  # "Apply Pseudo Color" (Light & Camera dialog)
    highlight_saturated: bool = False
    block_condenser_in_fluorescence: bool = False  # needs condenser motion; locked on hardware for now
    underlying_channel_save: str = "16bit_mono"  # "8bit_color" | "16bit_mono"
    capture_all: list[str] = Field(default_factory=list)  # slots; empty => every installed fluorescence cube
    quick_save: QuickSaveSettings = Field(default_factory=QuickSaveSettings)

    @field_validator("hot_buttons")
    @classmethod
    def _four(cls, v: list[HotButtonAction]) -> list[HotButtonAction]:
        return _pad_hot_buttons(v)

    @field_validator("underlying_channel_save")
    @classmethod
    def _ucs(cls, v: str) -> str:
        if v not in ("8bit_color", "16bit_mono"):
            raise ValueError("underlying channel save must be 8bit_color or 16bit_mono")
        return v


class ColorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hot_buttons: list[HotButtonAction] = Field(default_factory=lambda: [
        HotButtonAction.CAPTURE, HotButtonAction.QUICK_SAVE, HotButtonAction.SAVE, HotButtonAction.NONE])
    brightness: float = Field(default=0.0, ge=-100, le=100)
    contrast: float = Field(default=0.0, ge=-100, le=100)
    saturation: float = Field(default=0.0, ge=-100, le=100)
    hue: float = Field(default=0.0, ge=-180, le=180)
    channels: dict[str, ChannelSettings] = Field(default_factory=dict)
    highlight_saturated: bool = False
    quick_save: QuickSaveSettings = Field(default_factory=lambda: QuickSaveSettings(base_name="Color"))

    @field_validator("hot_buttons")
    @classmethod
    def _four(cls, v: list[HotButtonAction]) -> list[HotButtonAction]:
        return _pad_hot_buttons(v)

    def reset_color(self) -> ColorSettings:
        return self.model_copy(update={"brightness": 0.0, "contrast": 0.0, "saturation": 0.0, "hue": 0.0})


class GasPort1(str, Enum):
    PREMIX = "premix"
    AIR = "air"
    CUSTOM = "custom"


class GasConnections(BaseModel):
    """Onstage incubator gas ports (manual p.69): port 1 premix/air/custom, port 2 N2, port 3 CO2."""

    model_config = ConfigDict(extra="forbid")

    port1: GasPort1 = GasPort1.AIR
    port1_custom_co2_pct: float | None = Field(default=None, ge=0, le=20)
    port2_n2_connected: bool = False
    port3_co2_connected: bool = False


class SystemBasicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live_gesture: str = "expand"  # "expand" | "pinch" — manual: live expand/pinch gesture preference
    show_scale_bar: bool = True
    show_getting_started: bool = False
    timelapse_retract_objective: bool = False
    auto_fine_range_percent: float = Field(default=100.0, ge=25, le=400)
    temperature_offset_c: float = Field(default=0.0, ge=-5, le=5)
    gas: GasConnections = Field(default_factory=GasConnections)
    # OpenEVOS interface preferences (Craftimon conventions)
    theme: str = "dark"  # "dark" | "light" | "system"
    navigation: str = "auto"  # "auto" (rail below 1360 px) | "expanded" | "rail"
    scientific_display: bool = True  # linear display transforms only; display log saved with images
    #: Capture › Histogram toolbar toggle: the levels histogram and Auto range under the live image
    show_histogram: bool = True
    #: View › Developer tools: simulator fault injection and gate-evidence tables on the Hardware tab (UI audit)
    developer_mode: bool = False

    @field_validator("theme")
    @classmethod
    def _theme(cls, v: str) -> str:
        if v not in ("dark", "light", "system"):
            raise ValueError("theme must be dark, light or system")
        return v

    @field_validator("navigation")
    @classmethod
    def _nav(cls, v: str) -> str:
        if v not in ("auto", "expanded", "rail"):
            raise ValueError("navigation must be auto, expanded or rail")
        return v


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mono: MonoSettings = Field(default_factory=MonoSettings)
    color: ColorSettings = Field(default_factory=ColorSettings)
    basic: SystemBasicSettings = Field(default_factory=SystemBasicSettings)
    last_vessel_id: str = "plate-96"
    active_camera: CameraRole = CameraRole.MONO

    def camera_settings(self, role: CameraRole) -> MonoSettings | ColorSettings:
        return self.mono if role is CameraRole.MONO else self.color

    def channel(self, role: CameraRole, slot: str) -> ChannelSettings:
        cs = self.camera_settings(role).channels
        if slot not in cs:
            fluo = slot.startswith("cube")
            cs[slot] = ChannelSettings().apply_slider(45.0 if fluo else 30.0, fluo)
        return cs[slot]

    # ------------------------------------------------------------------ persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2), "utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> AppSettings:
        if not path.exists():
            return cls()
        return cls.model_validate(json.loads(path.read_text("utf-8")))


def default_settings_path() -> Path:
    from platformdirs import user_config_dir

    return Path(user_config_dir("OpenEVOS", appauthor=False)) / "ui_settings.json"
