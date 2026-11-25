"""What each adapter really exposes today (ADR-0068 §1; kit docs/MONITORING_CATALOG.md, roadmap Phase 0).

One row per signal: unit, rate, where it comes from, and its provenance on the real instrument (on the simulator every
value is DEMO). A row with `extract` is read by `assess` and shown on the Health page; a row with provenance UNAVAILABLE
is NOT MONITORED and says why. Nothing is listed that the code does not expose: a signal the backend keeps only inside
its own object (a counter, a file it writes) is NOT MONITORED here until it is published through the RuntimeStateStore
or the EventBus. The inventory is checked against the adapters by tests/test_health_model.py.

Extract keys (read by assess.py):
    pos:<axis>          DeviceSnapshot.observed_position[axis]
    attr:<name>         DeviceSnapshot attribute (reported by the backend with each observation)
    state:<field>       DeviceSnapshot connection / health / activity / firmware_mode / recovery_required
    derived:<name>      computed by assess from other values on the same snapshot
    monitor:<key>       StageMonitor.state() / refusal()
    health:<name>       measured by the Health service itself (disk, memory, threads, its own queue)
    bus:<name>          counted by the Health recorder from EventBus events
"""

from __future__ import annotations

from dataclasses import dataclass

from openevos.health.model import Provenance

M, D, U = Provenance.MEASURED, Provenance.DERIVED, Provenance.UNAVAILABLE

#: the Technosoft slow poll (TelemetryService.poll_interval_s, POLL_INTERVAL_S = 1.0 in the app)
POLL = "~1 Hz (Technosoft status poll)"
ON_CHANGE = "on change"


@dataclass(frozen=True)
class Signal:
    subsystem: str
    name: str
    unit: str
    rate: str
    source: str
    provenance: Provenance
    extract: str = ""
    why: str = ""  # for NOT MONITORED rows: why it is not available

    @property
    def monitored(self) -> bool:
        return self.provenance is not Provenance.UNAVAILABLE


_STAGE_SRC = "TelemetryService -> RuntimeStateStore motor.stage (APOS/CPOS/ESTATEV/IINITONCEDONE/LPOS* per poll)"
_MON_SRC = "StageMonitor.state() (backends/technosoft/stage_monitor.py), sampled 1 Hz by Health"

INVENTORY: tuple[Signal, ...] = (
    # ------------------------------------------------------------------ X/Y stage (Technosoft X, Y)
    Signal("xy_stage", "Position", "µm", POLL, _STAGE_SRC, M, "pos:*"),
    Signal("xy_stage", "Encoder (APOS)", "counts", POLL, _STAGE_SRC, M, "attr:counts"),
    Signal("xy_stage", "Commanded (CPOS)", "counts", POLL, _STAGE_SRC, M, "attr:commanded_counts"),
    Signal("xy_stage", "Following error |APOS - CPOS|", "counts", POLL, "Health, from APOS and CPOS of one poll", D,
           "derived:following_error"),
    Signal("xy_stage", "Homed (IINITONCEDONE = 12345)", "", POLL, _STAGE_SRC, M, "attr:homed"),
    Signal("xy_stage", "Drive state (ESTATEV)", "code", POLL, _STAGE_SRC, M, "attr:drive_state"),
    Signal("xy_stage", "Drive limits (LPOSMIN..LPOSMAX)", "mm", POLL, _STAGE_SRC, M, "attr:limits_mm"),
    Signal("xy_stage", "Activity", "", POLL, _STAGE_SRC, M, "state:activity"),
    Signal("xy_stage", "Drift-monitor lock", "", ON_CHANGE, _MON_SRC, M, "monitor:latched"),
    Signal("xy_stage", "Drift-monitor warnings", "", ON_CHANGE, _MON_SRC, M, "monitor:warnings"),
    Signal("xy_stage", "Fast encoder samples taken", "samples", "~20 Hz (X and Y APOS)", _MON_SRC, M,
           "monitor:fast_samples"),
    Signal("xy_stage", "Automatic re-approaches this session", "count", ON_CHANGE, _MON_SRC, M,
           "monitor:reapproaches"),
    Signal("xy_stage", "Motor current (IQ, IIQMAX, IIQMIN)", "A (raw)", "", "", U,
           why="read only when a lock is set and kept in the evidence JSON; not published"),
    Signal("xy_stage", "Drive flags (ISTATUSFLAGS, SRL)", "bits", "", "", U,
           why="read each poll by the StageMonitor, which latches on them; the raw values are not published"),
    Signal("xy_stage", "Landing miss and settle time", "counts, s", "", "", U,
           why="written to landing-log.csv (ADR-0061); not published"),
    Signal("xy_stage", "Idle drift trend / repeatability", "counts", "", "", U,
           why="needs qualified baselines (Phase 1)"),
    # ------------------------------------------------------------------ Z focus (Technosoft Z)
    Signal("focus", "Position", "µm", POLL, "TelemetryService -> RuntimeStateStore motor.focus", M, "pos:z"),
    Signal("focus", "Encoder (APOS)", "counts", POLL, "RuntimeStateStore motor.focus", M, "attr:counts"),
    Signal("focus", "Homed", "", POLL, "RuntimeStateStore motor.focus", M, "attr:homed"),
    Signal("focus", "Drive state (ESTATEV)", "code", POLL, "RuntimeStateStore motor.focus", M, "attr:drive_state"),
    Signal("focus", "Activity", "", POLL, "RuntimeStateStore motor.focus", M, "state:activity"),
    Signal("focus", "Limit-switch events, landing repeatability", "", "", "", U,
           why="not read by the adapter; motion alone does not prove calibration"),
    # ------------------------------------------------------------------ objective turret (Technosoft O)
    Signal("objective_turret", "Position sensor (ICURRENTPOSHF)", "position", POLL,
           "TelemetryService -> RuntimeStateStore motor.objective_turret", M, "attr:objective_position"),
    Signal("objective_turret", "Position", "position", ON_CHANGE, "RuntimeStateStore (the turret device)", M,
           "pos:*"),
    Signal("objective_turret", "Activity", "", POLL, "RuntimeStateStore", M, "state:activity"),
    Signal("objective_turret", "Switch time", "s", "", "", U,
           why="only as a command's duration (Communications); no per-turn timing is published"),
    # ------------------------------------------------------------------ F cube carriage, P phase wheel
    Signal("filter_carriage", "Position", "counts", POLL,
           "TelemetryService -> RuntimeStateStore motor.cube_carriage / motor.phase_wheel", M, "pos:*"),
    Signal("filter_carriage", "Homed (F)", "", POLL, "RuntimeStateStore motor.cube_carriage", M, "attr:homed"),
    Signal("filter_carriage", "Cube type in the light path", "", "", "", U,
           why="read by the LED board's cube sensor (0x47) before each epi ON; shown under Illumination"),
    # ------------------------------------------------------------------ mono camera (FLIR Chameleon, FlyCapture2)
    Signal("mono_camera", "Connection", "", ON_CHANGE, "RuntimeStateStore (camera backend)", M, "state:connection"),
    Signal("mono_camera", "Activity (idle / acquiring)", "", ON_CHANGE, "RuntimeStateStore (camera backend)", M,
           "state:activity"),
    Signal("mono_camera", "Exposure", "ms", ON_CHANGE, "RuntimeStateStore attribute exposure_ms", M,
           "attr:exposure_ms"),
    Signal("mono_camera", "Gain", "dB", ON_CHANGE, "RuntimeStateStore attribute gain_db", M, "attr:gain_db"),
    Signal("mono_camera", "Model / firmware", "", ON_CHANGE, "RuntimeStateStore attributes model, fw", M,
           "attr:model"),
    Signal("mono_camera", "Frame rate, incomplete frames, latency", "fps, frames, ms", "", "", U,
           why="counted inside the backend (frames_produced, incomplete_frames); not published"),
    Signal("mono_camera", "Reconnects after a USB drop", "count", "", "", U,
           why="counted inside the backend (reconnects, ADR-0066); its reason text is journaled"),
    Signal("mono_camera", "Sensor temperature", "°C", "", "", U, why="not read by the adapter"),
    # ------------------------------------------------------------------ colour camera (IDS uEye)
    Signal("colour_camera", "Connection", "", ON_CHANGE, "RuntimeStateStore (camera backend)", M,
           "state:connection"),
    Signal("colour_camera", "Activity (idle / acquiring)", "", ON_CHANGE, "RuntimeStateStore (camera backend)", M,
           "state:activity"),
    Signal("colour_camera", "Exposure", "ms", ON_CHANGE, "RuntimeStateStore attribute exposure_ms", M,
           "attr:exposure_ms"),
    Signal("colour_camera", "Gain", "dB", ON_CHANGE, "RuntimeStateStore attribute gain_db", M, "attr:gain_db"),
    Signal("colour_camera", "Model", "", ON_CHANGE, "RuntimeStateStore attribute model", M, "attr:model"),
    Signal("colour_camera", "Frame rate, skipped grabs, latency", "fps, frames, ms", "", "", U,
           why="counted inside the backend (frames_produced, skipped_grabs); not published"),
    # ------------------------------------------------------------------ illumination (EVOS LED board, USB HID)
    Signal("illumination", "Connection", "", ON_CHANGE, "RuntimeStateStore (LED board backend)", M,
           "state:connection"),
    Signal("illumination", "Firmware (application / bootloader)", "", ON_CHANGE,
           "RuntimeStateStore (LED board backend)", M, "state:firmware_mode"),
    Signal("illumination", "Channel", "", ON_CHANGE, "RuntimeStateStore attribute channel", M, "attr:channel"),
    Signal("illumination", "Light on", "", ON_CHANGE, "RuntimeStateStore attribute led_enabled (read back)", M,
           "attr:led_enabled"),
    Signal("illumination", "Intensity", "%", ON_CHANGE, "RuntimeStateStore attribute intensity_percent", M,
           "attr:intensity_percent"),
    Signal("illumination", "Cube in the light path (sensor 0x47)", "", ON_CHANGE,
           "RuntimeStateStore attribute cube_in_path", M, "attr:cube_in_path"),
    Signal("illumination", "Optical output", "mW", "", "", U, why="no photometer: no optical stability claim"),
    Signal("illumination", "LED temperature, current, on-time", "", "", "", U, why="not read by the adapter"),
    # ------------------------------------------------------------------ incubator (AMC1000 / onstage)
    Signal("incubator", "Temperature, CO2, O2, alarms", "°C, %", "", "", U,
           why="no control or telemetry path on this instrument (bring-up record)"),
    # ------------------------------------------------------------------ communications
    Signal("communications", "Link connection (per link)", "", "per observation",
           "RuntimeStateStore connection of the devices on each link", M, "health:links"),
    Signal("communications", "Link errors (last 10 min)", "count", ON_CHANGE,
           "EventBus DeviceStateChanged reasons ('telemetry failed', 'dropped off USB', ...)", M,
           "bus:link_errors"),
    Signal("communications", "Command refusals (session)", "count", "per command",
           "EventBus CommandLifecycleEvent REJECTED (the router's decision)", M, "bus:refusals"),
    Signal("communications", "Command failures and time-outs (session)", "count", "per command",
           "EventBus CommandLifecycleEvent FAILED / TIMED_OUT", M, "bus:failures"),
    Signal("communications", "Last command duration", "s", "per command",
           "Health, from CommandLifecycleEvent timestamps", D, "bus:last_duration"),
    Signal("communications", "Serial frame timeouts and retries", "count", "", "", U,
           why="handled inside DriveLink; not published"),
    # ------------------------------------------------------------------ storage & data
    Signal("storage", "Free space (data folder drive)", "GB", "every 10 s", "Health: shutil.disk_usage", M,
           "health:disk_data"),
    Signal("storage", "Free space (journal drive)", "GB", "every 10 s", "Health: shutil.disk_usage", M,
           "health:disk_journal"),
    Signal("storage", "Storage faults (session)", "count", ON_CHANGE, "EventBus StorageFaultEvent", M,
           "bus:storage_faults"),
    Signal("storage", "Frames failed to write (session)", "frames", "per acquisition",
           "EventBus AcquisitionStatusEvent.failed", M, "bus:frames_failed"),
    Signal("storage", "Preview frames dropped (display only)", "frames", "1 Hz",
           "FrameTransport.preview.stats (dropping preview frames is expected; not scientific data)", M,
           "health:preview_dropped"),
    Signal("storage", "Write latency, writer queue depth, checksums", "ms, frames", "", "", U,
           why="not exposed by the writers"),
    # ------------------------------------------------------------------ host & application
    Signal("host", "Memory in use", "%", "1 Hz", "Health: GlobalMemoryStatusEx (Windows)", M, "health:memory"),
    Signal("host", "Threads in the app", "count", "1 Hz", "Health: threading.active_count()", M, "health:threads"),
    Signal("host", "Event subscriber errors", "count", "1 Hz", "EventBus.delivery_errors", M, "health:bus_errors"),
    Signal("host", "Stale observations rejected", "count", "1 Hz", "RuntimeStateStore.stale_observations", M,
           "health:stale_obs"),
    Signal("host", "Health journal", "events", "1 Hz", "Health: its own queue and SQLite writer", M,
           "health:journal"),
    Signal("host", "CPU, GPU, UI latency, worker restarts", "", "", "", U, why="not measured in Phase 0"),
)


def signals(subsystem: str) -> tuple[Signal, ...]:
    return tuple(s for s in INVENTORY if s.subsystem == subsystem)


def not_monitored(subsystem: str) -> tuple[str, ...]:
    """'name (unit): why' for every NOT MONITORED signal of a subsystem."""
    return tuple(f"{s.name}" + (f" ({s.unit})" if s.unit else "") + f": {s.why}" for s in signals(subsystem)
                 if not s.monitored)
