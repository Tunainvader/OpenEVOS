"""Qt-free readback of the light and camera devices for the UI.

The Capture page keeps the operator's *intent* (per-channel settings). This module reads what the devices
actually report, so controls can show device state next to (or instead of) the stored settings:

* limits come from the capability record the router validates against (`core.devices`), so the UI never
  offers a value the safety path would reject (e.g. the uEye colour camera accepts gain 1x only);
* state comes from the registered backend's public attributes plus the runtime state store.

Nothing here sends a command. Every change still goes through `InstrumentController` -> `CommandRouter`.
Backends are read duck-typed (simulated rig camera: `gain`; FlyCapture2/uEye: `gain_db`), and a missing
attribute reads as unknown (None), never as a made-up value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from openevos.instrument.profile import CameraRole

#: the UI's gain factor never goes below unity (EVOS "gain 1x"), whatever a backend reports
MIN_GAIN = 1.0


def _db_to_factor(db: float) -> float:
    return 10.0 ** (db / 20.0)


@dataclass(frozen=True)
class CameraLimits:
    exposure_ms: tuple[float, float]
    gain: tuple[float, float]  # linear factor

    @property
    def gain_fixed(self) -> bool:
        return self.gain[1] - self.gain[0] < 1e-3

    def clamp_exposure(self, ms: float) -> float:
        return min(self.exposure_ms[1], max(self.exposure_ms[0], ms))

    def clamp_gain(self, g: float) -> float:
        return min(self.gain[1], max(self.gain[0], g))


@dataclass(frozen=True)
class CameraReadback:
    role: CameraRole
    device_id: str
    connection: str
    activity: str
    exposure_ms: float | None
    gain: float | None  # linear factor
    streaming: bool
    acquiring: bool  # a scientific acquisition owns the stream
    frames: int | None
    width: int | None
    height: int | None
    synthetic: bool

    def summary(self) -> str:
        exp = f"{self.exposure_ms:g} ms" if self.exposure_ms is not None else "exposure ?"
        gain = f"{self.gain:.2f}×" if self.gain is not None else "gain ?"
        state = "ACQUIRING" if self.acquiring else "LIVE" if self.streaming else "STOPPED"
        return f"{exp} · {gain} · {state}"


@dataclass(frozen=True)
class LightReadback:
    device_id: str
    connection: str
    activity: str
    channel: str | None
    enabled: bool | None
    intensity_percent: float | None
    carriage_moving: bool
    emitting: bool | None

    def summary(self, display: str | None = None) -> str:
        ch = display or self.channel or "?"
        if self.enabled is None:
            return f"{ch} · state unknown"
        if self.carriage_moving:
            return f"{ch} · carriage moving (dark)"
        pct = f"{self.intensity_percent:g} %" if self.intensity_percent is not None else "? %"
        return f"{ch} · {'ON' if self.enabled else 'OFF'} {pct}"


def _record(session, device_id: str | None):
    return session.core.devices.get(device_id) if device_id else None


def _snap_fields(session, device_id: str) -> tuple[str, str]:
    s = session.core.store.get(device_id)
    if s is None:
        return "unknown", "unknown"
    return s.connection.value, s.activity.value


def camera_limits(session, role: CameraRole) -> CameraLimits | None:
    cam = session.profile.camera(role)
    rec = _record(session, cam.device_id if cam else None)
    if rec is None:
        return None
    caps = rec.capabilities
    exp = caps.range("exposure_ms")
    gdb = caps.range("gain_db")
    exp_t = (exp.minimum, exp.maximum) if exp else (0.1, 60_000.0)
    if gdb is None:
        gain_t = (MIN_GAIN, MIN_GAIN)  # no gain range declared: the router would reject any change
    else:
        lo = max(MIN_GAIN, _db_to_factor(max(0.0, gdb.minimum)))
        hi = max(lo, _db_to_factor(gdb.maximum))
        # the simulator declares its max with a +1e-9 dB margin; show a clean number
        gain_t = (round(lo, 3), math.floor(hi * 1000 + 1e-6) / 1000)
    return CameraLimits(exposure_ms=exp_t, gain=gain_t)


def light_limits(session, channel: str) -> tuple[float, float] | None:
    rec = _record(session, session.profile.illumination_device)
    if rec is None:
        return None
    r = rec.capabilities.range(f"intensity:{channel}")
    return (r.minimum, r.maximum) if r else None


def light_channels(session) -> tuple[str, ...]:
    rec = _record(session, session.profile.illumination_device)
    return tuple(rec.capabilities.illumination_channels) if rec else ()


def read_camera(session, role: CameraRole) -> CameraReadback | None:
    cam = session.profile.camera(role)
    rec = _record(session, cam.device_id if cam else None)
    if rec is None:
        return None
    b = rec.backend
    gain = getattr(b, "gain", None)
    if not isinstance(gain, (int, float)):
        gdb = getattr(b, "gain_db", None)
        gain = _db_to_factor(gdb) if isinstance(gdb, (int, float)) else None
    exp = getattr(b, "exposure_ms", None)
    conn, act = _snap_fields(session, cam.device_id)
    frames = getattr(b, "frames_produced", None)
    w, h = getattr(b, "width", None), getattr(b, "height", None)
    return CameraReadback(
        role=role, device_id=cam.device_id, connection=conn, activity=act,
        exposure_ms=float(exp) if isinstance(exp, (int, float)) else None,
        gain=float(gain) if gain is not None else None,
        streaming=bool(getattr(b, "streaming", False)),
        acquiring=getattr(b, "current_acquisition", None) is not None,
        frames=frames if isinstance(frames, int) else None,
        width=w if isinstance(w, int) else None, height=h if isinstance(h, int) else None,
        synthetic=b.backend_kind.value == "simulated")


def read_light(session) -> LightReadback | None:
    did = session.profile.illumination_device
    rec = _record(session, did)
    if rec is None:
        return None
    b = rec.backend
    conn, act = _snap_fields(session, did)
    en = getattr(b, "enabled", None)
    pct = getattr(b, "intensity_percent", None)
    emitting = getattr(b, "emitting", None)
    return LightReadback(
        device_id=did, connection=conn, activity=act, channel=getattr(b, "channel", None),
        enabled=bool(en) if isinstance(en, bool) else None,
        intensity_percent=float(pct) if isinstance(pct, (int, float)) else None,
        carriage_moving=bool(getattr(b, "busy", False)) or act == "moving",
        emitting=bool(emitting) if isinstance(emitting, bool) else None)
