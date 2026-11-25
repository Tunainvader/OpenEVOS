"""The Health recorder and sampler (ADR-0068 §3): reads the EventBus, the RuntimeStateStore, the StageMonitor and the
SafetyManager's decisions; writes the journal; keeps the latest HealthReport for the page. It commands nothing.

Threads and blocking:
* EventBus callbacks run on the publisher's thread (device workers; the RuntimeStateStore publishes with its lock
  held). Each callback does O(1) work: a few field comparisons, a dict assignment, and `HealthJournal.offer`, which
  never blocks. Every callback catches its own errors, so a Health bug can never reach a publisher.
* A sampler thread (1 Hz) gathers a HealthView and runs `assess`. That is where the StageMonitor's lock is taken
  (`state()`, `refusal()`), where the disk and memory are read, and where the SafetyManager is asked about probe
  commands. The page only reads `latest`, an immutable report swapped in whole, so the UI never waits on any of it.

Probe commands: to show what the authority decides, the sampler builds one command per device (a move to where the
device already is, an exposure it already has, a light ON) and passes it to `SafetyManager.validate`, a pure function.
The probe is never submitted: it never reaches the router, a worker or a backend. Only the state checks of the decision
are kept (assess.STATE_CHECKS); target checks (limits, confirmation, interlocks) do not apply to a probe.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from openevos.core.event_bus import (
    AcquisitionStatusEvent,
    CommandLifecycleEvent,
    DeviceStateChanged,
    HardwareFaultEvent,
    StorageFaultEvent,
    Subscription,
)
from openevos.core.models import (
    BackendKind,
    Command,
    CommandOrigin,
    CommandStatus,
    Connection,
    DeviceKind,
    DeviceSnapshot,
    Health,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    SetCameraExposure,
    SetIllumination,
    SetTurretPosition,
    new_id,
    utcnow,
)
from openevos.health.assess import (
    DISK_PERIOD_S,
    STATE_CHECKS,
    DiskView,
    HealthView,
    LinkView,
    MonitorView,
    assess,
    failed_report,
)
from openevos.health.journal import JOURNAL_FILE, HealthJournal
from openevos.health.model import HealthEvent, HealthReport, HealthState, Provenance

SAMPLE_PERIOD_S = 1.0
RECENT_EVENTS = 300
MAX_PENDING_COMMANDS = 4096
#: words in a backend's reason that mean its link failed or came back (counted as link errors)
LINK_ERROR_WORDS = ("telemetry failed", "telemetry unavailable", "dropped off usb", "not on usb", "stream failed",
                    "link error", "reconnected", "reopened after")
MOTOR_EXTRA = {"xy_stage": "motor.stage", "focus": "motor.focus", "objective_turret": "motor.objective_turret"}
CARRIAGES = ("motor.cube_carriage", "motor.phase_wheel")


def default_data_folder() -> Path:
    return Path.home() / "Documents" / "OpenEVOS"


def memory_load_percent() -> float | None:
    """Physical memory in use (%), from GlobalMemoryStatusEx; None off Windows or on any error."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)) or not st.ullTotalPhys:
            return None
        return 100.0 * (1.0 - st.ullAvailPhys / st.ullTotalPhys)
    except Exception:  # noqa: BLE001 - unavailable is a value, not an error
        return None


def disk_view(path: Path, now: datetime) -> DiskView:
    """Free space on the drive holding `path` (its nearest existing parent)."""
    p = path
    try:
        while not p.exists() and p.parent != p:
            p = p.parent
        u = shutil.disk_usage(p)
        return DiskView(str(path), u.free, u.total, "", now)
    except OSError as exc:
        return DiskView(str(path), None, None, str(exc), now)


def _condition(s: DeviceSnapshot) -> tuple[Any, ...]:
    return (s.connection, s.health, s.firmware_mode, s.recovery_required, s.attr("homed"), s.attr("drift_lock"))


def _severity(s: DeviceSnapshot) -> str:
    if s.health is Health.FAULT or s.recovery_required:
        return "error"
    if s.health is Health.WARNING or s.connection is Connection.DISCONNECTED:
        return "warning"
    return "info"


class HealthService:
    def __init__(self, session: Any, *, journal: HealthJournal | None = None, journal_path: Path | str | None = None,
                 sample_period_s: float = SAMPLE_PERIOD_S, data_folder: Path | None = None,
                 now: Callable[[], datetime] = utcnow):
        self.session = session
        self.core = session.core
        self.journal = journal if journal is not None else HealthJournal(journal_path)
        self.sample_period_s = sample_period_s
        self.data_folder = data_folder or default_data_folder()
        self._now = now
        self._lock = threading.Lock()  # counters, the recent ring, the per-device dicts
        self._recent: deque[HealthEvent] = deque(maxlen=RECENT_EVENTS)
        self._last_reason: dict[str, tuple[str, datetime]] = {}
        self._last_refusal: dict[str, tuple[str, datetime]] = {}
        self._link_errors: dict[str, deque[tuple[datetime, str]]] = {}
        self._cmd_start: OrderedDict[str, datetime] = OrderedDict()
        self._counters: dict[str, Any] = {"refusals": 0, "failures": 0, "storage_faults": 0, "frames_failed": 0,
                                          "timeouts": deque(maxlen=50), "last_duration": None,
                                          "last_storage_fault": ""}
        self.recorder_errors = 0
        self.last_error = ""
        self.samples = 0
        self.latest: HealthReport = failed_report(now(), "not assessed yet")
        self._subs: list[Subscription[Any]] = []
        self._prev_states: dict[str, HealthState] | None = None
        self._prev_monitor: dict[str, dict[str, Any]] = {}
        self._disk: tuple[float, dict[str, DiskView]] | None = None
        self._device_sub: dict[str, str] = {}
        self._link_of: dict[str, str] = {}
        self._mapped: dict[str, tuple[str, ...]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False

    # ================================================================== lifecycle
    def start(self, *, sampler: bool = True) -> HealthService:
        if self.running:
            return self
        self.running = True
        try:
            self._remap()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
        bus = self.core.bus
        self._subs = [bus.subscribe(DeviceStateChanged, self._on_device),
                      bus.subscribe(CommandLifecycleEvent, self._on_command),
                      bus.subscribe(HardwareFaultEvent, self._on_fault),
                      bus.subscribe(StorageFaultEvent, self._on_storage),
                      bus.subscribe(AcquisitionStatusEvent, self._on_acquisition)]
        try:
            self.journal.start()
            self._emit("recorder.started", "host", "info", "Health recorder started (read-only; Phase 0)",
                       source="HealthService", data={"journal": self.journal.stats().get("path", "")})
        except Exception as exc:  # noqa: BLE001 - starting Health never fails the caller (the app launch)
            self._fail(exc)
        if sampler:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="health-sampler", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """Unsubscribe, stop sampling, flush the journal. Idempotent."""
        if not self.running:
            return
        self.running = False
        for s in self._subs:
            try:
                s.unsubscribe()
            except Exception:  # noqa: BLE001, S110 - shutting down
                pass
        self._subs = []
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None
        try:
            self._emit("recorder.stopped", "host", "info", "Health recorder stopped", source="HealthService")
            self.journal.close()
        except Exception as exc:  # noqa: BLE001 - stopping Health never fails the app's shutdown
            self._fail(exc)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.sample_period_s)

    # ================================================================== reads (any thread)
    def recent_events(self, limit: int = 100) -> list[HealthEvent]:
        with self._lock:
            items = list(self._recent)
        return items[::-1][:limit]

    def last_refusal(self, device_id: str) -> tuple[str, datetime] | None:
        with self._lock:
            return self._last_refusal.get(device_id)

    # ================================================================== bus callbacks (publisher threads; O(1))
    def _on_device(self, e: DeviceStateChanged) -> None:
        try:
            did, cur, prev = e.device_id, e.current, e.previous
            reason = e.reason or ""
            old = self._last_reason.get(did)
            if reason:
                with self._lock:
                    self._last_reason[did] = (reason, e.timestamp)
            link = self._link_of.get(did)
            lost = prev is not None and prev.connection is Connection.CONNECTED and \
                cur.connection is Connection.DISCONNECTED
            if link and (lost or (reason and (old is None or old[0] != reason)
                                  and any(w in reason.lower() for w in LINK_ERROR_WORDS))):
                with self._lock:
                    self._link_errors.setdefault(link, deque(maxlen=100)).append(
                        (e.timestamp, f"{did}: {reason or 'disconnected'}"))
            if prev is not None and _condition(prev) == _condition(cur):
                return
            what = "registered" if prev is None else (
                f"{cur.connection.value}/{cur.health.value}" + (" · recovery required" if cur.recovery_required else "")
                + (" · drift lock" if cur.attr("drift_lock") else ""))
            prov = Provenance.DEMO if cur.backend_kind is BackendKind.SIMULATED else Provenance.MEASURED
            self._emit("device.condition", self._sub_of(did), _severity(cur), f"{did} {what}"
                       + (f": {reason}" if reason and reason != "telemetry" else ""), device_id=did,
                       observed_at=e.timestamp, provenance=prov, source="EventBus.DeviceStateChanged",
                       data={"connection": cur.connection.value, "health": cur.health.value,
                             "firmware_mode": cur.firmware_mode.value, "recovery_required": cur.recovery_required,
                             "homed": cur.attr("homed"), "drift_lock": cur.attr("drift_lock"), "reason": reason,
                             "previous": None if prev is None else {"connection": prev.connection.value,
                                                                    "health": prev.health.value}})
        except Exception as exc:  # noqa: BLE001 - never reaches the publisher
            self._fail(exc)

    def _on_command(self, e: CommandLifecycleEvent) -> None:
        try:
            if not e.status.is_terminal:
                if e.command_id not in self._cmd_start:
                    with self._lock:
                        self._cmd_start[e.command_id] = e.timestamp
                        while len(self._cmd_start) > MAX_PENDING_COMMANDS:
                            self._cmd_start.popitem(last=False)
                return
            with self._lock:
                started = self._cmd_start.pop(e.command_id, None)
            dur = (e.timestamp - started).total_seconds() if started else None
            msg = e.message or ""
            data: dict[str, Any] = {"command_id": e.command_id, "status": e.status.value, "duration_s": dur,
                                    "correlation_id": e.correlation_id}
            sub = self._sub_of(e.device_id)
            if e.status is CommandStatus.SUCCEEDED:
                with self._lock:
                    if dur is not None:
                        self._counters["last_duration"] = (e.device_id, dur, e.timestamp)
                self._emit("command.succeeded", sub, "info", f"{e.device_id} command succeeded"
                           + (f" in {dur:.2f} s" if dur is not None else ""), device_id=e.device_id,
                           observed_at=e.timestamp, provenance=Provenance.MEASURED,
                           source="EventBus.CommandLifecycleEvent", data=data, recent=False)
                return
            sev = {CommandStatus.REJECTED: "warning", CommandStatus.CANCELLED: "info"}.get(e.status, "error")
            with self._lock:
                if e.status is CommandStatus.REJECTED:
                    self._counters["refusals"] += 1
                    self._last_refusal[e.device_id] = (f"rejected by the router: {msg}", e.timestamp)
                elif e.status in (CommandStatus.FAILED, CommandStatus.TIMED_OUT):
                    self._counters["failures"] += 1
                    self._last_refusal[e.device_id] = (f"{e.status.value} in the device service: {msg}",
                                                       e.timestamp)
                    if e.status is CommandStatus.TIMED_OUT:
                        self._counters["timeouts"].append((e.timestamp, f"{e.device_id}: {msg}"))
            self._emit(f"command.{e.status.value}", sub, sev, f"{e.device_id} command {e.status.value}: {msg}",
                       device_id=e.device_id, observed_at=e.timestamp, provenance=Provenance.MEASURED,
                       source="EventBus.CommandLifecycleEvent", data=data)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _on_fault(self, e: HardwareFaultEvent) -> None:
        try:
            sev = getattr(e.severity, "value", str(e.severity))
            self._emit("fault.hardware", self._sub_of(e.device_id), sev, f"{e.device_id}: {e.message}",
                       device_id=e.device_id, observed_at=e.timestamp, provenance=Provenance.MEASURED,
                       source="EventBus.HardwareFaultEvent", data={"error_code": e.error_code,
                                                                   "command_id": e.command_id})
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _on_storage(self, e: StorageFaultEvent) -> None:
        try:
            with self._lock:
                self._counters["storage_faults"] += 1
                self._counters["last_storage_fault"] = e.message
            self._emit("storage.fault", "storage", "error", f"storage fault ({e.acquisition_id}): {e.message}",
                       observed_at=e.timestamp, provenance=Provenance.MEASURED, source="EventBus.StorageFaultEvent",
                       data={"acquisition_id": e.acquisition_id, "frame_index": e.frame_index})
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _on_acquisition(self, e: AcquisitionStatusEvent) -> None:
        try:
            if e.state != "started":
                with self._lock:
                    self._counters["frames_failed"] += int(e.failed)
            sev = "info" if e.state in ("started", "completed") and not e.failed else "warning"
            self._emit(f"acquisition.{e.state}", "storage", sev,
                       f"acquisition {e.acquisition_id} {e.state}: {e.written} written, {e.failed} failed"
                       + (f" ({e.message})" if e.message else ""), observed_at=e.timestamp,
                       provenance=Provenance.MEASURED, source="EventBus.AcquisitionStatusEvent",
                       data={"accepted": e.accepted, "written": e.written, "failed": e.failed,
                             "rejected_at_ingress": e.rejected_at_ingress, "expected": e.expected})
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    # ================================================================== emitting
    def _emit(self, kind: str, subsystem: str, severity: str, message: str, *, device_id: str = "",
              observed_at: datetime | None = None, provenance: Provenance = Provenance.MEASURED,
              source: str = "HealthService", data: dict[str, Any] | None = None, recent: bool = True) -> None:
        now = self._now()
        ev = HealthEvent(new_id(), kind, subsystem, severity, message, observed_at or now, now, provenance, source,
                         device_id, data or {})
        if recent:
            with self._lock:
                self._recent.append(ev)
        self.journal.offer(ev)

    def _fail(self, exc: BaseException) -> None:
        try:
            with self._lock:
                self.recorder_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
        except Exception:  # noqa: BLE001, S110 - the last line of defence never raises
            pass

    def _sub_of(self, device_id: str) -> str:
        return self._device_sub.get(device_id, "instrument")

    # ================================================================== mapping
    def _remap(self) -> None:
        s = self.session
        p = s.profile
        ids = set(self.core.devices.device_ids())
        snaps = self.core.store.snapshot_all()

        def one(*dids: str | None) -> tuple[str, ...]:
            return tuple(dict.fromkeys(d for d in dids if d))

        from openevos.instrument.profile import CameraRole

        mono, color = p.camera(CameraRole.MONO), p.camera(CameraRole.COLOR)
        mapped = {
            "xy_stage": one(p.stage_device, *(["motor.stage"] if "motor.stage" in ids else [])),
            "focus": one(p.focus_device, *(["motor.focus"] if "motor.focus" in ids else [])),
            "objective_turret": one(p.turret_device,
                                    *(["motor.objective_turret"] if "motor.objective_turret" in ids else [])),
            "filter_carriage": tuple(d for d in CARRIAGES if d in ids),
            "mono_camera": one(mono.device_id if mono else None),
            "colour_camera": one(color.device_id if color else None),
            "illumination": one(p.illumination_device),
            "incubator": tuple(sorted(d for d, sn in snaps.items() if sn.kind is DeviceKind.INCUBATOR)),
        }
        links: dict[str, str] = {}
        for svc in getattr(s, "telemetry", []) or []:
            for b in getattr(svc, "backends", []):
                links[b.device_id] = f"technosoft:{getattr(svc, 'port_label', '')}"
        for key in ("mono_camera", "colour_camera", "illumination"):
            for d in mapped[key]:
                sn = snaps.get(d)
                if sn is not None and sn.backend_kind is BackendKind.PHYSICAL:
                    links[d] = f"usb:{d}"
        self._mapped = mapped
        self._device_sub = {d: k for k, ds in mapped.items() for d in ds}
        self._link_of = links

    def _links(self) -> tuple[LinkView, ...]:
        out: list[LinkView] = []
        s = self.session
        for svc in getattr(s, "telemetry", []) or []:
            label = getattr(svc, "port_label", "") or "motor port"
            out.append(LinkView(f"technosoft:{label}", f"Technosoft drives ({label})",
                                tuple(b.device_id for b in getattr(svc, "backends", []))))
        titles = {"mono_camera": "Mono camera USB", "colour_camera": "Colour camera USB",
                  "illumination": "LED board USB HID"}
        for key, title in titles.items():
            for d in self._mapped.get(key, ()):
                if self._link_of.get(d) == f"usb:{d}":
                    out.append(LinkView(f"usb:{d}", title, (d,)))
        return tuple(out)

    # ================================================================== sampling
    def sample(self) -> HealthReport:
        """One assessment now (the sampler thread calls this every second; tests call it directly)."""
        now = self._now()
        try:
            self._remap()
            report = assess(self._gather(now))
        except Exception as exc:  # noqa: BLE001 - an assessment failure shows UNKNOWN, never the last green
            self._fail(exc)
            report = failed_report(now, f"assessment failed: {type(exc).__name__}: {exc}")
        try:
            self._transitions(report)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
        self.latest = report
        self.samples += 1
        return report

    def _gather(self, now: datetime) -> HealthView:
        core = self.core
        snaps = core.store.snapshot_all()
        periods: dict[str, float] = {}
        monitors: list[MonitorView] = []
        stage_ids = set(self._mapped.get("xy_stage", ()))
        for svc in getattr(self.session, "telemetry", []) or []:
            period = float(getattr(svc, "poll_interval_s", 1.0))
            for b in getattr(svc, "backends", []):
                periods[b.device_id] = period
            mon = getattr(svc, "monitor", None)
            if mon is not None:
                label = f"X/Y drift monitor, {getattr(svc, 'port_label', '') or 'motor port'}"
                mv = MonitorView(label, mon.state(), mon.refusal(),
                                 owns_stage=any(b.device_id in stage_ids for b in getattr(svc, "backends", [])),
                                 carried_over=bool(getattr(mon, "carried_over", False)),
                                 simulated=bool(getattr(svc, "emulated", False)))
                monitors.append(mv)
                self._monitor_transitions(mv, mon)
        refusals, missing = self._probes(snaps)
        with self._lock:
            counters = {k: (tuple(v) if isinstance(v, deque) else v) for k, v in self._counters.items()}
            last_reason = dict(self._last_reason)
            last_refusal = dict(self._last_refusal)
            link_errors = {k: tuple(v) for k, v in self._link_errors.items()}
            rec_err, rec_msg = self.recorder_errors, self.last_error
        t = time.monotonic()
        if self._disk is None or t - self._disk[0] >= DISK_PERIOD_S:
            disks = {"data": disk_view(self.data_folder, now)}
            if self.journal.path is not None:
                disks["journal"] = disk_view(self.journal.path.parent, now)
            self._disk = (t, disks)
        preview = getattr(getattr(core, "transport", None), "preview", None)
        host = {"memory_percent": memory_load_percent(), "threads": threading.active_count(),
                "bus_errors": getattr(core.bus, "delivery_errors", None),
                "stale_obs": getattr(core.store, "stale_observations", None),
                "preview": dict(preview.stats) if preview is not None else None,
                "journal": self.journal.stats(), "recorder_errors": rec_err, "recorder_error": rec_msg,
                "period_s": self.sample_period_s}
        return HealthView(now=now, devices=dict(self._mapped), snapshots=snaps, periods=periods,
                          monitors=tuple(monitors), links=self._links(), probe_refusals=refusals,
                          probe_missing=missing, last_reason=last_reason, last_refusal=last_refusal,
                          link_errors=link_errors, counters=counters, disks=dict(self._disk[1]), host=host,
                          host_measured_at=now)

    # ------------------------------------------------------------------ SafetyManager probes (validated, never sent)
    def _probes(self, snaps: dict[str, DeviceSnapshot]) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
        p = self.session.profile
        from openevos.instrument.profile import CameraRole

        mono, color = p.camera(CameraRole.MONO), p.camera(CameraRole.COLOR)
        targets = {"xy_stage": p.stage_device, "focus": p.focus_device, "objective_turret": p.turret_device,
                   "illumination": p.illumination_device, "mono_camera": mono.device_id if mono else None,
                   "colour_camera": color.device_id if color else None}
        refusals: dict[str, tuple[str, ...]] = {}
        missing: dict[str, str] = {}
        for sub, did in targets.items():
            if not did:
                missing[sub] = "no device for it in this session"
                continue
            rec, snap = self.core.devices.get(did), snaps.get(did)
            if rec is None or snap is None:
                missing[sub] = f"{did} is not registered in this session"
                continue
            cmd = probe_command(sub, did, rec.capabilities, snap)
            d = self.core.safety.validate(cmd, rec.capabilities, snap, snaps)
            kept = tuple(r for r, c in zip(d.reasons, d.checks, strict=True) if c in STATE_CHECKS)
            if kept:
                refusals[sub] = kept
        return refusals, missing

    # ------------------------------------------------------------------ transitions -> journal
    def _monitor_transitions(self, mv: MonitorView, mon: Any) -> None:
        st = mv.state
        prev = self._prev_monitor.get(mv.label)
        cur: dict[str, Any] = {"latched": bool(st.get("latched")), "rule": st.get("rule"), "axis": st.get("axis"),
                               "since": st.get("since_utc"), "evidence": st.get("evidence") or "",
                               "warnings": tuple(st.get("warnings") or ())}
        self._prev_monitor[mv.label] = cur
        prov = Provenance.DEMO if mv.simulated else Provenance.MEASURED
        src = "StageMonitor.state()"
        if prev is None:
            if cur["latched"]:
                self._emit("stage_monitor.latched", "xy_stage", "critical",
                           f"{mv.label}: locked ({cur['rule']} on {cur['axis']}) since {cur['since']}"
                           + (" (carried over from an earlier session)" if mv.carried_over else ""),
                           provenance=prov, source=src, data={**_latch_data(st), "refusal": mv.refusal,
                                                              "carried_over": mv.carried_over})
            return
        if cur["latched"] and (not prev["latched"] or prev["since"] != cur["since"]):
            self._emit("stage_monitor.latched", "xy_stage", "critical",
                       f"{mv.label}: X/Y moves locked: {st.get('rule')} on {st.get('axis')}: {st.get('message', '')}",
                       provenance=prov, source=src, data={**_latch_data(st), "refusal": mv.refusal})
        elif prev["latched"] and not cur["latched"]:
            why = _last_clear_note(mon)
            self._emit("stage_monitor.cleared", "xy_stage", "info",
                       f"{mv.label}: lock {prev['rule']} on {prev['axis']} cleared" + (f": {why}" if why else ""),
                       provenance=prov, source=src, data={"rule": prev["rule"], "axis": prev["axis"],
                                                          "since_utc": prev["since"], "cleared_by": why})
        if cur["latched"] and cur["evidence"] and cur["evidence"] != prev.get("evidence"):
            self._emit("stage_monitor.evidence", "xy_stage", "info", f"{mv.label}: evidence written: "
                       f"{cur['evidence']}", provenance=prov, source=src, data={"evidence": cur["evidence"]})
        for w in set(cur["warnings"]) - set(prev["warnings"]):
            self._emit("stage_monitor.warning", "xy_stage", "warning", f"{mv.label}: {w}", provenance=prov,
                       source=src)
        for w in set(prev["warnings"]) - set(cur["warnings"]):
            self._emit("stage_monitor.warning_cleared", "xy_stage", "info", f"{mv.label}: no longer: {w}",
                       provenance=prov, source=src)

    def _transitions(self, report: HealthReport) -> None:
        states = {s.key: s.state for s in report.subsystems if s.monitored}
        prev = self._prev_states
        self._prev_states = states
        if report.error:
            self._emit("health.assessment_failed", "host", "warning", report.error, source="HealthService")
            return
        if prev is None:
            self._emit("health.assessed", "instrument", "info", f"first assessment: overall {report.overall.value}; "
                       + ", ".join(f"{k} {v.value}" for k, v in states.items()), provenance=Provenance.DERIVED,
                       data={"overall": report.overall.value, "states": {k: v.value for k, v in states.items()}})
            return
        for key, st in states.items():
            old = prev.get(key)
            if old is not None and old is not st:
                sub = report.subsystem(key)
                reasons = sub.reasons if sub else ()
                sev = {HealthState.FAULT: "error", HealthState.RESTRICTED: "warning", HealthState.OFFLINE: "warning",
                       HealthState.WATCH: "warning"}.get(st, "info")
                self._emit("health.state_changed", key, sev, f"{key}: {old.value} -> {st.value}"
                           + (f": {reasons[0]}" if reasons else ""), provenance=Provenance.DERIVED,
                           data={"from": old.value, "to": st.value, "reasons": list(reasons)})


def _latch_data(st: dict[str, Any]) -> dict[str, Any]:
    return {k: st.get(k) for k in ("rule", "axis", "message", "since_utc", "evidence", "recovery", "releasable")}


def _last_clear_note(mon: Any) -> str:
    """The StageMonitor's own note on why its latch cleared ("latch cleared: ..."), best effort."""
    try:
        notes = list(getattr(mon, "commands", ()))
    except RuntimeError:  # appended to while copying: next time
        return ""
    for _t, text in reversed(notes):
        if isinstance(text, str) and text.startswith("latch cleared: "):
            return text[len("latch cleared: "):]
    return ""


def probe_command(sub: str, did: str, caps: Any, snap: DeviceSnapshot) -> Command:
    """A command for the SafetyManager to judge, built from where the device already is. Never submitted."""
    pos = snap.observed_position
    o = CommandOrigin.UI
    if sub == "xy_stage":
        ax = caps.axis("x")
        unit = ax.limits.unit if ax is not None and ax.limits is not None else "um"
        frame = (ax.coordinate_frame if ax is not None else None) or "stage"
        lo = ax.limits.minimum if ax is not None and ax.limits is not None else 0.0
        x = pos.get("x") if pos is not None and "x" in pos.axes else lo
        y = pos.get("y") if pos is not None and "y" in pos.axes else lo
        return MoveStageAbsolute(device_id=did, origin=o, x=x, y=y, unit=unit, frame=frame)
    if sub == "focus":
        ax = caps.axis("z")
        unit = ax.limits.unit if ax is not None and ax.limits is not None else "um"
        frame = (ax.coordinate_frame if ax is not None else None) or "focus"
        z = pos.get("z") if pos is not None and "z" in pos.axes else (
            ax.limits.minimum if ax is not None and ax.limits is not None else 0.0)
        return MoveFocusAbsolute(device_id=did, origin=o, z=z, unit=unit, frame=frame)
    if sub == "objective_turret":
        cur = int(pos.values[0]) if pos is not None and pos.values and pos.values[0] >= 1 else 0
        known = tuple(caps.turret_positions)
        position = cur if (cur in known or not known) and cur >= 1 else (known[0] if known else 1)
        return SetTurretPosition(device_id=did, origin=o, position=position)
    if sub == "illumination":
        chans = tuple(caps.illumination_channels)
        return SetIllumination(device_id=did, origin=o, channel=chans[0] if chans else "trans",
                               intensity_percent=0.0, enabled=True)
    exp = snap.attr("exposure_ms")
    ms = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) and 0 < float(exp) <= 60_000 \
        else 10.0
    return SetCameraExposure(device_id=did, origin=o, exposure_ms=ms)


def start_health(session: Any, logs_folder: Path | None) -> HealthService:
    """The app's Health service: journal in <config>/Logs/health-journal.sqlite3 (memory only without a folder)."""
    path = Path(logs_folder) / JOURNAL_FILE if logs_folder is not None else None
    try:
        return HealthService(session, journal_path=path).start()
    except Exception:  # noqa: BLE001 - e.g. a bad path: Health runs memory-only rather than stopping the app
        return HealthService(session).start()
