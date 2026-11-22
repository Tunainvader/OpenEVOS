"""Session: one object holding the core, profile, controller and workflow services for a UI or a
script. `build_sim_session()` wires the optics simulator; a physical session will be built the same
way once verified backends exist (and not before).
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from openevos.acquisition.autofocus import Autofocus
from openevos.acquisition.calibration import CalibrationStore
from openevos.acquisition.capture import CaptureService
from openevos.acquisition.controller import InstrumentController
from openevos.core.application import CoreServices, build_core
from openevos.core.clock import Clock
from openevos.core.device_manager import OwnershipRegistry
from openevos.core.models import utcnow
from openevos.instrument.profile import InstrumentProfile, load_profile
from openevos.instrument.settings import AppSettings
from openevos.instrument.vessels import (
    PlateAlignmentError,
    Vessel,
    VesselLibrary,
    VesselStageMap,
    solve_plate_alignment,
)

ALIGNMENT_ATTEMPTS_FILE = "plate-alignment-attempts.jsonl"


@dataclass
class Session:
    core: CoreServices
    profile: InstrumentProfile
    settings: AppSettings
    vessels: VesselLibrary
    vessel: Vessel
    stage_map: VesselStageMap
    controller: InstrumentController
    capture: CaptureService
    autofocus: Autofocus
    rig: object | None = None  # simulator rig (simulation only)
    settings_path: Path | None = None
    listeners: list = field(default_factory=list)
    calibration: CalibrationStore = field(default_factory=CalibrationStore)
    nominal_map: VesselStageMap = field(default_factory=VesselStageMap)
    #: physical motor telemetry services (ADR-0023) and whether the real X/Y stage is under control (ADR-0024)
    telemetry: list = field(default_factory=list)
    physical_stage: bool = False
    physical_audit: list = field(default_factory=list)  # lifecycle of every motor.* command
    #: the router's turret-vs-focus interlock height, when the turret does not retract Z itself (audit APP-01)
    turret_safe_z_um: float | None = None

    def __post_init__(self) -> None:
        self.nominal_map = self.stage_map
        self.capture.defects_for = lambda role: (self.calibration.hot_pixels.get(role.value, [])
                                                 if self.calibration.hot_pixel_correction else [])
        self.capture.pixel_size_for = lambda role, slot: self.calibration.pixel_size_um.get(role.value, {}).get(slot)
        self._apply_vessel_offset()

    # ------------------------------------------------------------------ calibration application
    def _apply_vessel_offset(self) -> None:
        if self.physical_stage:
            # the real stage: the operator's plate alignment, one for every standard-footprint vessel; without it
            # the map is unverified and the Capture page shows no position and moves nothing
            a = self.calibration.plate_alignment
            self.stage_map = VesselStageMap(
                stage_frame=self.profile.stage_device, offset_x=a["offset_x"], offset_y=a["offset_y"],
                sign_x=a["sign_x"], sign_y=a["sign_y"], verified=True) if a else self.nominal_map.model_copy(
                update={"verified": False})
            return
        off = self.calibration.vessel_offset_um.get(self.vessel.vessel_id)
        self.stage_map = self.nominal_map.model_copy(update={
            "offset_x": off[0] if off else 0.0, "offset_y": off[1] if off else 0.0, "verified": bool(off)})

    def pixel_size_um(self, role, slot: int | None) -> float | None:
        if slot is None:
            return None
        measured = self.calibration.pixel_size_um.get(role.value, {}).get(slot)
        if measured:
            return measured
        cam, obj = self.profile.camera(role), self.profile.objective(slot)
        return cam.pixel_size_um / obj.magnification if cam and obj and cam.pixel_size_um and obj.magnification else None  # noqa: E501

    #: F carriage counts: the mirror routes the image to the colour camera (Motor Gate 12, 2026-11-15, Strongly
    #: supported: at any cube slot the colour camera reads 0, at the mirror it saturates under a flashlight)
    F_MIRROR_COUNTS = 128000
    F_AT_TOLERANCE = 200

    def route_light_path(self, role) -> str:
        """Put the F carriage where `role`'s camera sees the sample (operator, 2026-11-18: "no image colour cam"):
        the colour camera needs F at the mirror; the mono camera gets F back to slot 1 when it is at the mirror (a
        cube channel then moves F to its own cube as before, ADR-0051). Only with real motor control and carriage
        control, through the stage service's own F move (Gates 11/12); refused while an epi light is on. Blocking:
        call it off the UI thread. Returns what happened ('' = nothing to do)."""
        from openevos.instrument.profile import CameraRole

        svc = next((s for s in self.telemetry if getattr(s, "carriage_control", False)), None)
        if svc is None:
            return ""
        snap = self.core.store.get("motor.cube_carriage")
        pos = snap.observed_position.values[0] if snap is not None and snap.observed_position else None
        if pos is None:
            return "filter carriage position unknown; not moved"
        at_mirror = abs(pos - self.F_MIRROR_COUNTS) <= self.F_AT_TOLERANCE
        if role is CameraRole.COLOR and not at_mirror:
            dest = "mirror"
        elif role is CameraRole.MONO and at_mirror:
            dest = "slot1"
        else:
            return ""
        light = self.controller.light
        ch = self.profile.channel(light.channel) if light.channel else None
        if light.enabled and ch is not None and ch.kind.value == "fluorescence":
            return "an epi light is on; filter carriage not moved"
        ok, msg = svc.move_carriage(dest)
        return f"filter carriage to {dest} for the {role.value} camera" if ok else f"filter carriage not moved: {msg}"

    def change_objective(self, slot: int):
        """Turret command; once verified, apply calibrated parfocal (Z) and parcentric (XY) compensation so the
        same sample point stays centred and in focus (what the original software does after calibration)."""
        ctl = self.controller
        prev = ctl.objective_slot()
        # a turret that does not lower Z itself (the simulator): the controller lowers Z for the turn and puts it back
        # (audit APP-01); the real turret service retracts Z itself (ADR-0053)
        ticket = ctl.set_objective(slot)
        cal = self.calibration
        dz = (cal.parfocal_um.get(slot, 0.0) - cal.parfocal_um.get(prev, 0.0)) if prev in cal.parfocal_um and \
            slot in cal.parfocal_um else 0.0
        a, b = cal.parcentric_um.get(prev), cal.parcentric_um.get(slot)
        dxy = (a[0] - b[0], a[1] - b[1]) if a is not None and b is not None else (0.0, 0.0)
        if abs(dz) < 0.05 and abs(dxy[0]) < 0.05 and abs(dxy[1]) < 0.05:
            return ticket

        def compensate() -> None:
            from openevos.core.models import CommandStatus

            if ticket.wait(30) is not CommandStatus.SUCCEEDED:
                return
            z, xy = ctl.focus_z(), ctl.stage_xy()
            if z is not None and abs(dz) >= 0.05:
                ctl.move_focus(z + dz).wait(30)
            if xy is not None and (abs(dxy[0]) >= 0.05 or abs(dxy[1]) >= 0.05):
                ctl.move_stage(xy[0] + dxy[0], xy[1] + dxy[1]).wait(30)

        threading.Thread(target=compensate, daemon=True, name="objective-compensation").start()
        return ticket

    @property
    def simulated(self) -> bool:
        return self.profile.backend == "simulation"

    def set_plate_alignment(self, wells: list[tuple[str, tuple[float, float], tuple[float, float]]]) -> float:
        """Two wells centred on the physical stage: [(region_id, vessel mm, stage µm)] x2. Solves, saves to the
        calibration file and applies it at once. Returns the residual (µm); raises PlateAlignmentError."""
        (_, v1, s1), (_, v2, s2) = wells
        try:
            m, residual = solve_plate_alignment(v1, s1, v2, s2, self.profile.stage_device)
        except PlateAlignmentError as exc:
            self._record_alignment_attempt(wells, "refused", str(exc))
            raise
        self._record_alignment_attempt(wells, "saved", f"residual {residual:.0f} um", sign_x=m.sign_x,
                                       sign_y=m.sign_y)
        self.calibration.plate_alignment = {
            "sign_x": m.sign_x, "sign_y": m.sign_y, "offset_x": round(m.offset_x, 1), "offset_y": round(m.offset_y, 1),
            "residual_um": round(residual, 1), "vessel_id": self.vessel.vessel_id,
            "wells": [{"region": r, "vessel_mm": list(v), "stage_um": [round(s[0], 1), round(s[1], 1)]}
                      for r, v, s in wells]}
        self.calibration.touch("plate_alignment")
        self._apply_vessel_offset()
        for fn in list(self.listeners):
            fn("vessel")
        return residual

    def _record_alignment_attempt(self, wells, outcome: str, message: str, **extra) -> None:
        """Every alignment attempt, saved or refused, as one JSON line next to calibration.json (2026-11-21: two
        refusals were only in the on-screen log and were lost)."""
        if self.calibration.path is None:
            return
        row = {"utc": utcnow().isoformat(timespec="seconds"), "outcome": outcome, "message": message,
               "vessel_id": self.vessel.vessel_id,
               "wells": [{"region": r, "vessel_mm": list(v), "stage_um": [round(s[0], 1), round(s[1], 1)]}
                         for r, v, s in wells], **extra}
        try:
            with open(self.calibration.path.with_name(ALIGNMENT_ATTEMPTS_FILE), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass  # the record is a convenience; the alignment itself is saved (or refused) regardless

    def select_vessel(self, vessel_id: str) -> Vessel:
        self.vessel = self.vessels.get(vessel_id)
        self.settings.last_vessel_id = vessel_id
        # written at once (operator 2026-11-22: "the plate selection is not saved on quitting"): it used to be saved
        # only on a clean close, and the 22:28 run that day ended without one
        with contextlib.suppress(OSError):  # the window's autosave retries a failed write
            self.save_settings()
        self._apply_vessel_offset()
        if self.rig is not None:
            self.rig.world.set_vessel(self.vessel)
        for fn in list(self.listeners):
            fn("vessel")
        return self.vessel

    def capture_focus_nominal(self) -> float:
        z = self.controller.focus_z()
        if z is None:
            raise RuntimeError("focus position unknown")
        return self.set_focus_nominal(z)

    def set_focus_nominal(self, z: float) -> float:
        """The vessel's focus nominal (also the undo of Capture focus nominal, audit APP-21)."""
        self.vessel = self.vessels.with_focus_nominal(self.vessel.vessel_id, z)
        if self.rig is not None:
            self.rig.world.vessel = self.vessel  # sample unchanged; nominal only
        for fn in list(self.listeners):
            fn("vessel")
        return z

    def save_settings(self) -> None:
        if self.settings_path is not None:
            self.settings.save(self.settings_path)

    def shutdown(self) -> None:
        for svc in list(self.telemetry):  # release the motor port first
            try:
                svc.stop()
            except Exception:  # noqa: BLE001, S110 — best effort during shutdown
                pass
        for role in list(self.profile.cameras):
            try:
                self.controller.stop_live(role.role)
            except Exception:  # noqa: BLE001, S110 — best effort during shutdown
                pass
        self.core.shutdown()
        stop = getattr(self.core.clock, "stop", None)
        if stop:
            stop()


PHYSICAL_STAGE_DEVICE = "motor.stage"


def build_sim_session(clock: Clock, *, fast: bool = False, settings: AppSettings | None = None,
                      settings_path: Path | None = None, fps: float = 12.0,
                      calibration_path: Path | None = None,
                      telemetry: dict[str, dict] | None = None, audit_path: Path | None = None,
                      cameras: dict[str, str] | None = None, illumination: str | None = None,
                      led_hid=None) -> Session:
    """Simulated optics session. `telemetry` (e.g. {"technosoft": {"port": "COM3", "motion": False}})
    adds the physical motor drives (ADR-0023); with "motion": True the controller's stage becomes the
    real X/Y stage (ADR-0024): every move still passes the router's safety checks, and the drive
    clamps to its own limits. Cameras, light, focus and turret stay simulated."""
    from openevos.backends.rig.rig import attach_optical_simulator
    from openevos.core.instrument import attach_telemetry

    core = build_core(clock, ownership=OwnershipRegistry())
    profile = load_profile()
    settings = settings or (AppSettings.load(settings_path) if settings_path else AppSettings())
    vessels = VesselLibrary()
    try:
        vessel = vessels.get(settings.last_vessel_id)
    except KeyError:
        vessel = vessels.get("plate-96")
    light_factory = _real_illumination_factory(illumination, led_hid)
    if light_factory is not None:
        from openevos.backends.ledboard import DEVICE_ID as LED_DEVICE_ID

        profile = profile.model_copy(update={"illumination_device": LED_DEVICE_ID,
                                             "channels": unit_cube_channels(profile.channels)})
    rig = attach_optical_simulator(core, profile, vessel, fast=fast, fps=fps,  # rig keeps its own sim stage
                                   camera_factories=_real_camera_factories(core, cameras or {},
                                                                           light_real=light_factory is not None),
                                   illumination_factory=light_factory)
    services = attach_telemetry(core, telemetry or {})
    _link_carriage(light_factory, services)
    physical = any(getattr(s, "motion", False) for s in services)
    if physical:
        profile = profile.model_copy(update={"stage_device": PHYSICAL_STAGE_DEVICE})
        if _enable_focus_and_turret(core, services):
            profile = profile.model_copy(update={"focus_device": "motor.focus", "turret_device": "motor.objective_turret",  # noqa: E501
                                                 "objectives": unit_objectives()})
    audit = _audit_physical_commands(core, audit_path) if services else []
    ctl = InstrumentController(core, profile)
    cap = CaptureService(ctl, profile, settings)
    af = Autofocus(ctl, profile)
    session = Session(core, profile, settings, vessels, vessel, rig.stage_map, ctl, cap, af, rig, settings_path,
                      calibration=CalibrationStore.load(calibration_path), telemetry=services,
                      physical_stage=physical, physical_audit=audit)
    _install_interlocks(session)
    for svc in services:
        svc.start()
    return session


#: turrets that lower Z themselves before turning (the real turret service, ADR-0053, with its centred-stage check)
SELF_RETRACTING_TURRETS = frozenset({"motor.objective_turret"})


def _install_interlocks(session: Session) -> None:
    """Router-level instrument rules for the app (audit APP-01/APP-03), wired to what each backend really does:

    * turret vs focus height: for a turret that does not lower Z itself (the simulator). `Session.change_objective`
      lowers Z through the router first, so normal use meets the rule. The real turret service enforces it inside its
      own sequence (retract Z to 0, centred stage, ADR-0053).
    * focus safe height per objective (Settings › focus_limit_um): an upward move past it needs an operator-confirmed
      command; autofocus never searches above it.
    * light vs cube in the path: not a router pre-check here. Both light backends move the cube carriage and confirm
      the cube inside the same command (the real board with its own cube sensor, ADR-0051), so a pre-check would
      refuse every valid cube selection.
    """
    from openevos.core.command_language import DEFAULT_LIMITS, POSITION_TOLERANCE_UM
    from openevos.safety.interlocks import FocusSafeHeight, InstrumentInterlocks

    p, ctl = session.profile, session.controller
    if p.turret_device and p.focus_device and p.turret_device not in SELF_RETRACTING_TURRETS:
        session.turret_safe_z_um = ctl.turret_safe_z_um = DEFAULT_LIMITS["turret_safe_z_um"]
        session.core.safety.add_interlock(InstrumentInterlocks(
            focus_id=p.focus_device, objective_turret_id=p.turret_device,
            turret_safe_z=session.turret_safe_z_um, position_tolerance=POSITION_TOLERANCE_UM))

    def safe_height() -> tuple[str, float] | None:
        slot = ctl.objective_slot()
        v = session.settings.basic.focus_limit_um.get(str(slot)) if slot else None
        if v is None:
            return None
        obj = p.objective(slot)
        return (obj.short if obj else f"position {slot}"), float(v)

    if p.focus_device:
        session.core.safety.add_interlock(FocusSafeHeight(focus_id=p.focus_device, limit_for=safe_height))
        session.autofocus.z_cap = lambda: (lim[1] if (lim := safe_height()) else None)


def real_mono_camera_present() -> bool:
    """Read-only probe for the UI's `--mono-camera auto`: FlyCapture2 present and exactly one Chameleon
    (1E10:2005) with a working driver. Opens no device (ADR-0037)."""
    from openevos.backends.flycapture2.sdk import chameleon_present

    return chameleon_present()


def real_colour_camera_present() -> bool:
    """Read-only probe for the UI's `--colour-camera auto`: uEye API present and exactly one UI-146xLE
    (1409:1465) with a working driver. Opens no device (ADR-0039)."""
    from openevos.backends.ueye.sdk import ueye_present

    return ueye_present()


def real_led_board_present(hid=None) -> bool:
    """Read-only probe for the UI's `--illumination auto`: exactly one LED board application (0DAD:0051) and no
    bootloader interface. SetupAPI enumeration only; nothing is opened or sent (ADR-0048)."""
    from openevos.backends.ledboard import present_application_interfaces

    if hid is None:
        from openevos.backends.ledboard.winhid import WindowsHid

        hid = WindowsHid()
    apps, boot = present_application_interfaces(hid)
    return len(apps) == 1 and not boot


def _real_illumination_factory(illumination: str | None, hid=None):
    """'real' -> factory for the LED board backend (ADR-0048); None/'sim' -> the simulator."""
    if illumination in (None, "sim"):
        return None
    if illumination != "real":
        raise ValueError(f"unknown illumination backend {illumination!r} (known: real, sim)")

    def make(core, profile, chans, start_channel):
        from openevos.backends.ledboard import RealLedIllumination

        board = hid
        if board is None:
            from openevos.backends.ledboard.winhid import WindowsHid

            board = WindowsHid()
        labels = {c.slot: c.label for c in profile.channels}
        dev = RealLedIllumination(board, chans, labels, start_channel=start_channel)
        make.devices.append(dev)  # type: ignore[attr-defined]
        return dev

    make.devices = []  # type: ignore[attr-defined]
    return make


#: this unit's cube carriage (ADR-0051): slot -> (label, excitation nm, emission nm, pseudocolour). Slot 1 RFP was read
#: by the LED board's own cube sensor (LED Gate 3, 0x47); slots 3/4 from the operator's photo, checked by the sensor
#: before every epi ON; slot 2 is empty.
UNIT_CUBES: dict[int, tuple[str, int, int, str] | None] = {
    1: ("RFP", 531, 593, "#FF4D4D"), 2: None, 3: ("DAPI", 357, 447, "#4F7CFF"), 4: ("GFP", 470, 525, "#2EE66B")}


def unit_cube_channels(channels):
    """The profile's channels with the four cube slots replaced by this unit's loadout (UNIT_CUBES); transmitted
    and phase channels unchanged. Used with the real LED board, so each cube button lights the cube it names."""
    from openevos.instrument.profile import ChannelKind, ChannelSlot, Provenance

    cubes = []
    for n, spec in UNIT_CUBES.items():
        if spec is None:
            cubes.append(ChannelSlot(slot=f"cube{n}", kind=ChannelKind.FLUORESCENCE, label="", installed=False,
                                     provenance=Provenance.OPERATOR))
        else:
            label, ex, em, colour = spec
            cubes.append(ChannelSlot(slot=f"cube{n}", kind=ChannelKind.FLUORESCENCE, label=label, pseudocolor=colour,
                                     excitation_nm=ex, emission_nm=em, provenance=Provenance.OPERATOR))
    return (*cubes, *(c for c in channels if not c.slot.startswith("cube")))


#: this unit's objectives by turret position (calibration `tpco`; position 1 = 4X confirmed by the operator, the full
#: turret cycle observed 2026-11-17). NA from the AMG catalogue numbers in `tpco`; Probable, not measured here.
UNIT_OBJECTIVES = ((1, "4X Plan PH", 4, 0.13, "Ph"), (2, "10X LPlan FL PH", 10, 0.25, "Ph1"),
                   (3, "20X LPlan FL PH", 20, 0.40, "Ph1"), (4, "40X LPlan FL PH", 40, 0.65, "Ph2"),
                   (5, "60X Plan FL", 60, 0.75, None))


def unit_objectives():
    """The turret buttons for this unit (ADR-0053). Not calibrated (pixel size from the nominal magnification)."""
    from openevos.instrument.profile import ObjectiveSlot, Provenance

    return tuple(ObjectiveSlot(slot=s, label=label, magnification=mag, numerical_aperture=na, immersion="air",
                               phase_ring=ring, calibration_valid=False, provenance=Provenance.RECOVERED)
                 for s, label, mag, na, ring in UNIT_OBJECTIVES)


def _enable_focus_and_turret(core, services) -> bool:
    """ADR-0053: with real motor control (and focus/turret control, on by default with it), the app's focus and
    objective controls drive the real Z and turret through the stage service, the single owner of the motor port."""
    motion = [s for s in services if getattr(s, "motion", False) and getattr(s, "zo_control", False)]
    return len(motion) == 1


def _link_carriage(light_factory, services) -> None:
    """ADR-0051: with the real LED board AND real motor control, a cube channel drives the F carriage to its cube
    before the light goes on. The stage service is the only owner of the motor port, so it makes the move."""
    if light_factory is None:
        return
    motion = [s for s in services if getattr(s, "motion", False) and hasattr(s, "move_carriage")]
    if len(motion) != 1:
        return
    svc = motion[0]
    svc.carriage_control = True
    for dev in getattr(light_factory, "devices", ()):
        dev.carriage = svc.move_carriage


def _real_camera_factories(core: CoreServices, cameras: dict[str, str], *, light_real: bool = False) -> dict:
    """Camera roles to run on real hardware, e.g. {"mono": "flycapture2", "color": "ueye"}: the mono
    Chameleon (ADR-0037) and the colour uEye (ADR-0039). Anything else is refused before any device is
    touched."""
    from openevos.instrument.profile import CameraRole

    known = {("mono", "flycapture2"): CameraRole.MONO, ("color", "ueye"): CameraRole.COLOR}
    factories = {}
    for role_name, backend in cameras.items():
        role = known.get((role_name, backend))
        if role is None:
            raise ValueError(f"no real camera backend {backend!r} for role {role_name!r} "
                             "(known: mono=flycapture2, color=ueye)")

        def make(rig, cam, backend=backend):
            extra = lambda: (("camera_role", cam.role.value), ("light_simulated", str(not light_real)))
            if backend == "flycapture2":
                from openevos.backends.flycapture2 import FlyCapture2MonoCamera

                return FlyCapture2MonoCamera(cam.device_id, core.transport, channel_fn=lambda: rig.light.channel,
                                             extra_fn=extra)
            from openevos.backends.ueye import UeyeColourCamera

            return UeyeColourCamera(cam.device_id, core.transport, channel_fn=lambda: rig.light.channel,
                                    extra_fn=extra)
        factories[role] = make
    return factories


def _audit_physical_commands(core: CoreServices, path: Path | None) -> list[dict]:
    """Append-only JSONL record of every lifecycle step of a command to a physical motor device
    (the UI's equivalent of the gateway audit trail for ADR-0024). In memory only when path is None."""
    import json
    from datetime import datetime, timezone

    from openevos.core.event_bus import CommandLifecycleEvent

    lock = threading.Lock()

    def record(ev: CommandLifecycleEvent) -> None:
        if not ev.device_id.startswith("motor."):
            return
        line = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "device": ev.device_id,
                "command_id": ev.command_id, "status": ev.status.value,
                "previous": ev.previous_status.value if ev.previous_status else None, "message": ev.message,
                "result": ev.result.as_dict() if ev.result is not None else None}
        with lock:
            records.append(line)
            if path is not None:
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line) + "\n")

    records: list[dict] = []
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    core.bus.subscribe(CommandLifecycleEvent, record)
    return records
