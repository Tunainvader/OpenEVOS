"""Health assessment: pure functions from what the sources say now to a HealthReport (ADR-0068 §2).

No I/O, no threads, no device access: `service.HealthService` gathers a `HealthView` (device snapshots, the
StageMonitor's state, the SafetyManager's decision on probe commands, counters, disk and memory readings) and calls
`assess(view)`. That keeps every rule testable with hand-made inputs.

Rules (Phase 0; none of them restricts anything, they only describe):
* a device that is not registered or not connected is OFFLINE; one in its bootloader is FAULT (quarantined);
* recovery required or a device FAULT -> FAULT; a closed-loop axis that is not homed -> RESTRICTED;
* a backend WARNING -> WATCH; a connected device whose health is still unknown -> UNKNOWN;
* a periodic source (the Technosoft poll) that has not reported for max(STALE_MIN_S, STALE_FACTOR x its period) is
  stale -> UNKNOWN, whatever it said last ("unknown remains unknown");
* the StageMonitor's latch is authoritative and does not go stale: a drift or landing lock -> RESTRICTED, a drive-fault
  lock (over-current, oscillation, communication abort, power stage off, ...) -> FAULT; its warnings -> WATCH;
* a subsystem with no source in this session is NOT MONITORED (UNKNOWN, left out of the overall state);
* a capability is RESTRICTED only when the SafetyManager refuses its probe command (quoted verbatim); otherwise it
  follows what it depends on: OFFLINE -> UNAVAILABLE, FAULT/RESTRICTED -> AT RISK, UNKNOWN -> UNKNOWN, WATCH -> WATCH.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from openevos.core.models import Activity, BackendKind, Connection, DeviceSnapshot, FirmwareMode, Health
from openevos.health import inventory
from openevos.health.model import (
    CAPABILITIES,
    SEVERITY,
    SUBSYSTEMS,
    CapabilityDef,
    CapabilityState,
    CapabilityStatus,
    Finding,
    HealthReport,
    HealthState,
    Metric,
    Provenance,
    Restriction,
    SubsystemHealth,
    capabilities_affected_by,
    worst,
)

H, W, R, F, U, O = (HealthState.HEALTHY, HealthState.WATCH, HealthState.RESTRICTED, HealthState.FAULT,
                    HealthState.UNKNOWN, HealthState.OFFLINE)

#: a periodic source is stale after max(STALE_MIN_S, STALE_FACTOR x its period) without an observation
STALE_MIN_S = 10.0
STALE_FACTOR = 5.0
#: link errors and command time-outs this recent put Communications in WATCH
RECENT_S = 600.0
#: free space on a data or journal drive (provisional Phase 0 values, not qualified baselines)
DISK_WATCH_BYTES = 10 * 1024**3
DISK_RESTRICTED_BYTES = 2 * 1024**3
MEMORY_WATCH_PERCENT = 90.0
#: the Health service measures free disk space this often
DISK_PERIOD_S = 10.0
#: StageMonitor rules that are drift or landing locks (stage_monitor.RELEASABLE_RULES); every other latch is a fault
DRIFT_RULES = frozenset({"idle_encoder_drift", "command_mismatch", "cross_axis_encoder_drift", "landing_error"})
#: the SafetyManager checks that describe the device's state now; target-specific ones (limits, confirmation,
#: interlocks against the probe's own target) are left out because a probe has no real target
STATE_CHECKS = frozenset({"device", "kind", "capability", "device_state", "recovery", "firmware", "provenance"})


def stale_after(period_s: float) -> float:
    return max(STALE_MIN_S, STALE_FACTOR * period_s)


@dataclass(frozen=True)
class MonitorView:
    """One StageMonitor as Health sees it (StageMonitor.state() and .refusal(), read under the monitor's own lock)."""

    label: str
    state: dict[str, Any]
    refusal: str
    owns_stage: bool = True  # the monitor watches the stage the app drives
    carried_over: bool = False
    simulated: bool = False


@dataclass(frozen=True)
class LinkView:
    key: str
    title: str
    devices: tuple[str, ...]


@dataclass(frozen=True)
class DiskView:
    path: str
    free_bytes: int | None = None
    total_bytes: int | None = None
    error: str = ""
    measured_at: datetime | None = None


@dataclass(frozen=True)
class HealthView:
    now: datetime
    #: subsystem key -> the device ids mapped to it in this session
    devices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    snapshots: dict[str, DeviceSnapshot] = field(default_factory=dict)
    #: device id -> the period (s) its source reports at; devices not listed report on change only
    periods: dict[str, float] = field(default_factory=dict)
    monitors: tuple[MonitorView, ...] = ()
    links: tuple[LinkView, ...] = ()
    #: subsystem key -> the SafetyManager's refusal reasons (state checks only) for a probe command to its device
    probe_refusals: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: subsystem key -> why no probe could be made (e.g. the device is not registered)
    probe_missing: dict[str, str] = field(default_factory=dict)
    #: device id -> the backend's latest reason text and when it came
    last_reason: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    #: device id -> the router's latest refusal of a command to it
    last_refusal: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    #: link key -> recent link errors (time, text)
    link_errors: dict[str, tuple[tuple[datetime, str], ...]] = field(default_factory=dict)
    #: bus:* counters kept by the recorder
    counters: dict[str, Any] = field(default_factory=dict)
    disks: dict[str, DiskView] = field(default_factory=dict)
    #: health:* host readings (memory_percent, threads, bus_errors, stale_obs, preview, journal, recorder_errors)
    host: dict[str, Any] = field(default_factory=dict)
    host_measured_at: datetime | None = None


# ====================================================================================================== devices
def _prov(snap: DeviceSnapshot | None, physical: Provenance = Provenance.MEASURED) -> Provenance:
    return Provenance.DEMO if snap is not None and snap.backend_kind is BackendKind.SIMULATED else physical


def _age_s(now: datetime, t: datetime | None) -> float | None:
    return None if t is None else max(0.0, (now - t).total_seconds())


def device_findings(did: str, snap: DeviceSnapshot | None, now: datetime, period_s: float | None = None,
                    reason: str = "") -> tuple[list[Finding], bool]:
    """The findings for one device and whether its telemetry is stale."""
    src = f"RuntimeStateStore {did}"
    prov = _prov(snap)
    why = f": {reason}" if reason and reason != "telemetry" else ""
    if snap is None:
        return [Finding(O, f"{did} is not registered in this session", Provenance.MEASURED, "DeviceManager")], False
    if snap.firmware_mode in (FirmwareMode.BOOTLOADER, FirmwareMode.RECOVERY):
        return [Finding(F, f"{did} runs its {snap.firmware_mode.value} firmware: quarantined, commands are refused",
                        prov, src)], False
    if snap.connection is Connection.DISCONNECTED:
        return [Finding(O, f"{did} is disconnected{why}", prov, src)], False
    if snap.connection in (Connection.CONNECTING, Connection.UNKNOWN):
        return [Finding(U, f"{did} connection is {snap.connection.value}{why}", prov, src)], False
    out: list[Finding] = []
    if snap.recovery_required:
        out.append(Finding(F, f"{did} needs recovery before it accepts commands" + (f" ({snap.note})" if snap.note
                                                                                  else ""), prov, src))
    if snap.health is Health.FAULT:
        out.append(Finding(F, f"{did} reports a fault{why}", prov, src))
    if snap.attr("homed") is False:
        out.append(Finding(R, f"{did} is not homed: its moves are refused", prov, src))
    if snap.health is Health.WARNING and not snap.attr("drift_lock"):  # a drift lock is the monitor's finding
        out.append(Finding(W, f"{did} reports a warning{why or (': ' + str(snap.attr('health_reason', '')))}",
                           prov, src))
    if snap.health is Health.UNKNOWN:
        out.append(Finding(U, f"{did} has not reported its health yet", prov, src))
    if not out:
        busy = f", {snap.activity.value}" if snap.activity not in (Activity.IDLE, Activity.UNKNOWN) else ""
        sim = " (simulator)" if snap.backend_kind is BackendKind.SIMULATED else ""
        out.append(Finding(H, f"{did} connected, normal{busy}{sim}", prov, src))
    if period_s is not None:
        age = _age_s(now, snap.observed_at)
        limit = stale_after(period_s)
        if age is None or age > limit:
            last = worst(f.state for f in out)
            seen = "never observed" if age is None else f"no observation for {age:.0f} s"
            return [Finding(U, f"{did} telemetry is stale: {seen} (expected every {period_s:g} s; stale after "
                               f"{limit:g} s). Last known: {last.value}", prov, src)], True
    return out, False


def monitor_findings(m: MonitorView) -> list[Finding]:
    prov = Provenance.DEMO if m.simulated else Provenance.MEASURED
    st = m.state
    src = f"StageMonitor ({m.label})"
    if st.get("latched"):
        rule, axis = str(st.get("rule", "")), str(st.get("axis", ""))
        since = str(st.get("since_utc", ""))[:19].replace("T", " ")
        carried = " (carried over from an earlier session)" if m.carried_over else ""
        if rule in DRIFT_RULES:
            return [Finding(R, f"X/Y moves locked by the drift monitor since {since} UTC{carried}: {rule} on {axis}: "
                               f"{st.get('message', '')}", prov, src)]
        return [Finding(F, f"X/Y moves locked by a drive-fault latch since {since} UTC{carried}: {rule} on {axis}: "
                           f"{st.get('message', '')}", prov, src)]
    warn = [Finding(W, f"drift monitor: {w}", prov, src) for w in st.get("warnings") or ()]
    return warn or [Finding(H, "drift monitor watching, no lock", prov, src)]


def _fmt(v: Any) -> Any:
    if isinstance(v, tuple):
        return " / ".join(str(_fmt(x)) for x in v)
    if isinstance(v, float):
        return round(v, 3)
    return v


def device_metrics(subsystem: str, dids: tuple[str, ...], snaps: dict[str, DeviceSnapshot],
                   monitor: MonitorView | None = None, sampled_at: datetime | None = None) -> list[Metric]:
    out: list[Metric] = []
    many = len(dids) > 1
    for sig in inventory.signals(subsystem):
        if not sig.extract or sig.extract.startswith(("health:", "bus:")):
            continue
        kind, _, key = sig.extract.partition(":")
        if kind == "monitor":
            if monitor is None:
                continue
            v: Any = monitor.state.get(key)
            if key == "reapproaches":
                v = sum(len(x) for x in (v or {}).values()) if isinstance(v, dict) else v
            if key == "warnings":
                v = len(v or ())
            out.append(Metric(sig.name, _fmt(v), sig.unit, Provenance.DEMO if monitor.simulated else sig.provenance,
                              f"StageMonitor ({monitor.label})", sampled_at))
            continue
        for did in dids:
            snap = snaps.get(did)
            if snap is None:
                continue
            prov = _prov(snap, sig.provenance)
            name = f"{did} {sig.name}" if many else sig.name
            at = snap.observed_at
            src = f"RuntimeStateStore {did}"
            if kind == "pos":
                pos = snap.observed_position
                if pos is None:
                    continue
                for ax, val in zip(pos.axes, pos.values, strict=True):
                    if key in ("*", ax):
                        out.append(Metric(f"{name} {ax.upper()}" if key == "*" else name, round(val, 3), pos.unit,
                                          prov, src, at))
            elif kind == "attr":
                v = snap.attr(key)
                if v is None:
                    continue
                out.append(Metric(name, _fmt(v), sig.unit, prov, src, at))
            elif kind == "state":
                out.append(Metric(name, _fmt(getattr(snap, key).value if hasattr(getattr(snap, key), "value")
                                             else getattr(snap, key)), sig.unit, prov, src, at))
            elif kind == "derived" and key == "following_error":
                a, c = snap.attr("counts"), snap.attr("commanded_counts")
                if isinstance(a, tuple) and isinstance(c, tuple) and len(a) == len(c):
                    out.append(Metric(name, _fmt(tuple(abs(int(x) - int(y)) for x, y in zip(a, c, strict=True))),
                                      sig.unit, Provenance.DEMO if prov is Provenance.DEMO else Provenance.DERIVED,
                                      "Health, from attributes counts and commanded_counts", at))
    return out


def _device_subsystem(key: str, title: str, view: HealthView) -> SubsystemHealth:
    dids = view.devices.get(key, ())
    caps = capabilities_affected_by(key)
    nm = inventory.not_monitored(key)
    if not dids:
        return SubsystemHealth(key, title, U, monitored=False, provenance=Provenance.UNAVAILABLE,
                               source="no device for it in this session", findings=(
                                   Finding(U, "NOT MONITORED: no device for it in this session", Provenance.UNAVAILABLE,
                                           "session profile"),), not_monitored=nm, capabilities=caps)
    findings: list[Finding] = []
    stale = False
    times: list[datetime] = []
    periods = [view.periods[d] for d in dids if d in view.periods]
    for did in dids:
        snap = view.snapshots.get(did)
        reason = view.last_reason.get(did, ("", view.now))[0]
        f, s = device_findings(did, snap, view.now, view.periods.get(did), reason)
        findings += f
        stale = stale or s
        if snap is not None and snap.observed_at is not None:
            times.append(snap.observed_at)
    monitor = None
    if key == "xy_stage":
        monitor = next((m for m in view.monitors if m.owns_stage), None)
        if monitor is not None:
            findings += monitor_findings(monitor)
    snaps = [view.snapshots[d] for d in dids if d in view.snapshots]
    simulated = bool(snaps) and all(s.backend_kind is BackendKind.SIMULATED for s in snaps)
    state = worst(f.state for f in findings)
    findings.sort(key=lambda f: -_sev(f.state))
    src = ", ".join(f"RuntimeStateStore {d}" for d in dids) + (f"; StageMonitor ({monitor.label})" if monitor else "")
    return SubsystemHealth(key, title, state, monitored=True,
                           provenance=Provenance.DEMO if simulated else Provenance.MEASURED, source=src,
                           observed_at=max(times) if times else None,
                           expected_period_s=min(periods) if periods else None, stale=stale,
                           findings=tuple(findings),
                           metrics=tuple(device_metrics(key, dids, view.snapshots, monitor, view.now)),
                           not_monitored=nm,
                           devices=dids, simulated=simulated, capabilities=caps)


def _sev(s: HealthState) -> int:
    return SEVERITY[s]


# ====================================================================================================== cross-cutting
def _is_stale(view: HealthView, did: str) -> bool:
    """A connected, periodic device with no observation within its stale limit (never observed counts as stale)."""
    snap = view.snapshots.get(did)
    if snap is None or did not in view.periods or snap.connection is not Connection.CONNECTED:
        return False
    age = _age_s(view.now, snap.observed_at)
    return age is None or age > stale_after(view.periods[did])


def _communications(view: HealthView) -> SubsystemHealth:
    key, title = "communications", "Communications"
    nm = inventory.not_monitored(key)
    affected = sorted({c for sub, dids in view.devices.items() if dids and sub not in ("incubator",)
                       for c in capabilities_affected_by(sub)})
    if not view.links:
        return SubsystemHealth(key, title, U, monitored=False, provenance=Provenance.UNAVAILABLE,
                               source="no device link in this session",
                               findings=(Finding(U, "NOT MONITORED: no device link in this session (the simulator "
                                                    "rig's devices have none)", Provenance.UNAVAILABLE,
                                                 "session"),), not_monitored=nm, capabilities=tuple(affected))
    findings: list[Finding] = []
    metrics: list[Metric] = []
    times: list[datetime] = []
    sims: list[bool] = []
    recent = view.now - timedelta(seconds=RECENT_S)
    for link in view.links:
        snaps = [view.snapshots.get(d) for d in link.devices]
        known = [s for s in snaps if s is not None]
        sim = bool(known) and all(s.backend_kind is BackendKind.SIMULATED for s in known)
        sims.append(sim)
        prov = Provenance.DEMO if sim else Provenance.MEASURED
        src = f"RuntimeStateStore connection of {', '.join(link.devices)}"
        conns = {s.connection for s in known}
        stale = [d for d in link.devices if d in view.periods and _is_stale(view, d)]
        if not known:
            st, text = O, f"{link.title}: no device registered"
        elif Connection.DISCONNECTED in conns:
            st, text = O, f"{link.title}: disconnected"
        elif conns - {Connection.CONNECTED}:
            st, text = U, f"{link.title}: connecting"
        elif stale:
            st, text = U, f"{link.title}: no telemetry from {', '.join(stale)} (stale)"
        else:
            st, text = H, f"{link.title}: connected"
        findings.append(Finding(st, text, prov, src))
        errs = [e for e in view.link_errors.get(link.key, ()) if e[0] >= recent]
        if errs:
            findings.append(Finding(W, f"{link.title}: {len(errs)} link error(s) in the last {RECENT_S / 60:.0f} min; "
                                       f"last: {errs[-1][1]}", prov, "EventBus DeviceStateChanged reasons"))
        metrics.append(Metric(f"{link.title}", text.split(": ", 1)[1], "", prov, src,
                              max((s.observed_at for s in known if s.observed_at), default=None)))
        metrics.append(Metric(f"{link.title} errors (last {RECENT_S / 60:.0f} min)", len(errs), "count", prov,
                              "EventBus DeviceStateChanged reasons"))
        times += [s.observed_at for s in known if s.observed_at]
    c = view.counters
    timeouts = [t for t in c.get("timeouts", ()) if t[0] >= recent]
    if timeouts:
        findings.append(Finding(W, f"{len(timeouts)} command time-out(s) in the last {RECENT_S / 60:.0f} min; last: "
                                   f"{timeouts[-1][1]}", Provenance.MEASURED, "EventBus CommandLifecycleEvent"))
    for name, k, unit in (("Command refusals (session)", "refusals", "count"),
                          ("Command failures and time-outs (session)", "failures", "count")):
        metrics.append(Metric(name, int(c.get(k, 0)), unit, Provenance.MEASURED, "EventBus CommandLifecycleEvent"))
    last = c.get("last_duration")
    if last is not None:
        metrics.append(Metric("Last command duration", round(float(last[1]), 3), "s", Provenance.DERIVED,
                              "Health, from CommandLifecycleEvent timestamps", last[2], note=str(last[0])))
    sim_all = all(sims)
    state = worst(f.state for f in findings)
    findings.sort(key=lambda f: -_sev(f.state))
    periods = [view.periods[d] for lk in view.links for d in lk.devices if d in view.periods]
    return SubsystemHealth(key, title, state, monitored=True,
                           provenance=Provenance.DEMO if sim_all else Provenance.MEASURED,
                           source="RuntimeStateStore connections; EventBus reasons and command lifecycle",
                           observed_at=max(times) if times else None,
                           expected_period_s=min(periods) if periods else None, findings=tuple(findings),
                           metrics=tuple(metrics), not_monitored=nm,
                           devices=tuple(d for lk in view.links for d in lk.devices), simulated=sim_all,
                           capabilities=tuple(affected))


def _gb(b: int | None) -> float | None:
    return None if b is None else round(b / 1024**3, 1)


def _storage(view: HealthView) -> SubsystemHealth:
    key, title = "storage", "Storage & data"
    findings: list[Finding] = []
    metrics: list[Metric] = []
    times: list[datetime] = []
    src_disk = "Health: shutil.disk_usage"
    for k, name in (("data", "Free space (data folder drive)"), ("journal", "Free space (journal drive)")):
        d = view.disks.get(k)
        if d is None:
            continue
        if d.measured_at:
            times.append(d.measured_at)
        age = _age_s(view.now, d.measured_at)
        if d.free_bytes is not None and (age is None or age > stale_after(DISK_PERIOD_S)):
            findings.append(Finding(U, f"{name}: last measured {'never' if age is None else f'{age:.0f} s ago'} "
                                       "(stale)", Provenance.MEASURED, src_disk))
            metrics.append(Metric(name, _gb(d.free_bytes), "GB", Provenance.MEASURED, f"{src_disk} on {d.path}",
                                  d.measured_at, note="stale"))
            continue
        if d.error or d.free_bytes is None:
            findings.append(Finding(U, f"{name}: not measured ({d.error or 'no reading'}) at {d.path}",
                                    Provenance.UNAVAILABLE, src_disk))
            metrics.append(Metric(name, None, "GB", Provenance.UNAVAILABLE, f"{src_disk} on {d.path}", d.measured_at))
            continue
        metrics.append(Metric(name, _gb(d.free_bytes), "GB", Provenance.MEASURED, f"{src_disk} on {d.path}",
                              d.measured_at, note=f"of {_gb(d.total_bytes)} GB" if d.total_bytes else ""))
        if d.free_bytes < DISK_RESTRICTED_BYTES:
            findings.append(Finding(R, f"{name}: only {_gb(d.free_bytes)} GB free at {d.path}: saving may fail",
                                    Provenance.DERIVED, src_disk))
        elif d.free_bytes < DISK_WATCH_BYTES:
            findings.append(Finding(W, f"{name}: {_gb(d.free_bytes)} GB free at {d.path}", Provenance.DERIVED,
                                    src_disk))
    c = view.counters
    n_faults = int(c.get("storage_faults", 0))
    metrics.append(Metric("Storage faults (session)", n_faults, "count", Provenance.MEASURED,
                          "EventBus StorageFaultEvent"))
    if n_faults:
        findings.append(Finding(W, f"{n_faults} storage fault(s) this session; last: {c.get('last_storage_fault', '')}",
                                Provenance.MEASURED, "EventBus StorageFaultEvent"))
    failed = int(c.get("frames_failed", 0))
    metrics.append(Metric("Frames failed to write (session)", failed, "frames", Provenance.MEASURED,
                          "EventBus AcquisitionStatusEvent"))
    if failed:
        findings.append(Finding(W, f"{failed} frame(s) failed to write this session", Provenance.MEASURED,
                                "EventBus AcquisitionStatusEvent"))
    prev = view.host.get("preview")
    if isinstance(prev, dict):
        metrics.append(Metric("Preview frames dropped (display only)", prev.get("dropped"), "frames",
                              Provenance.MEASURED, "FrameTransport.preview.stats",
                              view.host_measured_at, note=f"of {prev.get('received')} received; not scientific data"))
    if not findings and not any(m.provenance is Provenance.MEASURED and m.name.startswith("Free") for m in metrics):
        findings.append(Finding(U, "free space not measured yet", Provenance.UNAVAILABLE, src_disk))
    if not findings:
        findings.append(Finding(H, "free space above 10 GB; no storage fault this session", Provenance.DERIVED,
                                src_disk))
    findings.sort(key=lambda f: -_sev(f.state))
    return SubsystemHealth(key, title, worst(f.state for f in findings), monitored=True,
                           provenance=Provenance.MEASURED, source="Health: disk usage; EventBus storage events",
                           observed_at=max(times) if times else None, expected_period_s=DISK_PERIOD_S,
                           findings=tuple(findings), metrics=tuple(metrics),
                           not_monitored=inventory.not_monitored(key), capabilities=capabilities_affected_by(key))


def _host(view: HealthView) -> SubsystemHealth:
    key, title = "host", "Host & application"
    h = view.host
    at = view.host_measured_at
    findings: list[Finding] = []
    metrics: list[Metric] = []
    mem = h.get("memory_percent")
    metrics.append(Metric("Memory in use", None if mem is None else round(float(mem), 1), "%",
                          Provenance.UNAVAILABLE if mem is None else Provenance.MEASURED,
                          "Health: GlobalMemoryStatusEx", at, note="" if mem is not None else "not available here"))
    if mem is not None and float(mem) >= MEMORY_WATCH_PERCENT:
        findings.append(Finding(W, f"memory {float(mem):.0f} % in use", Provenance.MEASURED,
                                "Health: GlobalMemoryStatusEx"))
    for name, k, src in (("Threads in the app", "threads", "Health: threading.active_count()"),
                         ("Event subscriber errors", "bus_errors", "EventBus.delivery_errors"),
                         ("Stale observations rejected", "stale_obs", "RuntimeStateStore.stale_observations")):
        if h.get(k) is not None:
            metrics.append(Metric(name, int(h[k]), "count", Provenance.MEASURED, src, at))
    if int(h.get("bus_errors") or 0):
        findings.append(Finding(W, f"{int(h['bus_errors'])} event-subscriber error(s) this session (publishers were "
                                   "not affected)", Provenance.MEASURED, "EventBus.delivery_errors"))
    j = h.get("journal")
    if isinstance(j, dict):
        state = str(j.get("state", ""))
        metrics.append(Metric("Health journal", f"{state}: {j.get('written', 0)} written, {j.get('dropped', 0)} "
                              f"dropped, {j.get('queued', 0)}/{j.get('max_queue', 0)} queued", "events",
                              Provenance.MEASURED, f"Health journal {j.get('path') or '(memory only)'}", at))
        if state == "failed":
            findings.append(Finding(W, f"Health journal failed ({j.get('error', '')}): events are no longer saved; "
                                       "safety is not affected", Provenance.MEASURED, "Health journal"))
        elif int(j.get("dropped") or 0):
            findings.append(Finding(W, f"Health journal dropped {j['dropped']} event(s) (queue full); safety is not "
                                       "affected", Provenance.MEASURED, "Health journal"))
    if int(h.get("recorder_errors") or 0):
        findings.append(Finding(W, f"Health recorder caught {int(h['recorder_errors'])} error(s): "
                                   f"{h.get('recorder_error', '')}", Provenance.MEASURED, "Health recorder"))
    if not findings:
        findings.append(Finding(H, "no host or recorder problem seen", Provenance.MEASURED, "Health"))
    findings.sort(key=lambda f: -_sev(f.state))
    return SubsystemHealth(key, title, worst(f.state for f in findings), monitored=True,
                           provenance=Provenance.MEASURED, source="Health: the PC and this app (not the microscope)",
                           observed_at=at, expected_period_s=h.get("period_s"), findings=tuple(findings),
                           metrics=tuple(metrics),
                           not_monitored=inventory.not_monitored(key), capabilities=())


# ====================================================================================================== capabilities
_CAP_FROM_DEP = {O: CapabilityState.UNAVAILABLE, F: CapabilityState.AT_RISK, R: CapabilityState.AT_RISK,
                 U: CapabilityState.UNKNOWN, W: CapabilityState.WATCH, H: CapabilityState.PERMITTED}


def capability_status(cap: CapabilityDef, subs: dict[str, SubsystemHealth], view: HealthView) -> CapabilityStatus:
    missing = [view.probe_missing[p] for p in cap.probes if p in view.probe_missing]
    refusals = [r for p in cap.probes for r in view.probe_refusals.get(p, ())]
    deps = [subs[k] for k in cap.depends_on if k in subs]
    monitored = [d for d in deps if d.monitored]
    unmonitored = [d.title for d in deps if not d.monitored]
    note = (f"not monitored in this session: {', '.join(unmonitored)}",) if unmonitored and monitored else ()
    if missing:
        return CapabilityStatus(cap.key, cap.title, CapabilityState.UNAVAILABLE, tuple(missing), "Health",
                                cap.depends_on)
    if refusals:
        return CapabilityStatus(cap.key, cap.title, CapabilityState.RESTRICTED,
                                tuple(f"SafetyManager refuses it: {r}" for r in refusals), "SafetyManager",
                                cap.depends_on)
    if not monitored:
        return CapabilityStatus(cap.key, cap.title, CapabilityState.UNAVAILABLE,
                                (f"no device for it in this session ({', '.join(unmonitored)} not monitored)",),
                                "Health", cap.depends_on, Provenance.UNAVAILABLE)
    dep_state = worst(d.state for d in monitored)
    state = _CAP_FROM_DEP[dep_state]
    if state is CapabilityState.PERMITTED:
        why: tuple[str, ...] = ("the SafetyManager's state checks pass now; " + ", ".join(d.title for d in monitored)
                                + " healthy",) if cap.probes else (", ".join(d.title for d in monitored) + " healthy",)
    else:
        why = tuple(f"{d.title} {d.state.value}: {d.reasons[0] if d.reasons else ''}" for d in monitored
                    if d.state is dep_state)
        if state is CapabilityState.AT_RISK:
            why += ("advisory: the SafetyManager does not refuse it, and Phase 0 Health restricts nothing",)
    return CapabilityStatus(cap.key, cap.title, state, why + note, "Health", cap.depends_on)


def _restrictions(view: HealthView, caps: list[CapabilityStatus], subs: dict[str, SubsystemHealth]
                  ) -> list[Restriction]:
    out: list[Restriction] = []
    restricted = {c.key for c in caps if c.state is CapabilityState.RESTRICTED}
    stage_ids = view.devices.get("xy_stage", ())
    explained: set[str] = set()
    for m in view.monitors:
        st = m.state
        if not st.get("latched"):
            continue
        affected = tuple(c.key for c in CAPABILITIES if "xy_stage" in c.probes and c.key in restricted) \
            if m.owns_stage else ()
        explained |= set(affected)
        last = next((view.last_refusal[d] for d in stage_ids if d in view.last_refusal), None)
        rule = str(st.get("rule", ""))
        kind = "drift or landing lock" if rule in DRIFT_RULES else "drive-fault latch"
        out.append(Restriction(
            source=f"StageMonitor ({m.label})", capabilities=affected,
            summary=f"X/Y moves are refused: {kind} {rule} on {st.get('axis', '')}",
            rule=rule, axis=str(st.get("axis", "")), since_utc=str(st.get("since_utc", "")), refusal=m.refusal,
            recovery=str(st.get("recovery", "")), releasable=st.get("releasable"),
            evidence=str(st.get("evidence", "")), carried_over=m.carried_over,
            last_router_refusal=last[0] if last else "", last_router_refusal_at=last[1] if last else None,
            safety_reasons=view.probe_refusals.get("xy_stage", ())))
    by_sub: dict[str, list[str]] = {}
    for c in CAPABILITIES:
        if c.key in restricted and c.key not in explained:
            for p in c.probes:
                if view.probe_refusals.get(p):
                    by_sub.setdefault(p, []).append(c.key)
    for sub, keys in by_sub.items():
        dids = view.devices.get(sub, ())
        last = next((view.last_refusal[d] for d in dids if d in view.last_refusal), None)
        title = subs[sub].title if sub in subs else sub
        out.append(Restriction(source="SafetyManager", capabilities=tuple(dict.fromkeys(keys)),
                               summary=f"Commands to the {title.lower()} are refused",
                               last_router_refusal=last[0] if last else "",
                               last_router_refusal_at=last[1] if last else None,
                               safety_reasons=view.probe_refusals.get(sub, ())))
    return out


# ====================================================================================================== report
def assess(view: HealthView) -> HealthReport:
    subs: dict[str, SubsystemHealth] = {}
    for d in SUBSYSTEMS:
        if d.key == "communications":
            subs[d.key] = _communications(view)
        elif d.key == "storage":
            subs[d.key] = _storage(view)
        elif d.key == "host":
            subs[d.key] = _host(view)
        else:
            subs[d.key] = _device_subsystem(d.key, d.title, view)
    caps = [capability_status(c, subs, view) for c in CAPABILITIES]
    overall = worst(s.state for s in subs.values() if s.monitored)
    return HealthReport(view.now, overall, tuple(subs[d.key] for d in SUBSYSTEMS), tuple(caps),
                        tuple(_restrictions(view, caps, subs)))


def failed_report(now: datetime, error: str) -> HealthReport:
    """What the page shows when an assessment itself fails: UNKNOWN, never the last green."""
    return HealthReport(now, U, error=error)


def overall_reasons(report: HealthReport) -> list[str]:
    return [r for s in report.subsystems if s.monitored and s.state is report.overall for r in s.reasons]


def iter_metrics(report: HealthReport) -> Iterable[tuple[str, Metric]]:
    for s in report.subsystems:
        for m in s.metrics:
            yield s.key, m
