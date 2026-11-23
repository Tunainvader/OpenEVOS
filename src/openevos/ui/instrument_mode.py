"""What is real and what is simulated in this session, read from each device's own backend.

Impeccable critique 2026-11-24 (P1): on the real instrument the header chip read "SIM + PHYSICAL X/Y STAGE" and its
tooltip said "Cameras, light, focus and turret are simulators; frames are synthetic", while the cameras, the LED board
and all five motor drives were physical (MCP readout the same day). The header decided from three session flags; the
real-instrument launch builds on the simulator profile and swaps the real devices in one by one, so the flags and the
profile name ("Simulated EVOS FL Auto") no longer said what was real. This asks every device the profile uses (stage,
focus, turret, light, cameras) for its backend, as the MCP link already does (mcp_link/view.py), and words the
window title, header subtitle, mode chip, footer and About box from that one answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openevos.core.models import BackendKind
from openevos.instrument.profile import CameraRole


@dataclass(frozen=True)
class Part:
    title: str  # "X/Y stage", "focus": mid-sentence form
    short: str  # "STAGE", for the chip
    device_id: str
    real: bool | None  # None: the profile names a device that is not registered


@dataclass(frozen=True)
class InstrumentMode:
    parts: tuple[Part, ...]
    badge: str
    state: str  # Badge state: "warn" when anything real can move or light up, else "simulated"
    footer: str
    tooltip: str
    name: str  # the instrument, as the title bar and header name it
    about: str

    @property
    def real(self) -> tuple[Part, ...]:
        return tuple(p for p in self.parts if p.real)

    @property
    def simulated(self) -> tuple[Part, ...]:
        return tuple(p for p in self.parts if not p.real)


def parts(session) -> tuple[Part, ...]:
    """Every device the profile uses for a role (stage, focus, turret, light, cameras), with whether it is real."""
    p = session.profile
    rows = [("X/Y stage", "STAGE", p.stage_device), ("focus", "FOCUS", p.focus_device),
            ("objective turret", "TURRET", p.turret_device), ("light", "LIGHT", p.illumination_device)]
    for role, title, short in ((CameraRole.MONO, "mono camera", "MONO CAM"),
                               (CameraRole.COLOR, "colour camera", "COLOUR CAM")):
        cam = p.camera(role)
        if cam is not None:
            rows.append((title, short, cam.device_id))
    out = []
    for title, short, did in rows:
        if not did:
            continue
        snap = session.core.store.get(did)
        out.append(Part(title, short, did, None if snap is None else snap.backend_kind is BackendKind.PHYSICAL))
    return tuple(out)


def _shorts(parts: tuple[Part, ...]) -> str:
    names = [p.short for p in parts]
    if "MONO CAM" in names and "COLOUR CAM" in names:  # both cameras on the same side: one word
        names = [n for n in names if n != "COLOUR CAM"]
        names[names.index("MONO CAM")] = "CAMERAS"
    return ", ".join(names)


def _titles(parts: tuple[Part, ...]) -> str:
    return ", ".join(p.title for p in parts)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def instrument_mode(session) -> InstrumentMode:
    found = parts(session)
    real = tuple(p for p in found if p.real)
    sim = tuple(p for p in found if not p.real)
    services = list(getattr(session, "telemetry", None) or [])
    emulated = any(getattr(s, "emulated", False) for s in services)
    base = re.sub(r"^simulated\s+", "", session.profile.name, flags=re.IGNORECASE)

    if not real:
        state, name = "simulated", f"Simulated {base}"
        if emulated:
            badge, footer = "SIMULATION + EMULATED DRIVES", "SIM+EMULATED DRIVES"
            about = "Simulation, with emulated motor drives: no physical hardware is commanded."
        elif services:
            badge, footer = "SIMULATION + MOTOR TELEMETRY", "SIM+TELEMETRY"
            about = "Simulation: the real motor drives are read for display; nothing is sent to them."
        else:
            badge, footer = "SIMULATION", "SIM"
            about = "Simulation: no physical hardware is commanded."
    elif not sim:
        state, name, badge, footer = "warn", base, "REAL INSTRUMENT", "REAL"
        about = f"Real instrument: {_titles(real)} are the physical devices."
    else:
        state, name, footer = "warn", f"{base}, partly simulated", "MIXED"
        # name the smaller side, so the chip stays short and the exception is what it shows
        badge = (f"REAL INSTRUMENT · SIM {_shorts(sim)}" if len(sim) <= len(real)
                 else f"SIMULATION · REAL {_shorts(real)}")
        about = f"Real: {_titles(real)}. Simulated: {_titles(sim)}."

    lines = ["Read from each device:"]
    # no device ids here: the real instrument's cameras keep the simulator's ids ("sim-camera"), and "real
    # (sim-camera)" would undo the point; the Instrument page lists the ids
    lines += [f"  {_cap(p.title)}: {'real' if p.real else 'simulator' if p.real is False else 'not registered'}"
              for p in found]
    notes = []
    stage = next((p for p in found if p.short == "STAGE"), None)
    if stage is not None and stage.real:
        svc = next((s for s in services if getattr(s, "motion", False)), None)
        box = getattr(svc, "travel_box_note", "power-up box")
        notes.append(f"The joystick, jogs and Stop move the real stage, inside the {box} and through the safety "
                     "checks.")
    if any(p.real for p in found if p.short == "LIGHT"):
        notes.append("The light is the real LED board: a channel lit here is real excitation light.")
    if any(p.real is False for p in found if p.short.endswith("CAM")):
        notes.append("A simulated camera gives synthetic frames, labelled SIMULATED on the image.")
    if emulated:
        notes.append("The motor drives are an emulator (--motor-telemetry sim): nothing physical moves.")
    elif services and not real:
        notes.append("The real motor drives are read live for display only; nothing is sent to them.")
    tooltip = "\n".join(lines + ([""] + notes if notes else []))
    return InstrumentMode(found, badge, state, footer, tooltip, name, about)
