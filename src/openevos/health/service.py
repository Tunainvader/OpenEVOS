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

Phase 1 (ADR-0069): each sample also records metric series (backend counters, following error at rest, free space)
and runs the rules (rules.py) over them and the recent events, before the assessment. A rule's WATCH and its end are
journaled (assessment.raised / assessment.resolved). Backend counters are plain integers the backends already keep;
Health reads them with getattr and never calls a backend method.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import replace
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
from openevos.health.model import Assessment, HealthEvent, HealthReport, HealthState, Provenance
from openevos.health.protection import (
    POLICY_BY_ID,
    RECURRENCE_S,
    Proposal,
    Verification,
    VerifyInput,
    page_capabilities,
    policy_for,
)
from openevos.health.rules import AXES, MetricSeries, RuleEngine, RuleInput, following_metric
from openevos.health.stats import BASELINE_FILE, Baseline, BaselineStore, build_baseline
from openevos.safety.restrictions import RESTRICTIONS_FILE, Restriction, RestrictionRegistry

SAMPLE_PERIOD_S = 1.0
RECENT_EVENTS = 300
MAX_PENDING_COMMANDS = 4096
#: words in a backend's reason that mean its link failed or came back (counted as link errors)
LINK_ERROR_WORDS = ("telemetry failed", "telemetry unavailable", "dropped off usb", "not on usb", "stream failed",
                    "link error", "reconnected", "reopened after")
#: ADR-0067: the stage service's notes for its automatic recovery, and the steps that end one
RECOVERY_NOTE = "automatic recovery: "
RECOVERY_END_STEPS = ("recovered", "escalated", "handed back", "objective not raised back")
RECORD_SCAN_S = 5.0
MOTOR_EXTRA = {"xy_stage": "motor.stage", "focus": "motor.focus", "objective_turret": "motor.objective_turret"}
CARRIAGES = ("motor.cube_carriage", "motor.phase_wheel")
#: Phase 1: events the rules read (kept apart from the page's recent list, which busy devices can roll over)
RULE_KINDS = frozenset({"command.failed", "command.timed_out", "device.condition", "stage_monitor.warning"})
RULE_EVENTS = 2000
#: Phase 1: plain integer counters some backends keep (read with getattr, never a call)
BACKEND_COUNTERS = ("frames_produced", "incomplete_frames", "skipped_grabs", "reconnects")
#: Phase 1: snapshot attributes that identify a device for a baseline's fingerprint
FINGERPRINT_ATTRS = ("model", "fw", "firmware", "serial", "limits_mm")


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
                 now: Callable[[], datetime] = utcnow, baselines: BaselineStore | None = None,
                 rules: RuleEngine | None = None):
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
        #: ADR-0067 automatic recovery, per monitor label: how many notes were read and the last one, the last step,
        #: the recovery records seen (path -> mtime), the latest record, when the folder was last scanned
        self._note_pos: dict[str, tuple[int, Any]] = {}
        self._last_step: dict[str, str] = {}
        self._records: dict[str, dict[str, float]] = {}
        self._last_record: dict[str, dict[str, Any]] = {}
        self._records_t: dict[str, float] = {}
        self._disk: tuple[float, dict[str, DiskView]] | None = None
        self._device_sub: dict[str, str] = {}
        self._link_of: dict[str, str] = {}
        self._mapped: dict[str, tuple[str, ...]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        # Phase 1 (ADR-0069)
        if baselines is None:
            jp = self.journal.path
            baselines = BaselineStore(jp.with_name(BASELINE_FILE) if jp is not None else None)
        self.baselines = baselines
        self.rules = rules if rules is not None else RuleEngine()
        self.series = MetricSeries()
        self._rule_events: deque[HealthEvent] = deque(maxlen=RULE_EVENTS)
        self._series_disk_t: dict[str, datetime] = {}
        self._series_obs: dict[str, datetime] = {}
        #: how the free space is read, and how often (the simulator injector replaces the reader; tests the period)
        self.disk_reader: Callable[[Path, datetime], DiskView] = disk_view
        self.disk_period_s = DISK_PERIOD_S
        # Phase 2 (ADR-0070): the SafetyManager's registry of confirmed restrictions, Health's proposals for it
        reg = getattr(getattr(self.core, "safety", None), "restrictions", None)
        self.registry: RestrictionRegistry | None = reg if isinstance(reg, RestrictionRegistry) else None
        self._proposals: tuple[Proposal, ...] = ()
        self._declined: set[str] = set()  # assessment ids the operator declined to restrict for
        self._acked: dict[str, str] = {}  # assessment id -> when the operator acknowledged it
        self._verify: dict[str, Verification] = {}
        self._plock = threading.Lock()  # proposals, declined, acked, verify (page thread vs sampler)

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
        reg = self.registry
        if reg is not None:
            try:
                reg.now = self._now
                if reg.path is None and self.journal.path is not None:
                    reg.attach(self.journal.path.with_name(RESTRICTIONS_FILE))
                reg.listeners.append(self._on_restriction)
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
        try:
            self.journal.start()
            self._emit("recorder.started", "host", "info", "Health recorder started (Phases 0-2: it restricts only "
                       "what the operator confirms)",
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
        reg = self.registry
        if reg is not None and self._on_restriction in reg.listeners:
            reg.listeners.remove(self._on_restriction)
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
        if recent or kind in RULE_KINDS:
            with self._lock:
                if recent:
                    self._recent.append(ev)
                if kind in RULE_KINDS:
                    self._rule_events.append(ev)
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
            view = self._gather(now)
            try:
                view = self._run_rules(view)
            except Exception as exc:  # noqa: BLE001 - a rule failure leaves the Phase 0 assessment standing
                self._fail(exc)
            try:
                view = self._protect(view)
            except Exception as exc:  # noqa: BLE001 - a protection failure leaves the enforcement as it is
                self._fail(exc)
            report = assess(view)
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
                sim = bool(getattr(svc, "emulated", False))
                mv = MonitorView(label, mon.state(), mon.refusal(),
                                 owns_stage=any(b.device_id in stage_ids for b in getattr(svc, "backends", [])),
                                 carried_over=bool(getattr(mon, "carried_over", False)), simulated=sim,
                                 recovery=self._recovery(label, svc, mon, sim))
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
        if self._disk is None or t - self._disk[0] >= self.disk_period_s:
            disks = {"data": self.disk_reader(self.data_folder, now)}
            if self.journal.path is not None:
                disks["journal"] = self.disk_reader(self.journal.path.parent, now)
            self._disk = (t, disks)
        preview = getattr(getattr(core, "transport", None), "preview", None)
        host = {"memory_percent": memory_load_percent(), "threads": threading.active_count(),
                "bus_errors": getattr(core.bus, "delivery_errors", None),
                "stale_obs": getattr(core.store, "stale_observations", None),
                "preview": dict(preview.stats) if preview is not None else None,
                "journal": self.journal.stats(), "recorder_errors": rec_err, "recorder_error": rec_msg,
                "period_s": self.sample_period_s}
        counters_by_device: dict[str, dict[str, int]] = {}
        for key in ("mono_camera", "colour_camera", "illumination"):
            for did in self._mapped.get(key, ()):
                backend = getattr(core.devices.get(did), "backend", None)
                cs = {n: v for n in BACKEND_COUNTERS
                      if isinstance(v := getattr(backend, n, None), int) and not isinstance(v, bool)}
                if cs:
                    counters_by_device[did] = cs
        return HealthView(now=now, devices=dict(self._mapped), snapshots=snaps, periods=periods,
                          monitors=tuple(monitors), links=self._links(), probe_refusals=refusals,
                          probe_missing=missing, last_reason=last_reason, last_refusal=last_refusal,
                          link_errors=link_errors, counters=counters, disks=dict(self._disk[1]), host=host,
                          host_measured_at=now, backend_counters=counters_by_device)

    # ------------------------------------------------------------------ Phase 2 (ADR-0070): protection
    def _protect(self, view: HealthView) -> HealthView:
        """Verify the active restrictions (lift those whose verification passed), refresh the proposals, and show
        what the SafetyManager enforces now on the capability list."""
        reg = self.registry
        if reg is None:
            return view
        now_s = view.now.timestamp()
        fp = self.fingerprint(view)
        for r in reg.open():
            pol = POLICY_BY_ID.get(r.policy_id)
            if pol is None or not r.active:
                continue
            act = datetime.fromisoformat(r.activated_at).timestamp()
            v = pol.verify(VerifyInput(now_s, act, str(r.trigger.get("key", "")), self.series, self.baselines, fp))
            with self._plock:
                prev = self._verify.get(r.restriction_id)
                self._verify[r.restriction_id] = v
            if v.passed:
                reg.release(r.restriction_id, by="verification", verification=v.to_json())
            elif prev is None or prev.summary.startswith("watching") != v.summary.startswith("watching"):
                reg.record(r.restriction_id, "verification", passed=False, summary=v.summary)
        props = self._make_proposals(view, reg)
        with self._plock:
            old = {p.proposal_id for p in self._proposals}
            self._proposals = props
        for p in props:
            if p.proposal_id not in old:
                self._emit("protection.proposed", p.assessment.subsystem, "warning",
                           f"Health proposes restricting: {p.title} (policy {p.policy.policy_id} v{p.policy.version},"
                           f" after rule {p.assessment.detector}). Nothing is restricted until the operator confirms",
                           provenance=p.assessment.provenance, source="Health protection",
                           data={"proposal": p.proposal_id, "assessment": p.assessment.to_json(),
                                 "capabilities": list(p.capabilities), "runs": list(p.runs)})
        subof = self._sub_of
        confirmed = tuple({"policy": r.policy_id, "version": r.policy_version, "title": r.title, "reason": r.reason,
                           "since": r.activated_at, "release": list(r.release_conditions),
                           "capabilities": tuple(dict.fromkeys(k for c in r.capabilities
                                                               for k in page_capabilities(c, subof)))}
                          for r in reg.open() if r.active)
        return replace(view, confirmed=confirmed)

    def _make_proposals(self, view: HealthView, reg: RestrictionRegistry) -> tuple[Proposal, ...]:
        rs = reg.all()
        open_keys = {(r.policy_id, r.trigger.get("key")) for r in rs if r.open}
        with self._plock:
            declined = set(self._declined)
        out = []
        for a in view.assessments:
            pol = policy_for(a)
            if pol is None or (pol.policy_id, a.key) in open_keys or a.assessment_id in declined:
                continue
            caps, cmds = pol.capabilities(a.key), pol.commands(a.key)
            recurring = any(r.policy_id == pol.policy_id and r.trigger.get("key") == a.key and r.released_at
                            and (view.now - datetime.fromisoformat(r.released_at)).total_seconds() < RECURRENCE_S
                            for r in rs)
            out.append(Proposal(f"{pol.policy_id}:{a.key}", pol, a, pol.title(a.key), caps, cmds,
                                tuple(x.kind for x in reg.conflicting_runs(caps, cmds)), recurring))
        return tuple(out)

    def proposals(self) -> tuple[Proposal, ...]:
        with self._plock:
            return self._proposals

    def protections(self) -> list[tuple[Restriction, Verification | None]]:
        """The open restrictions (pending and active), with their latest verification."""
        reg = self.registry
        if reg is None:
            return []
        with self._plock:
            return [(r, self._verify.get(r.restriction_id)) for r in reg.open()]

    def confirm(self, proposal_id: str, *, stop_runs: bool = False) -> Restriction:
        """The operator confirmed a proposal: request the restriction from the SafetyManager's registry."""
        reg = self.registry
        if reg is None:
            raise RuntimeError("no SafetyManager restriction registry in this session")
        with self._plock:
            p = next((x for x in self._proposals if x.proposal_id == proposal_id), None)
        if p is None:
            raise KeyError(f"no open proposal {proposal_id!r} (its WATCH may have resolved)")
        r = reg.request(p.restriction(self._now()), stop_runs=stop_runs)
        with self._plock:
            self._proposals = tuple(x for x in self._proposals if x.proposal_id != proposal_id)
        stopping = ", the runs that need it were asked to stop at their next safe point" if stop_runs and p.runs \
            else ""
        self._emit("protection.confirmed", p.assessment.subsystem, "warning",
                   f"Operator confirmed the restriction: {p.title} ({r.state}{stopping})", source="operator",
                   data={"restriction": r.to_json()})
        if p.recurring:
            self._emit("service.escalated", p.assessment.subsystem, "warning",
                       f"Recurring: {p.title} was restricted and released within the last "
                       f"{RECURRENCE_S / 3600:.0f} h for the same reason. A technician should inspect it",
                       source="Health protection", data={"restriction": r.restriction_id,
                                                         "policy": p.policy.policy_id, "key": p.assessment.key})
        return r

    def decline(self, proposal_id: str) -> None:
        """The operator declined a proposal: no restriction for this WATCH (a new WATCH proposes again)."""
        with self._plock:
            p = next((x for x in self._proposals if x.proposal_id == proposal_id), None)
            if p is None:
                return
            self._declined.add(p.assessment.assessment_id)
            self._proposals = tuple(x for x in self._proposals if x.proposal_id != proposal_id)
        self._emit("protection.declined", p.assessment.subsystem, "info",
                   f"Operator declined to restrict: {p.title}; the WATCH stays", source="operator",
                   data={"proposal": proposal_id, "assessment_id": p.assessment.assessment_id})

    def lift(self, restriction_id: str, note: str = "") -> Restriction:
        """The operator lifts a restriction without its verification (recorded as such)."""
        if self.registry is None:
            raise RuntimeError("no SafetyManager restriction registry in this session")
        return self.registry.release(restriction_id, by="operator", note=note)

    def stop_runs(self, restriction_id: str) -> list[str]:
        """Ask the runs a pending restriction waits for to stop at their next safe point."""
        if self.registry is None:
            return []
        stopped = self.registry.stop_runs(restriction_id)
        if stopped:
            self._emit("protection.runs_stopped", "host", "warning",
                       f"Operator asked {', '.join(stopped)} to stop at the next safe point so a restriction can take "
                       "effect; everything already taken is kept", source="operator",
                       data={"restriction": restriction_id, "runs": stopped})
        return stopped

    def acknowledge(self, assessment_id: str) -> None:
        """The operator saw this WATCH. Acknowledging clears nothing: the WATCH stays until its rule resolves it."""
        a = next((x for x in self.latest.assessments if x.assessment_id == assessment_id), None)
        with self._plock:
            if assessment_id in self._acked:
                return
            self._acked[assessment_id] = self._now().isoformat()
        self._emit("assessment.acknowledged", a.subsystem if a else "host", "info",
                   f"Operator acknowledged the WATCH{': ' + a.title if a else ''} (it stays open until its rule "
                   "resolves it)", source="operator", data={"assessment_id": assessment_id})

    def acknowledged(self, assessment_id: str) -> str:
        with self._plock:
            return self._acked.get(assessment_id, "")

    def _on_restriction(self, what: str, r: Restriction) -> None:
        key = str(r.trigger.get("key", ""))
        if r.policy_id.startswith("xy."):
            subsystem = "xy_stage"
        elif r.policy_id.startswith("camera."):
            subsystem = self._sub_of(key) or "mono_camera"
        elif r.policy_id.startswith("storage."):
            subsystem = "storage"
        else:
            subsystem = "host"
        how = "its verification passed" if r.released_by == "verification" else \
            "by the operator, without verification"
        text = {"active": f"Restriction in force (SafetyManager): {r.title}",
                "pending": f"Restriction confirmed, waiting for the run that needs it to end: {r.title}",
                "released": f"Restriction lifted ({how}): {r.title}"}
        self._emit(f"restriction.{what}", subsystem, "info" if what == "released" else "warning",
                   text.get(what, f"Restriction {what}: {r.title}"), source="SafetyManager (Health restriction)",
                   data=r.to_json())

    # ------------------------------------------------------------------ Phase 1 (ADR-0069): series, rules, baselines
    def _run_rules(self, view: HealthView) -> HealthView:
        self._record_series(view)
        with self._lock:
            events = tuple(self._rule_events)
        inp = RuleInput(view.now, view, events, self.series, self.baselines, self.fingerprint(view))
        active, statuses, transitions = self.rules.evaluate(inp)
        for what, a in transitions:
            self._journal_assessment(what, a)
        return replace(view, assessments=active, detectors=statuses)

    def _journal_assessment(self, what: str, a: Assessment) -> None:
        sev = "warning" if what == "raised" else "info"
        text = (f"WATCH raised by rule {a.detector} v{a.detector_version}: {a.title}" if what == "raised" else
                f"WATCH resolved, rule {a.detector} v{a.detector_version} back within its limit: {a.title}")
        self._emit(f"assessment.{what}", a.subsystem, sev, text, provenance=a.provenance,
                   source=f"Health rule {a.detector} v{a.detector_version}", data=a.to_json())

    def _record_series(self, view: HealthView) -> None:
        t = view.now.timestamp()
        series = self.series
        for did in view.devices.get("xy_stage", ()):
            snap = view.snapshots.get(did)
            if snap is None:
                continue
            a, c = snap.attr("counts"), snap.attr("commanded_counts")
            if not (isinstance(a, tuple) and isinstance(c, tuple) and len(a) == len(c) == len(AXES)):
                continue
            if snap.observed_at is None or self._series_obs.get(did) == snap.observed_at:
                continue  # no new reading since the last sample: not counted twice
            self._series_obs[did] = snap.observed_at
            regime = "idle" if snap.activity.value == "idle" else str(snap.activity.value)
            for ax, x, y in zip(AXES, a, c, strict=True):
                series.record(following_metric(ax), t, abs(int(x) - int(y)), regime)
        for m in view.monitors:
            if not m.owns_stage:
                continue
            ra = m.state.get("reapproaches")
            if isinstance(ra, dict):
                for ax in AXES:
                    series.record(f"xy_stage.reapproaches.{ax}", t, len(ra.get(ax) or ()))
        for did, cs in view.backend_counters.items():
            for name, v in cs.items():
                series.record(f"backend.{name}.{did}", t, v)
        for name, d in view.disks.items():
            if d.free_bytes is None or d.measured_at is None or self._series_disk_t.get(name) == d.measured_at:
                continue
            self._series_disk_t[name] = d.measured_at
            series.record(f"storage.free_bytes.{name}", d.measured_at.timestamp(), d.free_bytes)

    def fingerprint(self, view: HealthView | None = None) -> dict[str, str]:
        """What identifies the instrument for a baseline: each mapped device's backend kind, link and identifying
        attributes (model, firmware, serial, drive limits), keyed "<device id>.<what>". A baseline keeps the keys of
        its own subsystem's devices; any of them changing ends it."""
        snaps = view.snapshots if view is not None else self.core.store.snapshot_all()
        devices = view.devices if view is not None else self._mapped
        out: dict[str, str] = {}
        for ds in devices.values():
            for did in ds:
                snap = snaps.get(did)
                if snap is None:
                    continue
                out[f"{did}.backend"] = snap.backend_kind.value
                if did in self._link_of:
                    out[f"{did}.link"] = self._link_of[did]
                for name in FINGERPRINT_ATTRS:
                    v = snap.attr(name)
                    if v is not None:
                        out[f"{did}.{name}"] = str(v)
        return out

    def capture_baseline(self, metric: str, regime: str = "idle", *, window_s: float = 3600.0,
                         notes: str = "") -> Baseline:
        """Build and store the next version of a baseline from the recorded series (at least 30 samples within
        `window_s`). Always "not-qualified"; one made on the simulator says so in its source and can never be
        qualified. Reads only; nothing is commanded."""
        subsystem = metric.split(".", 1)[0]
        pts = self.series.points(metric, since=self._now().timestamp() - window_s, regime=regime)
        devices = set(self._mapped.get(subsystem, ()))
        fp = {k: v for k, v in self.fingerprint().items() if k.rsplit(".", 1)[0] in devices}
        sim = any(v == BackendKind.SIMULATED.value for k, v in fp.items() if k.endswith(".backend"))
        unit = "counts" if "following_error" in metric else ""
        b = self.baselines.add(build_baseline(metric, regime, unit, [v for _, v in pts],
                                              source="simulator" if sim else "instrument", fingerprint=fp,
                                              now=self._now(), notes=notes))
        self._emit("baseline.recorded", subsystem, "info",
                   f"baseline {b.baseline_id} v{b.version} recorded from {b.n} samples ({b.source}, "
                   f"{b.qualification}): median {b.median:g}, p95 {b.p95:g} {b.unit}",
                   provenance=Provenance.DEMO if sim else Provenance.DERIVED, source="HealthService.capture_baseline",
                   data={"baseline_id": b.baseline_id, "version": b.version, "n": b.n, "median": b.median,
                         "mad": b.mad, "p05": b.p05, "p95": b.p95, "source": b.source,
                         "qualification": b.qualification, "fingerprint": b.fingerprint})
        return b

    def rule_events(self) -> tuple[HealthEvent, ...]:
        with self._lock:
            return tuple(self._rule_events)

    # ------------------------------------------------------------------ ADR-0067 automatic recovery (read only)
    def _recovery(self, label: str, svc: Any, mon: Any, sim: bool) -> dict[str, Any]:
        """What the stage service exposes about its automatic recovery: the public attempts and blocker, the monitor's
        "automatic recovery: ..." notes, and the stage-recovery-*.json records. Journaled as automatic actions (the
        operator's choice, 2026-11-26), never as rule breaches. Empty for a service without ADR-0067."""
        if not hasattr(svc, "auto_recovery_blocker"):
            return {}
        prov = Provenance.DEMO if sim else Provenance.MEASURED
        self._recovery_steps(label, mon, prov)
        self._recovery_records(label, mon, prov)
        enabled = bool(getattr(getattr(self.session.settings, "basic", None), "stage_auto_recovery", False))
        blocker = svc.auto_recovery_blocker() if mon.latched else ""
        if not enabled:
            status = "switched off (Settings › Interface)"
        elif not mon.latched:
            status = "on; nothing to recover"
        elif blocker:
            status = f"not attempted: {blocker}"
        else:
            status = ("allowed for this lock: the app starts it by itself (light off, objective down, re-home X then "
                      "Y, a slow test of the tripped axis, return)")
        step = self._last_step.get(label, "")
        return {"enabled": enabled, "status": status, "attempts": dict(getattr(svc, "auto_recovery_attempts", {})),
                "last": self._last_record.get(label), "last_step": step,
                "in_progress": bool(step) and not step.startswith(RECOVERY_END_STEPS)}

    def _recovery_steps(self, label: str, mon: Any, prov: Provenance) -> None:
        try:
            notes = list(getattr(mon, "commands", ()))
        except RuntimeError:  # appended to while copying: next sample
            return
        # by position, not by time: the monitor's clock is coarse (~16 ms on Windows), so many notes share a time
        count, last = self._note_pos.get(label, (0, None))
        maxlen = getattr(getattr(mon, "commands", None), "maxlen", None)
        if maxlen is not None and len(notes) >= maxlen:  # full: old notes rotate out; find where we stopped
            start = next((i + 1 for i in range(len(notes) - 1, -1, -1) if notes[i] == last), 0)
        else:
            start = min(count, len(notes))
        self._note_pos[label] = (len(notes), notes[-1] if notes else None)
        for entry in notes[start:]:
            text = entry[1] if isinstance(entry, tuple) and len(entry) == 2 else None
            if not isinstance(text, str) or not text.startswith(RECOVERY_NOTE):
                continue
            step = text[len(RECOVERY_NOTE):]
            self._last_step[label] = step
            sev = "warning" if step.startswith(("escalated", "light off not confirmed", "objective not raised")) \
                else "info"
            self._emit("stage_recovery.step", "xy_stage", sev, f"Automatic action (ADR-0067): {step}",
                       provenance=prov, source="StageMonitor notes (automatic recovery)",
                       data={"automatic": True, "adr": "ADR-0067", "step": step, "monitor": label})

    def _recovery_records(self, label: str, mon: Any, prov: Provenance) -> None:
        folder = getattr(mon, "evidence_dir", None)
        t = time.monotonic()
        if folder is None or t - self._records_t.get(label, -1e9) < RECORD_SCAN_S:
            return
        self._records_t[label] = t
        first = label not in self._records
        known = self._records.setdefault(label, {})
        try:
            files = sorted(Path(folder).glob("stage-recovery-*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for p in files:
            try:
                mtime = p.stat().st_mtime
                if known.get(str(p)) == mtime:
                    continue
                known[str(p)] = mtime
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            summary = {k: rec.get(k) for k in ("utc", "stage", "ok", "message", "trip_class", "who")}
            summary.update(lock=rec.get("lock") or {}, path=str(p), steps=len(rec.get("steps") or []))
            self._last_record[label] = summary
            if first:  # records from before this Health service started: shown as the last one, not re-journaled
                continue
            raw_lock = rec.get("lock")
            lock: dict[str, Any] = raw_lock if isinstance(raw_lock, dict) else {}
            sev = "info" if rec.get("ok") or rec.get("stage") != "escalated" else "warning"
            self._emit("stage_recovery.record", "xy_stage", sev,
                       f"Automatic action (ADR-0067): recovery of {lock.get('axis', '?')} after {lock.get('rule', '?')}"
                       f" {summary['stage']}" + (f": {summary['message']}" if summary.get("message") else ""),
                       provenance=prov, source=f"stage-recovery record {p.name}",
                       data={"automatic": True, "adr": "ADR-0067", **summary})

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
