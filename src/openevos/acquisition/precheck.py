"""System precheck: one read-only pass over everything the UI drives, run at start and from Microscope › System
precheck (operator, 2026-11-18: "every time I see some or other fault or an unresponsive UI to a mapped item").

It reads the runtime state store, the drift monitor, the latest preview frame and the settings. It sends nothing to
any device, so it can run at any time. Each check says what is wrong and what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from openevos.core.models import Activity, BackendKind, Connection, FirmwareMode, Health, utcnow
from openevos.instrument.profile import CameraRole

#: worst first
ORDER = ("fault", "warn", "info", "ok")
#: no live frame for this long while live view should be running = the camera looks unresponsive
FRAME_STALE_S = 5.0
#: the turret refuses unless the stage is inside this box (ADR-0053; the Gate 19 collision)
TURRET_BOX_MM = {"X": (40.0, 80.0), "Y": (20.0, 60.0)}

LED_FIX = ("The LED board powers up in its bootloader. The app starts it at launch (ADR-0055): close the app and start "
           "it again. If it stays in the bootloader, see the 'LED board:' line the app printed at start, or run "
           "`python -m openevos.maintenance.led_autostart` to see why")
RECOVER_FIX = "Open Service › Hardware and use Recover for this device, or power-cycle the instrument"
HOME_FIX = "Power-cycle the instrument so the drives home, then restart the app"


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    status: str  # "ok" | "info" | "warn" | "fault"
    detail: str
    fix: str = ""


def worst(checks: list[Check]) -> str:
    return min((c.status for c in checks), key=ORDER.index, default="ok")


def _mapped(session) -> list[tuple[str, str]]:
    """(title, device_id) for every device a UI control drives."""
    p = session.profile
    out = [("Stage X/Y", p.stage_device), ("Z focus", p.focus_device), ("Objective turret", p.turret_device),
           ("Light (LED board)", p.illumination_device)]
    for role, title in ((CameraRole.MONO, "Mono camera"), (CameraRole.COLOR, "Colour camera")):
        cam = p.camera(role)
        out.append((title, cam.device_id if cam else None))
    store = session.core.store.snapshot_all()
    if "motor.cube_carriage" in store:
        out.append(("Cube carriage (F)", "motor.cube_carriage"))
    return [(t, d) for t, d in out if d]


def ready(session) -> bool:
    """True once every mapped device has reported a definite connection state (the drives poll ~1 s after start)."""
    store = session.core.store
    for _title, did in _mapped(session):
        s = store.get(did)
        if s is None or s.connection in (Connection.CONNECTING, Connection.UNKNOWN):
            return False
        if s.backend_kind is BackendKind.PHYSICAL and s.connection is Connection.CONNECTED and s.observed_at is None:
            return False
    return True


def _device(session, title: str, did: str) -> Check:
    s = session.core.store.get(did)
    key = f"device.{did}"
    if s is None:
        return Check(key, title, "fault", "reports no state", "Restart the app; if it persists, check the cable "
                                                             "and power")
    note = f" ({s.note})" if s.note else ""
    if s.firmware_mode in (FirmwareMode.BOOTLOADER, FirmwareMode.RECOVERY):
        fix = LED_FIX if did == session.profile.illumination_device else "Power-cycle the instrument"
        return Check(key, title, "fault", f"in its {s.firmware_mode.value} firmware: commands are refused", fix)
    if s.connection is not Connection.CONNECTED:
        fix = LED_FIX if (did == session.profile.illumination_device and "bootloader" in (s.note or "")) else \
            "Check the cable and power, then restart the app"
        return Check(key, title, "fault", f"{s.connection.value}{note}", fix)
    if s.recovery_required:
        return Check(key, title, "fault", f"needs recovery before it accepts commands{note}", RECOVER_FIX)
    if s.health is Health.FAULT:
        return Check(key, title, "fault", f"fault{note}", RECOVER_FIX)
    if dict(s.attributes).get("homed") is False:
        return Check(key, title, "fault", "not homed: moves are refused", HOME_FIX)
    if s.health is Health.WARNING:
        return Check(key, title, "warn", f"warning{note}", "See Service › Events for details")
    if s.backend_kind is BackendKind.SIMULATED:
        return Check(key, title, "info", "simulated: no real device is mapped to this control",
                     "Start the app from the 'OpenEVOS (real stage)' shortcut to use the instrument")
    busy = f", {s.activity.value}" if s.activity not in (Activity.IDLE, Activity.UNKNOWN) else ""
    return Check(key, title, "ok", f"connected{busy}")


def _monitor(session) -> list[Check]:
    out = []
    for svc in getattr(session, "telemetry", []) or []:
        mon = getattr(svc, "monitor", None)
        if mon is None:
            continue
        st = mon.state()
        port = getattr(svc, "port_label", "") or "motor port"
        if st.get("latched"):
            fix = ("Small drift: Microscope › Release stage lock (it checks X and Y first; ADR-0057). Otherwise: "
                   if st.get("releasable") else "") + st.get("recovery", RECOVER_FIX)
            out.append(Check("monitor", f"X/Y drift monitor ({port})", "fault", f"latched: {st.get('message', '')}",
                             fix))
        elif st.get("warnings"):
            out.append(Check("monitor", f"X/Y drift monitor ({port})", "warn", "; ".join(map(str, st["warnings"])),
                             "Watch the stage; the monitor latches if the drift continues"))
        else:
            out.append(Check("monitor", f"X/Y drift monitor ({port})", "ok", "watching, no drift"))
    return out


def _frames(session) -> Check | None:
    role = session.settings.active_camera
    cam = session.profile.camera(role)
    if cam is None:
        return None
    frame = session.core.transport.preview.latest()
    title = "Live view"
    if frame is None:
        return Check("frames", title, "warn", "no frame from the camera yet",
                     "Press Live on the Capture page; if nothing comes, check the camera's cable and restart the app")
    age = (utcnow() - frame.metadata.timestamp) if frame.metadata.timestamp else timedelta(0)
    if age.total_seconds() > FRAME_STALE_S:
        return Check("frames", title, "warn", f"last frame {age.total_seconds():.0f} s ago",
                     "Live view is paused or the camera stopped sending; press Live, or restart the app")
    return Check("frames", title, "ok", f"frames arriving from {frame.metadata.device_id}")


#: the X/Y drives' power-up LPOSMAX: still in place = the calibrated limits (Gate 7) were not loaded since power-on
POWER_UP_LPOSMAX = 50000


def stage_limits(session) -> Check | None:
    if not session.physical_stage:
        return None
    s = session.core.store.get(session.profile.stage_device)
    lim = dict(s.attributes).get("limits_counts") if s is not None else None
    if not lim:
        return None
    if any(hi == POWER_UP_LPOSMAX for _lo, hi in lim):
        return Check("stage_limits", "Stage travel limits", "fault",
                     "the drives hold their power-up limits (0–22 mm): the stage cannot reach the centre and "
                     "objective changes are refused",
                     "Jog the stage back to its home corner (X 0, Y 0), then close and restart the app: it loads "
                     "the calibrated limits at launch (Gate 7, ADR-0056)")
    return Check("stage_limits", "Stage travel limits", "ok", "calibrated limits loaded")


def stage_box(session) -> Check | None:
    if not session.physical_stage or not session.profile.turret_device:
        return None
    xy = session.controller.stage_xy()
    if xy is None:
        return None
    x, y = xy[0] / 1000.0, xy[1] / 1000.0
    inside = TURRET_BOX_MM["X"][0] <= x <= TURRET_BOX_MM["X"][1] and TURRET_BOX_MM["Y"][0] <= y <= TURRET_BOX_MM["Y"][1]
    if inside:
        return Check("stage_box", "Stage position", "ok", f"X {x:.1f} mm, Y {y:.1f} mm: objectives can change")
    return Check("stage_box", "Stage position", "info",
                 f"X {x:.1f} mm, Y {y:.1f} mm: outside the turret box (X {TURRET_BOX_MM['X'][0]:g}–"
                 f"{TURRET_BOX_MM['X'][1]:g}, Y {TURRET_BOX_MM['Y'][0]:g}–{TURRET_BOX_MM['Y'][1]:g} mm)",
                 "Objective changes are refused until the stage is centred: jog it to the middle first")


def _safe_heights(session) -> list[Check]:
    limits = session.settings.basic.focus_limit_um
    objs = [o for o in session.profile.objectives if o.installed]
    if not objs:
        return []
    missing = [o.short for o in objs if str(o.slot) not in limits]
    out = []
    if missing:
        out.append(Check("safe_heights", "Objective safe heights", "warn",
                         f"{len(objs) - len(missing)} of {len(objs)} set; missing: {', '.join(missing)}",
                         "Use Set safe heights below (or ⚠ next to the focus step list) for each objective"))
    else:
        out.append(Check("safe_heights", "Objective safe heights", "ok", f"all {len(objs)} set"))
    slot, z = session.controller.objective_slot(), session.controller.focus_z()
    safe = limits.get(str(slot)) if slot else None
    if safe is not None and z is not None and z > safe + 1e-6:
        obj = session.profile.objective(slot)
        out.append(Check("z_above", "Z focus height", "warn",
                         f"Z {z:,.2f} µm is above the {obj.short if obj else slot} safe height ({safe:,.2f} µm)",
                         "Lower Z (bottom half of the focus pad) before moving the stage"))
    return out


def run_precheck(session) -> list[Check]:
    """Every check, in the order they are shown. Reads only."""
    checks = [_device(session, title, did) for title, did in _mapped(session)]
    checks += _monitor(session)
    for c in (_frames(session), stage_limits(session), stage_box(session)):
        if c is not None:
            checks.append(c)
    checks += _safe_heights(session)
    return checks


def summary(checks: list[Check]) -> str:
    n = {s: sum(c.status == s for c in checks) for s in ORDER}
    if n["fault"] or n["warn"]:
        parts = [f"{n['fault']} fault" + ("s" if n["fault"] != 1 else "")] if n["fault"] else []
        parts += [f"{n['warn']} warning" + ("s" if n["warn"] != 1 else "")] if n["warn"] else []
        return f"Precheck: {', '.join(parts)} of {len(checks)} checks"
    return f"Precheck passed: {len(checks)} checks"
