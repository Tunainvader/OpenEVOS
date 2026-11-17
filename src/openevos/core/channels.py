"""Imaging channel presets: which camera, LED, carriage slot and phase setting make a channel.

Presets are derived from what the illumination and camera devices report, so a real backend
with different cubes yields the matching channel list without code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openevos.hal.types import Role

if TYPE_CHECKING:
    from openevos.core.core_gateway import CoreGateway

TRANSMITTED = "transmitted"
FLUORESCENCE = "fluorescence"


@dataclass(frozen=True)
class ChannelPreset:
    name: str
    kind: str  # TRANSMITTED | FLUORESCENCE
    camera: Role
    led_id: str
    carriage_position: int
    phase_mode: str  # "open" | "matched" | "keep"
    default_exposure_ms: float
    default_intensity_pct: float
    default_gain_db: float = 0.0
    display_rgb: tuple[int, int, int] | None = None  # pseudo-colour for mono display


@dataclass
class ChannelSettings:
    exposure_ms: float
    intensity_pct: float
    gain_db: float


def wavelength_to_rgb(nm: float) -> tuple[int, int, int]:
    """Approximate display colour of a visible wavelength (380–780 nm)."""
    if nm < 440:
        r, g, b = (440 - nm) / 60, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, (510 - nm) / 20
    elif nm < 580:
        r, g, b = (nm - 510) / 70, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, (645 - nm) / 65, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0
    r, g, b = (max(0.0, min(1.0, v)) for v in (r, g, b))
    if nm > 700:  # far red / near-IR: show as magenta so it stays visible
        r, g, b = 1.0, 0.2, 0.7
    return int(r * 255), int(g * 255), int(b * 255)


def _centre_nm(spec: str | None) -> float | None:
    if not spec:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)", spec)
    return float(m.group(1)) if m else None


def build_presets(gw: CoreGateway) -> list[ChannelPreset]:
    if not gw.has(Role.ILLUMINATION):
        return []
    ill = gw.read(Role.ILLUMINATION)
    channels = ill.get_channels()
    carriage = ill.get_carriage_positions()
    mirror = next((p.index for p in carriage if p.label.lower() == "mirror"), None)
    has_color = gw.has(Role.CAMERA_COLOR)
    has_mono = gw.has(Role.CAMERA_MONO)
    trans_cam = Role.CAMERA_COLOR if has_color else Role.CAMERA_MONO
    presets: list[ChannelPreset] = []
    tl = next((c for c in channels if c.carriage_slot is None), None)
    if tl is not None and mirror is not None and (has_color or has_mono):
        presets.append(ChannelPreset("Brightfield", TRANSMITTED, trans_cam, tl.channel_id, mirror,
                                     "open", 20.0, 35.0))
        if gw.has(Role.PHASE_TURRET):
            presets.append(ChannelPreset("Phase contrast", TRANSMITTED, trans_cam, tl.channel_id,
                                         mirror, "matched", 25.0, 45.0))
        if has_color and has_mono:
            presets.append(ChannelPreset("Brightfield (mono)", TRANSMITTED, Role.CAMERA_MONO,
                                         tl.channel_id, mirror, "open", 20.0, 35.0))
    if has_mono:
        for c in channels:
            if c.carriage_slot is None:
                continue
            name = c.label.removesuffix(" LED")
            em = _centre_nm(c.emission_nm)
            presets.append(ChannelPreset(name, FLUORESCENCE, Role.CAMERA_MONO, c.channel_id,
                                         c.carriage_slot, "keep", 100.0, 50.0,
                                         display_rgb=wavelength_to_rgb(em) if em else None))
    return presets
