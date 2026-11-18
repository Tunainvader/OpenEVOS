"""Live motor telemetry from the Technosoft drives into the command core (ADR-0023), with optional,
narrowly scoped X/Y stage control (ADR-0024).

One `TelemetryService` owns the serial port and, on its own thread, polls a fixed set of variable reads
per axis (state, homed marker, motion-complete bit, actual/commanded position, limits). It reports
through five native PHYSICAL `DeviceBackend`s:

    motor.stage (X+Y, um)   motor.focus (Z, um)   motor.cube_carriage (F)   motor.objective_turret (O)
    motor.phase_wheel (P)   (F/O/P in drive counts)

Read-only by default: every device rejects every command. Only when the session enables motor control
(`motion=True`) does `motor.stage` accept `stage.move_absolute`, executed exactly as verified by Motor
Gates 4-6 (target write, move request, completion by SRH bit 9, encoder position within the drive's
band, then the verified controlled stop to return the drive to state 0). Speed and acceleration are
never written (the drive's power-up values are used). `halt()` (the router's STOP) and `cancel()` run
the controlled stop on X and Y, ahead of anything else. Focus and the turrets stay read-only.

The X/Y drift monitor (`stage_monitor`, ADR-0040) watches X and Y encoders at ~20 Hz and the drive's own flags; once
it latches, every X/Y move is refused until X and Y are seen to re-home. Until the source of Y's drift is found, X
and Y never move together: a target that changes both runs as two legs, X then Y (Y-axis plan v3 ground rule).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from openevos.backends.technosoft import power_up_record, watchdog
from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm
from openevos.backends.technosoft.stage_monitor import (
    EXTRA_READS,
    FAST_PERIOD_S,
    RECOVERY,
    SETTLE_S,
    StageMonitor,
)
from openevos.backends.technosoft.transport import (
    Port,
    SimulatedDriveChain,
    TransportError,
    WinSerialPort,
)
from openevos.core.models import (
    Activity,
    BackendKind,
    Connection,
    DeviceKind,
    FirmwareMode,
    Health,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    Position,
    SetTurretPosition,
)
from openevos.safety.capabilities import (
    AxisCapability,
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
)

POLL_INTERVAL_S = 1.0
REOPEN_INTERVAL_S = 5.0
MOVE_POLL_S = 0.05
SETTLE_BAND_COUNTS = 5  # the drives' LCONVERGEBAND (Gate 4)
POWER_UP_SETTLE_S = 5.0  # the power-up record waits this long after X and Y report homed (ADR-0040 §9)
#: max |APOS - CPOS| before a move and for an idle axis after another moves (ADR-0030: 1 count = 6.25 deg
#: electrical on these closed-loop steppers; ~14 counts reverses torque)
DRIFT_MAX_COUNTS = 3
#: moving away from the target by more than this (~44 um) during a move = not under control (stop and lock)
WRONG_WAY_COUNTS = 100
SPEED_MM_S = 8.27  # power-up CSPDCMD 15 at 0.8 ms sampling (Gate 5)
#: PC-side travel box for X/Y control: the drives' power-up limits (0..50,000 counts = 22.05 mm), less a
#: margin. Widening it needs the calibrated limits (Gate 7) and a new review.
CONTROL_BOX_UM = (0.0, 22000.0)
READBACK_TOLERANCE_UM = 3.0  # > 5 counts (2.2 um)
EVIDENCE = ("ADR-0016", "ADR-0017", "ADR-0018", "ADR-0019", "ADR-0020", "ADR-0021",
            "motor-gate4-20261112T193713-pass", "motor-gate5-20261112T194543-pass",
            "motor-gate6-20261112T195237-pass")
EVIDENCE_FULL_RANGE = ("ADR-0022", "ADR-0028", "motor-gate7-20261113T103701-pass", "motor-gate8-20261113T104204-pass")
CALIBRATION_MARGIN_UM = 2000.0  # Gate 8 verified 0 .. (calibrated max - 2 mm)
EXPECTED_SPAN_MM = {"X": 119.1, "Y": 78.1}
CLOSED_LOOP = ("X", "Y", "Z", "F")
#: Y-axis plan v3 ground rule until the source of Y's drift is found: never move X and Y together. A target that
#: changes both runs as two legs, X then Y; the drift monitor watches the still axis during each leg (ADR-0040).
SEQUENTIAL_XY = True


def calibrated_travel_box(parsed_calibration) -> tuple[dict[str, tuple[float, float]] | None, str]:
    """PC travel box from the git-ignored parsed calibration (Step D + pearl_persistent): per axis
    0 .. (calibrated max - 2 mm), exactly the range Motor Gate 8 verified. Validated like Gate 7
    (min within (-2 mm, 0], span within 0.5 mm of the stored spans); any problem -> (None, reason)."""
    import json
    from pathlib import Path

    try:
        mb = json.loads(Path(parsed_calibration).read_text(encoding="utf-8"))["motor_base"]
        lim = {ax: (int(mb[f"{ax}AxisPositionLimits.lMin"]), int(mb[f"{ax}AxisPositionLimits.lMax"]))
               for ax in ("X", "Y")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"calibration unreadable: {exc}"
    box = {}
    for ax, (lo, hi) in lim.items():
        cpm = tp.COUNTS_PER_MM[ax]
        if not -2.0 * cpm < lo <= 0 or abs((hi - lo) / cpm - EXPECTED_SPAN_MM[ax]) > 0.5:
            return None, f"{ax} calibration limits fail validation"
        box[ax.lower()] = (0.0, round(um_from_counts(ax, hi) - CALIBRATION_MARGIN_UM, 1))
    return box, ""
def calibrated_limits_counts(parsed_calibration) -> tuple[dict[str, tuple[int, int]] | None, str]:
    """The X/Y drive limits Gate 7 loads (counts), validated exactly as Gate 7's load_limits does: lMin within
    (-2 mm, 0], span within 0.5 mm of the stored span, lMax wider than the power-up 50,000. (None, why) otherwise."""
    from pathlib import Path

    try:
        mb = json.loads(Path(parsed_calibration).read_text(encoding="utf-8"))["motor_base"]
        lim = {ax: (int(mb[f"{ax}AxisPositionLimits.lMin"]), int(mb[f"{ax}AxisPositionLimits.lMax"]))
               for ax in ("X", "Y")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"calibration unreadable: {exc}"
    for ax, (lo, hi) in lim.items():
        cpm = tp.COUNTS_PER_MM[ax]
        if not -2.0 * cpm < lo <= 0 or abs((hi - lo) / cpm - EXPECTED_SPAN_MM[ax]) > 0.5 or hi <= tp.POWER_UP_LIMITS[1]:
            return None, f"{ax} calibration limits fail validation"
    return lim, ""


#: the slow status poll; X/Y also give the drift monitor their power stage (SRL) and program flags (ADR-0040)
READ_ORDER = {ax: [n for n in ("ESTATEV", "IINITONCEDONE", "SRH", "SRL", "ISTATUSFLAGS", "BDOAXISOFF", "APOS", "CPOS",
                               "LPOSMIN", "LPOSMAX", "ICURRENTPOSHF") if n in tp.VARS[ax]] for ax in tp.AXIS_IDS}


@dataclass(frozen=True)
class DeviceSpec:
    device_id: str
    kind: DeviceKind
    axes: tuple[str, ...]
    frame: str


DEVICES = (
    DeviceSpec("motor.stage", DeviceKind.XY_STAGE, ("X", "Y"), "stage"),
    DeviceSpec("motor.focus", DeviceKind.FOCUS, ("Z",), "focus"),
    DeviceSpec("motor.cube_carriage", DeviceKind.TURRET, ("F",), "drive"),
    DeviceSpec("motor.objective_turret", DeviceKind.TURRET, ("O",), "drive"),
    DeviceSpec("motor.phase_wheel", DeviceKind.TURRET, ("P",), "drive"),
)


def counts_from_um(axis: str, um: float) -> int:
    return round(um / 1000.0 * tp.COUNTS_PER_MM[axis])


def um_from_counts(axis: str, counts: int) -> float:
    return counts / tp.COUNTS_PER_MM[axis] * 1000.0


class _LinkError(RuntimeError):
    pass


class DriveLink:
    """Sync + one allowlisted request + an exact-length reply, as verified by the Motor Gates."""

    def __init__(self, port: Port):
        self.port = port
        self._synced = False

    def sync(self) -> bool:
        self.port.purge_rx()
        for attempt in range(15):
            self.port.write(bytes([tp.SYNC_BYTE]))
            if self.port.read(1) == bytes([tp.SYNC_REPLY]):
                if attempt:
                    # a late 0D from an earlier FF must not prefix the next reply (Gate 8, 2026-11-13):
                    # one timeout-bounded read lets it arrive (no sleep in the core, ADR-0008), then purge
                    self.port.read(1)
                    self.port.purge_rx()
                return True
        return False

    def open(self) -> int:
        for baud in (115200, 9600):
            self.port.configure(baud)
            if self.sync():
                self.port.write(tp.ID_QUERY)
                # 'O' + length byte first, then the rest by length (the real reply is 13 bytes; Gate 1)
                head = self.port.read(2)
                raw = head + (self.port.read(head[1] + 1) if len(head) == 2 else b"")
                gid = tp.parse_id_reply(raw)
                if gid != 1:
                    raise _LinkError(f"gateway axis ID {gid}, not 1")
                return baud
        raise _LinkError("no line sync at 115200 or 9600")

    def read(self, var: tp.Var) -> int:
        """One read. The line is synced once and then only after a reply fails its checks (checksum, axis,
        address, length), with one retry: halves the time per read (32 -> 16 ms measured on COM3, 2026-11-13,
        0 failures in 100) so a move starts ~0.2 s sooner. Writes still sync every time."""
        for attempt in (0, 1):
            if not self._synced or attempt:
                if not self.sync():
                    raise _LinkError("line sync lost")
                self._synced = True
            self.port.write(var.frame)
            n = tp.reply_length(var.bits)
            raw = self.port.read(n)
            for _ in range(4):  # a late sync reply (0D) can precede the real reply; a real reply starts with 'O'
                if not raw or raw[0] != tp.SYNC_REPLY:
                    break
                raw = raw[1:] + self.port.read(1)
            try:
                return tp.parse_read_reply(raw, var.axis_id, var.address, var.bits)
            except ValueError as exc:
                self._synced = False
                if attempt:
                    raise _LinkError(f"{var.axis} {var.name}: {exc}") from exc
        raise _LinkError("unreachable")

    def write(self, frame: bytes) -> None:
        """One reviewed motion write; the drive must acknowledge it with a single 'O'."""
        if not self.sync():
            raise _LinkError("line sync lost before a write")
        self.port.write(frame)
        ack = b""
        for _ in range(10):
            ack = self.port.read(1)
            if ack:
                break
        if ack != bytes([tp.ACK]):
            raise _LinkError(f"write {frame.hex(' ')} not acknowledged ({ack.hex() or 'nothing'})")


@dataclass
class _MoveJob:
    command_id: str
    ctx: object
    targets: dict[str, int]  # axis -> counts
    target_um: tuple[float, float]


@dataclass
class _Leg:
    """One leg of a stage move while it runs (audit APP-16): what the steps of TelemetryService._move_leg share.
    `outcome` is what the monitor and the motion profile record; each step sets it before it acts."""
    ctx: object
    axes: list[str]
    targets: dict[str, int]
    start: dict[str, int]
    speed: dict[str, float]  # mm/s per axis, for the timeout
    watches: dict  # axis -> watchdog.LegWatch (shadow mode, ADR-0047)
    outcome: str = "link error"
    live: dict[str, int] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)
    rest: dict[str, dict] = field(default_factory=dict)
    extra: Iterator[str] = field(default_factory=lambda: itertools.cycle(("POSERR", "IQ")))  # watchdog reads
    deadline: float = 0.0
    last_report: float = 0.0


@dataclass
class _CarriageJob:
    """F to a cube slot or the mirror (ADR-0051), asked for by the LED device before it lights a cube."""
    dest: str  # 'slot1'..'slot4' or 'mirror'
    done: threading.Event
    ok: bool = False
    message: str = ""


#: F carriage: 0.1 s polls, generous timeout floor (CSPDCMD 20.0 is ~25,000 counts/s; Gate 11), and the distance it may
#: run away from its target before it is stopped (Gates 11/12 used the same 200 counts)
CARRIAGE_POLL_S = 0.1
CARRIAGE_SPEED_COUNTS_S_MIN = 5000
CARRIAGE_WRONG_WAY_COUNTS = 200
CARRIAGE_WAIT_S = 40.0
CARRIAGE_TIMEOUT_MARGIN_S = 5.0


@dataclass
class _ReleaseJob:
    """Operator release of a small drift latch (ADR-0057): fresh X/Y reads on the service thread, then the monitor
    decides. Reads only; nothing is written to any drive."""
    done: threading.Event
    who: str = "operator"
    message: str = ""
    ok: bool = False


@dataclass
class _RehomeJob:
    """Software re-home of X and Y during a stage lock (ADR-0058): remember X/Y, send each axis the drives' own home
    request (ESTATE 19) in turn, watch it finish, let the monitor clear the lock, then return X then Y. `stage` is a
    one-line progress text for the UI; `done` is set at the end, whatever happened."""
    done: threading.Event
    who: str = "operator"
    stage: str = "queued"
    ok: bool = False
    message: str = ""
    homed: bool = False  # both axes homed and the lock cleared (the return may still have failed)
    remembered: dict[str, int] = field(default_factory=dict)  # counts
    returned_um: tuple[float, float] | None = None
    record: dict = field(default_factory=dict)


def _set_extra_writes(link: DriveLink, frames: frozenset) -> None:
    """Register exact extra frames on the port for one operation (ADR-0056/0058); the transport's last check uses it."""
    port: Any = link.port
    port.extra_writes = frames


class _ReturnCtx:
    """The minimal command context `_run_move` needs, for the re-home's own return move (no router command)."""

    def __init__(self) -> None:
        self.cancel_requested = False
        self.started = False
        self.ok: bool | None = None
        self.message = ""
        self.position: Position | None = None

    def mark_started(self) -> None:
        self.started = True

    def complete(self, position=None) -> None:
        if self.ok is None:
            self.ok, self.position, self.message = True, position, "returned"

    def fail(self, message: str, error_code: str = "") -> None:
        if self.ok is None:
            self.ok, self.message = False, message

    def mark_cancelled(self, position=None) -> None:
        if self.ok is None:
            self.ok, self.position, self.message = False, position, "return cancelled by a stop"


#: the re-home's limits (Gate 20 on the unit: started at the first poll, done in 10.6-17.7 s)
REHOME_START_S = 5.0
REHOME_AXIS_TIMEOUT_S = 120.0
REHOME_POLL_S = 0.2
REHOME_Z_BAND = 5  # Z must be at home (counts), as Gate 20
REHOME_PRE_READS = ("ESTATEV", "IINITONCEDONE", "SRH", "SRL", "MER", "ISTATUSFLAGS", "APOS", "CPOS", "HOMEDONE")


@dataclass
class _FocusJob:
    """Z to an absolute target in counts (ADR-0053)."""
    command_id: str
    ctx: object
    counts: int


@dataclass
class _TurretJob:
    """Objective turret to position 1..5 (ADR-0053): Z retracted to 0 first, stage required in TURRET_STAGE_BOX_MM."""
    command_id: str
    ctx: object
    position: int


ZO_DEVICES = ("motor.focus", "motor.objective_turret")
#: Z: generous timeout floor (power-up CSPDCMD 10.0 is ~12,500 counts/s; Gate 18: 5 mm in < 4 s) and wrong-way guard
Z_SPEED_COUNTS_S_MIN = 5000
Z_WRONG_WAY_COUNTS = 200
Z_TIMEOUT_MARGIN_S = 3.0
Z_READBACK_TOLERANCE_UM = 1.0  # LCONVERGEBAND 5 counts = 0.61 um
#: the turret turns only with the stage over its opening (Gate 19: an objective struck the stage frame at X/Y 0, 0; the
#: full cycle was clean at X 59.5, Y 39.0 mm) and Z retracted at 0
TURRET_STAGE_BOX_MM = {"X": (40.0, 80.0), "Y": (20.0, 60.0)}
TURRET_STUCK_FLAG = 0x0002
#: one Geneva step took 5.3-6.1 s on the unit (Gate 19 rev 2); timeout = steps x this + margin
TURRET_STEP_S_MAX = 10.0
TURRET_OBJECTIVES = {1: "4X", 2: "10X", 3: "20X", 4: "40X", 5: "60X"}


class TelemetryBackend:
    """A PHYSICAL device fed by the TelemetryService. Only motor.stage, with motion enabled, takes commands. On the
    drive emulator (`--motor-telemetry sim`) it is SIMULATED, with no hardware evidence (audit APP-02)."""

    @property
    def backend_kind(self) -> BackendKind:
        return BackendKind.SIMULATED if self._service.emulated else BackendKind.PHYSICAL

    def capabilities(self) -> DeviceCapabilities:
        caps = self._capabilities()
        if not self._service.emulated:
            return caps
        return caps.model_copy(update={"backend_kind": BackendKind.SIMULATED, "source": CapabilitySource.SIMULATED,
                                       "evidence_ids": (), "note": f"drive emulator (no hardware): {caps.note or ''}"})

    def __init__(self, spec: DeviceSpec, port_label: str, service: TelemetryService):
        self.spec = spec
        self.device_id, self.kind = spec.device_id, spec.kind
        self.physical_id = f"technosoft:{port_label}:{'+'.join(spec.axes)}"
        self._service = service
        self._reporter = None
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._position: Position | None = None

    @property
    def controllable(self) -> bool:
        s = self._service
        return s.motion and (self.spec.device_id == "motor.stage" or (self.spec.device_id in ZO_DEVICES and s.zo_control))  # noqa: E501

    def _capabilities(self) -> DeviceCapabilities:
        if not self.controllable:
            return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.PHYSICAL,
                                      source=CapabilitySource.CANDIDATE, supported_commands=frozenset(),
                                      note="read-only Technosoft telemetry (ADR-0023); no commands")
        if self.spec.device_id == "motor.focus":
            lo, hi = (round(um_from_counts("Z", c), 1) for c in tp.Z_TARGET_BOUNDS)
            ax = AxisCapability(name="z", limits=AxisLimits(minimum=lo, maximum=hi, unit="um"), coordinate_frame="focus",  # noqa: E501
                                calibration_valid=True, readback_tolerance=Z_READBACK_TOLERANCE_UM)
            return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.PHYSICAL,
                                      source=CapabilitySource.VERIFIED,
                                      supported_commands=frozenset({"focus.move_absolute"}), axes=(ax,),
                                      evidence_ids=("ADR-0052", "ADR-0053", "motor-gate18-20261117T101842-pass"),
                                      note="Z at power-up speed, 0 (fully retracted) .. 8.05 mm; + = towards the stage")
        if self.spec.device_id == "motor.objective_turret":
            return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.PHYSICAL,
                                      source=CapabilitySource.VERIFIED,
                                      supported_commands=frozenset({"turret.set_position"}),
                                      turret_positions=tp.O_POSITIONS,
                                      evidence_ids=("ADR-0052", "ADR-0053", "motor-gate19-20261117T114734-pass"),
                                      note="drive's own GOTO_POS; Z is retracted to 0 first, and the stage must be "
                                           "centred (X 40-80, Y 20-60 mm)")
        box = self._service.travel_box_um
        axes = tuple(AxisCapability(name=a, limits=AxisLimits(minimum=box[a][0], maximum=box[a][1], unit="um"),
                                    coordinate_frame="stage", calibration_valid=True,
                                    readback_tolerance=READBACK_TOLERANCE_UM) for a in ("x", "y"))
        kind = "calibrated envelope (ADR-0029)" if self._service.calibrated_box else "power-up box (ADR-0024)"
        return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.PHYSICAL,
                                  source=CapabilitySource.VERIFIED, supported_commands=frozenset({"stage.move_absolute"}),  # noqa: E501
                                  axes=axes, evidence_ids=EVIDENCE + (EVIDENCE_FULL_RANGE if self._service.calibrated_box  # noqa: E501
                                                                     else ()),
                                  note=f"X/Y moves at power-up speed inside the {kind}")

    def attach(self, reporter) -> None:
        self._reporter = reporter

    def connect(self) -> None:
        self._service.start()

    def disconnect(self) -> None:
        self._service.stop()

    def start(self, command, ctx) -> None:
        if self.controllable and self.spec.device_id == "motor.focus" and isinstance(command, MoveFocusAbsolute):
            self._service.submit(_FocusJob(command.command_id, ctx, counts_from_um("Z", command.target().get("z"))))
            return
        if self.controllable and self.spec.device_id == "motor.objective_turret" \
                and isinstance(command, SetTurretPosition):
            self._service.submit(_TurretJob(command.command_id, ctx, command.position))
            return
        if not self.controllable or not isinstance(command, MoveStageAbsolute) or self.spec.device_id != "motor.stage":
            ctx.fail(f"{self.device_id} accepts no {getattr(command, 'kind', 'such command')} in this session "
                     "(read-only telemetry)",
                     error_code="read_only")
            return
        target = command.target().as_dict()
        targets = {"X": counts_from_um("X", target["x"]), "Y": counts_from_um("Y", target["y"])}
        self._service.submit(_MoveJob(command.command_id, ctx, targets, (target["x"], target["y"])))

    def cancel(self, command_id: str) -> None:
        if self.controllable:
            self._service.request_stop(f"cancel {command_id}")

    def halt(self) -> None:
        """Router STOP: the drive's controlled stop on X and Y (motion sessions); returns at once."""
        if self.controllable:
            self._service.request_stop("STOP")

    def read_position(self) -> Position | None:
        with self._lock:
            return self._position

    def verify_result(self, command) -> tuple[bool, str]:
        """Turret: the drive's position sensor must read the commanded position (ADR-0053)."""
        if isinstance(command, SetTurretPosition):
            pos = self.read_position()
            got = int(pos.values[0]) if pos is not None else None
            return got == command.position, f"turret sensor reads {got}, commanded {command.position}"
        return True, ""

    def recover(self) -> None:
        """Operator recovery after the service's last-resort handler (audit APP-04): clears the flag; the next poll
        reports the drive's real state (the router clears recovery_required). Drift-monitor latches are separate
        (power cycle or ADR-0057 release)."""
        self.report(health=Health.NORMAL, reason="recovered by the operator")

    def report(self, **fields) -> None:
        if "position" in fields:
            with self._lock:
                self._position = fields["position"]
        if self._reporter is not None:
            self._reporter(seq=next(self._seq), **fields)


class TelemetryService:
    def __init__(self, open_port: Callable[[], Port], port_label: str, *, poll_interval_s: float = POLL_INTERVAL_S,
                 motion: bool = False, sleep: Callable[[float], None] = time.sleep,
                 travel_box_um: dict[str, tuple[float, float]] | None = None,
                 monitor: StageMonitor | None = None, fast_log: bool = False):
        self._open_port, self.port_label = open_port, port_label
        self.poll_interval_s = poll_interval_s
        self.motion = motion
        #: X/Y drift monitor (ADR-0040); `fast_log` adds X/Y encoder samples at ~20 Hz between the slow polls
        self.monitor = monitor or StageMonitor()
        self.fast_log = fast_log
        self.sequential_xy = SEQUENTIAL_XY
        self._last_fast = 0.0
        #: power-up record (ADR-0040 §9): why the next one is due ("app start", "re-home observed"), and since when
        #: X and Y have been homed (the record waits POWER_UP_SETTLE_S after homing)
        self._power_up_pending: str | None = None
        self._homed_since: float | None = None
        #: PC-side travel box per axis (um): the calibrated envelope when given, else the power-up box
        self.calibrated_box = travel_box_um is not None
        self.travel_box_um = travel_box_um or {"x": CONTROL_BOX_UM, "y": CONTROL_BOX_UM}
        self._sleep = sleep
        self.backends = [TelemetryBackend(s, port_label, self) for s in DEVICES]
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._stop_request = threading.Event()
        self._drift_locked: dict[str, str] = {}  # operator locks (--lock-axis); drive faults latch the monitor
        self.speed_control = False  # ADR-0032: the operator may choose slow/medium/fast (else drives untouched)
        self.speed_preset = "fast"
        self._speed_changed = False  # axis -> why; cleared only by restarting after a power cycle
        self._jobs: queue.Queue[_MoveJob] = queue.Queue()
        self._started = False
        self._last: dict[str, dict[str, int]] | None = None
        self.cycles = 0
        self.last_error = ""
        self.stops_sent = 0
        self.watch_rows: list[dict] = []  # the last motion-profile rows (ADR-0047), newest last
        #: F cube carriage moves for the LED device (ADR-0051); only with motor control
        self.carriage_control = False
        #: Z focus moves and objective-turret changes from the app (ADR-0053); only with motor control
        self.zo_control = False
        #: ADR-0056: the validated calibrated X/Y limits (counts). With motor control the service loads them (Gate 7's
        #: frames) once X and Y finish homing on their power-up limits, e.g. after a power cycle while the app runs
        self.calibrated_limits: dict[str, tuple[int, int]] | None = None
        #: the drive emulator (`port="sim"`): devices report SIMULATED, never physical or verified (audit APP-02)
        self.emulated = False
        self._limits_attempted = False  # once per homing; reset when X or Y re-initialises
        self.limits_reloads: list[dict] = []
        self._rehome: _RehomeJob | None = None  # the latest software re-home (ADR-0058)
        self.rehome_start_s = REHOME_START_S
        self.rehome_timeout_s = REHOME_AXIS_TIMEOUT_S

    @property
    def speed_mm_s(self) -> float:
        """The X/Y speed moves will use: the selected preset with speed control (ADR-0032), else the power-up speed."""
        return tp.SPEED_PRESETS_MM_S[self.speed_preset] if self.speed_control else SPEED_MM_S

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._report_all(connection=Connection.CONNECTING, health=Health.UNKNOWN, activity=Activity.UNKNOWN,
                         firmware_mode=FirmwareMode.UNKNOWN, reason="telemetry starting")
        self._thread = threading.Thread(target=self._run, name="technosoft-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.motion:
            self._stop_request.set()  # a move still running gets the controlled stop before the thread exits
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    # ------------------------------------------------------------------ control requests (any thread)
    def submit(self, job: _MoveJob) -> None:
        self._jobs.put(job)
        self._wake.set()

    def request_stop(self, reason: str) -> None:
        self._stop_request.set()
        self._wake.set()

    def release_latch(self, who: str = "operator", timeout_s: float = 10.0) -> tuple[bool, str]:
        """Blocking, from any thread except the service's: release a small drift latch after fresh reads (ADR-0057).
        Returns (released, message). Reads X/Y only; writes nothing."""
        if threading.current_thread() is self._thread:
            return False, "release_latch called from the telemetry thread"
        if not self.monitor.latched:
            return False, "the stage is not locked"
        why = self.monitor._release_blocker()
        if why:
            return False, why
        job = _ReleaseJob(threading.Event(), who)
        self.submit(job)  # type: ignore[arg-type]
        if not job.done.wait(timeout_s):
            return False, f"no fresh X/Y reads within {timeout_s:g} s; the lock stays"
        return job.ok, job.message

    def rehome_blocker(self) -> str:
        """'' when the operator may start a software re-home now (ADR-0058); otherwise why not."""
        if not self.motion:
            return "motor control is not enabled in this session"
        if self._rehome is not None and not self._rehome.done.is_set():
            return "a re-home is already running"
        return self.monitor.rehome_blocker()

    def rehome_stage(self, who: str = "operator") -> _RehomeJob | str:
        """Start a software re-home of X and Y (ADR-0058), from any thread except the service's. Returns the running
        job (poll `stage`, wait on `done`), or why it cannot start. The service re-checks everything on its thread."""
        if threading.current_thread() is self._thread:
            return "rehome_stage called from the telemetry thread"
        why = self.rehome_blocker()
        if why:
            return why
        job = _RehomeJob(threading.Event(), who)
        self._rehome = job
        self.submit(job)  # type: ignore[arg-type]
        return job

    def move_carriage(self, dest: str, timeout_s: float = CARRIAGE_WAIT_S) -> tuple[bool, str]:
        """Blocking, from any thread except the service's: F to 'slot1'..'slot4' or 'mirror' through the drive's own
        GOTO_CUBE / GOTO_MIRR (ADR-0051). Returns (ok, message). Needs motor control and carriage control."""
        if not (self.motion and self.carriage_control):
            return False, "F carriage control is not enabled in this session"
        if threading.current_thread() is self._thread:
            return False, "move_carriage called from the telemetry thread"
        try:
            tp.f_move_frames(dest)
        except tp.ProtocolRefused as exc:
            return False, str(exc)
        job = _CarriageJob(dest, threading.Event())
        self.submit(job)  # type: ignore[arg-type]
        if not job.done.wait(timeout_s):
            self.request_stop("carriage timeout")
            return False, f"F did not report back within {timeout_s:g} s; STOP sent"
        return job.ok, job.message

    # ------------------------------------------------------------------ service thread
    def poll_once(self, link: DriveLink, interruptible: bool = False) -> dict[str, dict[str, int]] | None:
        """All axes' status. With `interruptible`, returns None as soon as a move or stop is waiting, so an
        operator command never waits for a full poll (~35 reads) to finish."""
        out = {}
        for ax in tp.AXIS_IDS:
            vals = {}
            for name in READ_ORDER[ax]:
                if interruptible and (not self._jobs.empty() or self._stop_request.is_set()):
                    return None
                if self.fast_log and time.monotonic() - self._last_fast >= FAST_PERIOD_S:
                    self._fast_sample(link)  # the X/Y encoder log keeps its ~20 Hz through the slow poll
                vals[name] = link.read(tp.VARS[ax][name])
            out[ax] = vals
        return out

    def _fast_sample(self, link: DriveLink) -> tuple[int, int]:
        """X and Y encoders, back to back, into the drift monitor. The ~20 Hz period runs from each sample's start:
        stamped after the two reads, it would add their ~30 ms on the real line to every period (seen: ~10 Hz)."""
        self._last_fast = time.monotonic()
        x, y = link.read(tp.VARS["X"]["APOS"]), link.read(tp.VARS["Y"]["APOS"])
        if self.monitor.fast(x, y) is not None:
            self._on_latch(link)
        return x, y

    def _idle(self, link: DriveLink, seconds: float) -> None:
        """Between slow polls: fast X/Y samples (when enabled) until the next poll, a job or a stop."""
        if self.monitor.evidence_due():
            self.monitor.write_evidence()
        if not self.fast_log:
            self._wait(seconds)
            return
        end = time.monotonic() + seconds
        while not (self._stop.is_set() or self._stop_request.is_set() or not self._jobs.empty()):
            now = time.monotonic()
            if now - self._last_fast >= FAST_PERIOD_S:  # a fixed ~20 Hz, however short the slow poll interval
                self._fast_sample(link)
                if self.monitor.evidence_due():
                    self.monitor.write_evidence()
                now = time.monotonic()
            if now >= end:
                return
            self._wake.wait(min(end - now, max(0.0, FAST_PERIOD_S - (now - self._last_fast))))
            self._wake.clear()  # the loop condition re-checks the queue, so no request is lost

    def _on_latch(self, link: DriveLink) -> None:
        """The drift monitor just latched: read the drive's own record before anything else is sent (the next move
        start would reset the current peaks and clear the trip flags), and show the lock on the stage."""
        reads: dict = {}
        for ax in tp.MOTION_AXES:
            try:
                r = {n: link.read(tp.VARS[ax][n]) for n in EXTRA_READS}
                for n in ("IQ", "IIQMAX", "IIQMIN"):
                    r[n] = tp.signed16(r[n])
                reads[ax] = r
            except (_LinkError, TransportError, OSError) as exc:
                reads[ax] = {"error": str(exc)}
        self.monitor.add_extra(reads)
        stage = self.backends[0]
        if self._last is not None:  # show the lock now, not at the next status poll (position/activity unchanged)
            obs = self._with_monitor(observation(stage.spec, self._last, controllable=stage.controllable))
            stage.report(health=obs["health"], reason=obs["reason"], attributes=obs["attributes"])
        else:
            stage.report(health=Health.WARNING, reason=self.monitor.refusal())

    def _run(self) -> None:
        port = None
        while not self._stop.is_set():
            if port is None:
                try:
                    port = self._open_port()
                    baud = DriveLink(port).open()
                    self.last_error = ""
                    self._power_up_pending, self._homed_since = "app start", None
                    self._report_all(connection=Connection.CONNECTED, reason=f"telemetry on {self.port_label} @ {baud}")
                except Exception as exc:  # noqa: BLE001 - any open failure: report, back off, retry (audit APP-04)
                    self.last_error = str(exc)
                    if port is not None:
                        with contextlib.suppress(Exception):
                            port.close()
                        port = None
                    self._report_all(connection=Connection.DISCONNECTED, health=Health.UNKNOWN,
                                     activity=Activity.UNKNOWN, reason=f"telemetry unavailable: {exc}")
                    self._fail_pending("motor port unavailable")
                    self._wait(REOPEN_INTERVAL_S)
                    continue
            link = DriveLink(port)
            try:
                if self._stop_request.is_set():
                    self._send_stop(link)
                self._run_jobs(link)
                values = self.poll_once(link, interruptible=self.motion)
                if values is None:  # a move or stop arrived mid-poll: serve it first
                    continue
                if self.monitor.status(values) is not None:
                    self._on_latch(link)
                self._publish(values)
                self._power_up_check(link, values)
                self._maybe_reload_limits(link, values)
                self._idle(link, self.poll_interval_s)
            except (_LinkError, TransportError, OSError, tp.ProtocolRefused) as exc:
                self.last_error = str(exc)
                self.monitor.link_lost()
                self._report_all(health=Health.WARNING, activity=Activity.UNKNOWN, reason=f"telemetry failed: {exc}")
                self._fail_pending(f"motor link error: {exc}")
                port.close()
                port = None
                self._wait(self.poll_interval_s)
                continue
            except Exception as exc:  # noqa: BLE001 - last resort (audit APP-04): never die silently
                # an unexpected error (a bug, not the link) used to end the only thread that owns COM3: snapshots froze
                # at "connected, idle" and new moves hung. Stop, say so, fail what is queued, reconnect after a pause.
                self.last_error = f"internal error: {exc!r}"
                if self.motion:
                    with contextlib.suppress(Exception):
                        self._send_stop(link)
                with contextlib.suppress(Exception):
                    self.monitor.note(f"telemetry internal error: {exc!r}")
                    self.monitor.link_lost()
                self._report_all(health=Health.FAULT, activity=Activity.UNKNOWN, recovery_required=True,
                                 reason=f"motor service internal error ({exc!r}); controlled stop sent; recover the "
                                        "device in Service › Hardware, and report this")
                self._fail_pending(f"motor service internal error: {exc!r}")
                with contextlib.suppress(Exception):
                    port.close()
                port = None
                self._wait(REOPEN_INTERVAL_S)
                continue
        self.monitor.write_evidence(force=True)  # a latch less than 10 s old: keep what there is
        self.monitor.close()
        if port is not None:
            if self._stop_request.is_set():  # app closing: leave the stage stopped
                try:
                    self._send_stop(DriveLink(port))
                except (_LinkError, TransportError, OSError):
                    pass
            if self._speed_changed:  # leave the drives at their power-up speed (the motor gates require it)
                try:
                    link = DriveLink(port)
                    for ax in tp.MOTION_AXES:
                        link.write(tp.speed_write(ax, "fast"))
                    self._speed_changed = False
                except (_LinkError, TransportError, OSError):
                    pass
            port.close()
        self._fail_pending("telemetry stopped")
        self._report_all(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN, reason="telemetry stopped")

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _publish(self, values: dict[str, dict[str, int]]) -> None:
        self._last = values
        self.cycles += 1
        for b in self.backends:
            obs = observation(b.spec, values, controllable=b.controllable)
            if b.spec.device_id == "motor.stage":
                self._with_monitor(obs)
            b.report(**obs)

    def _maybe_reload_limits(self, link: DriveLink, values: dict) -> None:
        """ADR-0056: after X and Y finish homing on their power-up limits (a power cycle, with the app running or
        before it started), load the calibrated limits with Gate 7's four frames, each read back. Only with motor
        control, a validated calibration, no queued job, and X and Y homed, idle, at home (|APOS|, |CPOS| <= 5) and on
        exactly the power-up limits. Once per homing; nothing is retried. No motion."""
        if not self.motion or not self.calibrated_limits:
            return
        xy = {ax: values.get(ax) or {} for ax in ("X", "Y")}
        if any(v.get("IINITONCEDONE") != tp.INIT_DONE_MARKER for v in xy.values()):
            self._limits_attempted = False  # (re-)homing: try once more when it is done
            return
        if self._limits_attempted or not self._jobs.empty():
            return
        if any("LPOSMIN" not in v or "LPOSMAX" not in v for v in xy.values()):
            return
        if all((v["LPOSMIN"], v["LPOSMAX"]) == tuple(self.calibrated_limits[ax]) for ax, v in xy.items()):
            self._limits_attempted = True  # already loaded
            return
        for v in xy.values():
            if (v["LPOSMIN"], v["LPOSMAX"]) != tp.POWER_UP_LIMITS:
                self._limits_attempted = True  # neither power-up nor calibrated: leave them alone
                self.backends[0].report(reason=f"X/Y limits {v['LPOSMIN']}..{v['LPOSMAX']} are neither power-up nor "
                                               "the calibration; not changed")
                return
            if v.get("ESTATEV") != 0 or not v.get("SRH", 0) & tp.SRH_MOTION_COMPLETE_BIT:
                return  # still settling after homing: next poll
            if abs(v.get("APOS", 99)) > 5 or abs(v.get("CPOS", 99)) > 5:
                self._limits_attempted = True  # moved off home on the power-up limits: the precheck says what to do
                return
        self._limits_attempted = True
        frames = [(ax, var, val, tp.limit_write(ax, var, val))
                  for ax in ("X", "Y") for var, val in zip(("LPOSMIN", "LPOSMAX"), self.calibrated_limits[ax],
                                                           strict=True)]
        port = link.port
        record = {"utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "writes": [], "ok": False}
        port.extra_writes = frozenset(f for *_r, f in frames)
        try:
            for ax, var, val, frame in frames:
                link.write(frame)
                got = link.read(tp.VARS[ax][var])
                record["writes"].append({"axis": ax, "var": var, "value": val, "read_back": got})
                if got != val:
                    raise _LinkError(f"{ax} {var} read back {got}, wrote {val}")
            record["ok"] = True
        except (_LinkError, TransportError, OSError) as exc:
            record["error"] = str(exc)
            self.backends[0].report(health=Health.FAULT, reason=f"calibrated limit reload failed: {exc}; no further "
                                                                 "write. Power-cycle the instrument")
        finally:
            port.extra_writes = frozenset()
        self.limits_reloads.append(record)
        self.monitor.note("calibrated X/Y limits " + ("reloaded after homing" if record["ok"] else "reload FAILED"))
        if record["ok"]:
            self.backends[0].report(reason="calibrated X/Y limits loaded after power-up homing (Gate 7 frames, "
                                           "read back; no motion)")
        if self.monitor.evidence_dir is not None:
            try:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                (self.monitor.evidence_dir / f"limits-reload-{stamp}-{'pass' if record['ok'] else 'fail'}.json"
                 ).write_text(json.dumps(record, indent=1), encoding="utf-8")
            except OSError:
                pass

    def _power_up_check(self, link: DriveLink, values: dict) -> None:
        """ADR-0040 §9: one read-only power-up record per homing (at connect, and after each re-home seen), taken
        POWER_UP_SETTLE_S after X and Y report homed and only while neither moves. Real port only (the monitor's
        folder). An index jump > 10 counts becomes a standing stage warning; never a lock."""
        folder = self.monitor.evidence_dir
        if folder is None:
            return
        homed = all(values.get(ax, {}).get("IINITONCEDONE") == tp.INIT_DONE_MARKER for ax in ("X", "Y"))
        if not homed:
            self._power_up_pending, self._homed_since = "re-home observed", None
            return
        if self._power_up_pending is None:
            return
        now = time.monotonic()
        if self._homed_since is None:
            self._homed_since = now
        moving = any(not values.get(ax, {}).get("SRH", 0) & tp.SRH_MOTION_COMPLETE_BIT for ax in ("X", "Y"))
        if now - self._homed_since < POWER_UP_SETTLE_S or moving or not self._jobs.empty():
            return
        trigger, self._power_up_pending = self._power_up_pending, None
        row = power_up_record.take(lambda ax, var: link.read(tp.VARS[ax][var]))
        try:
            notes = power_up_record.append(folder, row, "app", trigger)
        except OSError as exc:
            notes = [f"could not write the power-up record: {exc}"]
        for n in notes:
            self.monitor.note(f"power-up record ({trigger}): {n}")
            if "index-to-hard-stop" in n:
                self.monitor.warn(f"{n[0]} index", n)

    def _with_monitor(self, obs: dict) -> dict:
        """The drift monitor's state on the stage observation; a latch shows as WARNING health with its reason
        (the safety layer then refuses stage commands and names the lock)."""
        st = self.monitor.state()
        why = self.monitor.refusal()
        obs["attributes"].update(drift_lock=st["latched"], drift_lock_reason=why,
                                 drift_warnings=tuple(st["warnings"]), drift_samples=st["fast_samples"])
        if st["latched"]:
            obs["health"], obs["reason"] = Health.WARNING, why
            obs["attributes"]["health_reason"] = why
        elif st["warnings"]:
            obs["reason"] = "; ".join(st["warnings"])
        return obs

    def _report_all(self, **fields) -> None:
        for b in self.backends:
            b.report(**fields)

    def _send_stop(self, link: DriveLink) -> None:
        """The verified controlled stop (Gates 3/5) on X then Y. Clears the stop request."""
        self._stop_request.clear()
        for ax in tp.MOTION_AXES:
            for frame in tp.stop_frames(ax):
                link.write(frame)
        self.stops_sent += 1

    def _fail_pending(self, why: str) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if isinstance(job, _CarriageJob):
                job.ok, job.message = False, f"F not moved: {why}"
                job.done.set()
                continue
            if isinstance(job, _ReleaseJob):
                job.ok, job.message = False, f"lock kept: {why}"
                job.done.set()
                continue
            if isinstance(job, _RehomeJob):
                job.ok, job.message, job.stage = False, f"re-home not started: {why}", "not started"
                job.done.set()
                continue
            job.ctx.fail(why, error_code="link")

    def _run_jobs(self, link: DriveLink) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if isinstance(job, _CarriageJob):
                try:
                    self._run_carriage(link, job)
                finally:
                    job.done.set()
                continue
            if isinstance(job, _ReleaseJob):
                try:
                    reads = {ax: {n: link.read(tp.VARS[ax][n]) for n in sm.RELEASE_READS} for ax in ("X", "Y")}
                    why = self.monitor.release(reads, job.who)
                    job.ok, job.message = (not why), (why or "stage lock released; the drift monitor keeps watching")
                    if not why:  # health back to normal at once (the router refuses moves while it is a warning)
                        self.backends[0].report(health=Health.NORMAL, reason="stage lock released by the operator")
                except (_LinkError, TransportError, OSError) as exc:
                    job.ok, job.message = False, f"lock kept: X/Y reads failed ({exc})"
                    raise
                finally:
                    job.done.set()
                continue
            if isinstance(job, _RehomeJob):
                try:
                    self._run_rehome(link, job)
                except (_LinkError, TransportError, OSError, tp.ProtocolRefused) as exc:
                    job.ok, job.stage = False, "failed"
                    job.message = (f"motor link error during the re-home ({exc}); nothing more was sent. "
                                   f"The lock stays unless both axes were seen to home: {RECOVERY}")
                    raise
                finally:
                    _set_extra_writes(link, frozenset())
                    self._write_rehome_record(job)
                    job.done.set()
                continue
            if isinstance(job, (_FocusJob, _TurretJob)):
                try:
                    (self._run_focus if isinstance(job, _FocusJob) else self._run_turret)(link, job)
                except (_LinkError, TransportError, OSError, tp.ProtocolRefused) as exc:
                    job.ctx.fail(f"motor link error: {exc}", error_code="link")
                    raise
                except Exception as exc:
                    job.ctx.fail(f"motor service internal error: {exc!r}", error_code="internal")
                    raise
                continue
            try:
                self._run_move(link, job)
            except (_LinkError, TransportError, OSError, tp.ProtocolRefused) as exc:
                job.ctx.fail(f"motor link error during the move: {exc}", error_code="link")  # no-op if already ended
                raise
            except Exception as exc:
                job.ctx.fail(f"motor service internal error during the move: {exc!r}", error_code="internal")
                raise
            self._report_settled(link)

    def _report_settled(self, link: DriveLink) -> None:
        """Right after a move ends (done, stopped, failed): publish the fresh position and whether X/Y are still
        moving, so the next command is not refused as 'not idle' until the slow status poll catches up."""
        try:
            moving = any(not link.read(tp.VARS[a]["SRH"]) & tp.SRH_MOTION_COMPLETE_BIT for a in tp.MOTION_AXES)
            pos = self._position_now(link)
        except (_LinkError, TransportError, OSError):
            return  # the next poll reports it
        self.backends[0].report(position=pos, activity=Activity.MOVING if moving else Activity.IDLE,
                                reason="move ended")

    # ------------------------------------------------------------------ software re-home (ADR-0058)
    def _run_rehome(self, link: DriveLink, job: _RehomeJob) -> None:
        """Remember X/Y, home X then Y with ESTATE 19 (Gate 20's frames), let the monitor clear the lock on fresh reads,
        reload the calibrated limits if needed (ADR-0056 path), then return X then Y with the ordinary move. Anything
        off: refuse (before sending) or stop sending, keep the lock, say why."""
        job.stage = "checking X, Y and Z"
        job.record = {"utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "who": job.who,
                      "lock": self.monitor.refusal(), "frames": {ax: f.hex(" ") for ax, f in tp.XY_HOME_FRAMES.items()}}
        why = self.monitor.rehome_blocker()
        if why:
            job.stage, job.message = "refused", f"re-home not started: {why}"
            return
        pre = {ax: {n: link.read(tp.VARS[ax][n]) for n in REHOME_PRE_READS} for ax in tp.MOTION_AXES}
        z = {n: link.read(tp.VARS["Z"][n]) for n in ("ESTATEV", "APOS", "CPOS")}
        others = {ax: link.read(tp.VARS[ax]["ESTATEV"]) for ax in ("P", "F", "O")}
        job.record["before"] = {"XY": pre, "Z": z, "ESTATEV_PFO": others}
        problems = []
        for ax, v in pre.items():
            if v["ESTATEV"] not in (0, 13) or not v["SRH"] & tp.SRH_MOTION_COMPLETE_BIT:
                problems.append(f"{ax} is not idle (state {v['ESTATEV']})")
            if v["MER"]:
                problems.append(f"{ax} has a latched drive fault (MER 0x{v['MER']:04X}) that only a power cycle clears")
            if v["SRH"] & 0x8000:
                problems.append(f"{ax} reports a drive fault (SRH 0x{v['SRH']:04X}); power-cycle instead")
        if z["ESTATEV"] != 0 or abs(z["APOS"]) > REHOME_Z_BAND or abs(z["CPOS"]) > REHOME_Z_BAND:
            problems.append(f"the objective is not lowered to 0 (Z at {um_from_counts('Z', z['APOS']):.0f} µm): lower "
                            "it fully first, the stage travels to its home corner")
        problems += [f"{ax} is not idle (state {s})" for ax, s in others.items() if s != 0]
        if problems:
            job.stage, job.message = "refused", "re-home refused, nothing sent: " + "; ".join(problems)
            return
        job.remembered = {ax: pre[ax]["APOS"] for ax in tp.MOTION_AXES}
        self.monitor.note("software re-home: remembered X {X}, Y {Y} (counts)".format(**job.remembered))
        for ax in tp.MOTION_AXES:
            if self._stop_request.is_set():
                self._send_stop(link)
                job.stage, job.message = "stopped", (f"re-home stopped before {ax} by a stop request; the lock stays: "
                                                     f"{RECOVERY}")
                return
            job.stage = f"homing {ax}"
            self.backends[0].report(activity=Activity.MOVING, reason=f"software re-home: homing {ax} (ESTATE 19)")
            ok, msg = self._rehome_axis(link, job, ax)
            if not ok:
                job.stage, job.message = "failed", f"{msg}. Nothing more was sent; the lock stays: {RECOVERY}"
                self.backends[0].report(reason=f"software re-home failed: {msg}")
                return
        job.stage = "verifying"
        reads = {ax: {n: link.read(tp.VARS[ax][n]) for n in sm.REHOME_READS} for ax in tp.MOTION_AXES}
        job.record["after"] = reads
        why = self.monitor.rehome_done(reads, job.who)
        if why:
            job.stage, job.message = "failed", f"X and Y homed but the lock stays: {why}. {RECOVERY}"
            return
        job.homed = True
        self.backends[0].report(health=Health.NORMAL, reason="stage lock cleared by a software re-home (ADR-0058)")
        values = self.poll_once(link)  # fresh state; the ADR-0056 path reloads calibrated limits if needed
        if values is not None:
            if self.monitor.status(values) is not None:
                self._on_latch(link)
            self._publish(values)
            self._maybe_reload_limits(link, values)
        self._rehome_return(link, job)

    def _rehome_axis(self, link: DriveLink, job: _RehomeJob, ax: str) -> tuple[bool, str]:
        """One home request and its follow-up: started (ESTATEV 19 or HOMEDONE cleared), then done (ESTATEV 0 with
        HOMEDONE set). No stop is possible meanwhile: the drive reads ESTATE only after homing."""
        frame = tp.home_frame(ax)
        self.monitor.begin_rehome(ax)
        trace: list = []
        outcome = "failed"
        t0 = time.monotonic()
        try:
            _set_extra_writes(link, frozenset({frame}))
            try:
                link.write(frame)
            finally:
                _set_extra_writes(link, frozenset())
            started = False
            while True:
                est = link.read(tp.VARS[ax]["ESTATEV"])
                hd = link.read(tp.VARS[ax]["HOMEDONE"])
                apos = link.read(tp.VARS[ax]["APOS"])
                t = time.monotonic() - t0
                trace.append([round(t, 3), est, hd, apos])
                if not started and (est == tp.HOME_STATE or not hd & 1):
                    started = True
                if started and est == 0:
                    if not hd & 1:
                        return False, f"{ax} homing ended without completing (interrupted or over-current)"
                    outcome = "homed"
                    return True, f"{ax} homed in {t:.1f} s"
                if not started and t > self.rehome_start_s:
                    return False, f"{ax} did not start homing within {self.rehome_start_s:g} s"
                if t > self.rehome_timeout_s:
                    return False, f"{ax} still homing after {self.rehome_timeout_s:g} s"
                self._sleep(REHOME_POLL_S)
        finally:
            job.record.setdefault("axes", {})[ax] = {"outcome": outcome, "seconds": round(time.monotonic() - t0, 3),
                                                     "trace": trace}
            self.monitor.end_rehome(ax, outcome)

    def _rehome_return(self, link: DriveLink, job: _RehomeJob) -> None:
        """Back to the remembered X/Y with the ordinary verified move (X then Y, pre-move and landing checks)."""
        targets = dict(job.remembered)
        um = (um_from_counts("X", targets["X"]), um_from_counts("Y", targets["Y"]))
        lim = {ax: (link.read(tp.VARS[ax]["LPOSMIN"]), link.read(tp.VARS[ax]["LPOSMAX"])) for ax in tp.MOTION_AXES}
        outside = [f"{ax} {targets[ax]} outside {lo}..{hi}" for ax, (lo, hi) in lim.items()
                   if not lo <= targets[ax] <= hi]
        if outside:
            job.stage, job.message = "homed, not returned", (
                "Stage re-homed and the lock cleared, but the old position is outside the drives' current limits ("
                + "; ".join(outside) + "): the stage stays at home. Move it from the Capture page")
            return
        job.stage = "returning X then Y"
        self.backends[0].report(activity=Activity.MOVING,
                                reason=f"software re-home: returning to X {um[0] / 1000:.3f}, Y {um[1] / 1000:.3f} mm")
        ctx = _ReturnCtx()
        self._run_move(link, _MoveJob("rehome-return", ctx, targets, um))
        self._report_settled(link)
        job.record["return"] = {"targets": targets, "ok": ctx.ok, "message": ctx.message}
        if ctx.ok:
            job.ok, job.stage, job.returned_um = True, "done", um
            job.message = ("Stage re-homed (X then Y), the lock cleared, and the stage is back at "
                           f"X {um[0] / 1000:.3f}, Y {um[1] / 1000:.3f} mm")
        else:
            job.stage = "homed, return failed"
            job.message = f"Stage re-homed and the lock cleared, but the return move did not finish: {ctx.message}"

    def _write_rehome_record(self, job: _RehomeJob) -> None:
        job.record.update(stage=job.stage, ok=job.ok, homed=job.homed, message=job.message,
                          remembered=job.remembered)
        self.monitor.note(f"software re-home: {job.stage}: {job.message}")
        folder = self.monitor.evidence_dir
        if folder is None:
            return
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"stage-rehome-{stamp}-{'pass' if job.ok else job.stage.replace(' ', '_')}.json").write_text(
                json.dumps(job.record, indent=1, default=str), encoding="utf-8")
        except OSError:
            pass

    def _run_move(self, link: DriveLink, job: _MoveJob) -> None:
        ctx = job.ctx
        if getattr(ctx, "cancel_requested", False) or self._stop_request.is_set():
            if self._stop_request.is_set():
                self._send_stop(link)
            ctx.mark_cancelled(None)
            return
        if self.monitor.latched:
            ctx.fail(f"{self.monitor.refusal()}; nothing written", error_code="stage_latched")
            return
        # fresh state right before writing: idle, homed, at rest, targets inside the drive's own limits
        now = {ax: {n: link.read(tp.VARS[ax][n]) for n in ("ESTATEV", "IINITONCEDONE", "SRH", "APOS", "CPOS",
                                                           "LPOSMIN", "LPOSMAX", "CSPDCMD")} for ax in tp.MOTION_AXES}
        # an axis already within the drive's settle band of its target (2.2 um) is not commanded at all
        moved = [ax for ax in tp.MOTION_AXES if abs(job.targets[ax] - now[ax]["APOS"]) > SETTLE_BAND_COUNTS]
        for ax, v in now.items():
            why = ""
            moves = ax in moved  # an axis that stays put is not refused for its own lock or drift
            if moves and ax in self._drift_locked:
                why = f"locked ({self._drift_locked[ax]})"
            elif v["IINITONCEDONE"] != tp.INIT_DONE_MARKER:
                why = "not homed"
            elif v["ESTATEV"] not in (0, 13) or not v["SRH"] & tp.SRH_MOTION_COMPLETE_BIT:
                why = f"not idle (state {v['ESTATEV']})"
            elif not v["LPOSMIN"] <= job.targets[ax] <= v["LPOSMAX"]:
                why = (f"target {job.targets[ax]} outside the drive's current limits {v['LPOSMIN']}..{v['LPOSMAX']}"
                       + ("; the drives hold their power-up limits: run Gate 7 (calibrated limits) after power-on"
                          if v["LPOSMAX"] == 50000 else ""))
            elif moves:
                # closed-loop stepper commutation: >~14 counts of encoder/rotor mismatch reverses torque (ADR-0030).
                # Refused and latched (ADR-0040): the phase may already be wrong.
                drift = self.monitor.pre_move(ax, v["APOS"], v["CPOS"])
                if drift:
                    why = f"{drift}; the stage is now locked: {RECOVERY}"
            if why:
                ctx.fail(f"{ax}: {why}; nothing written", error_code="precondition")
                return
        ctx.mark_started()
        start = {ax: now[ax]["APOS"] for ax in tp.MOTION_AXES}
        # Plan v3 ground rule: one axis at a time (SEQUENTIAL_XY); the monitor watches the still one.
        legs = [[ax] for ax in moved] if self.sequential_xy else ([moved] if moved else [])
        for i, leg in enumerate(legs):
            if i:
                self._settle_between_legs(link, ctx)
                if self._stop_request.is_set() or getattr(ctx, "cancel_requested", False):
                    self._send_stop(link)
                    ctx.mark_cancelled(self._position_now(link))
                    return
                if self.monitor.latched:
                    ctx.fail(f"{'+'.join(leg)} not moved: {self.monitor.refusal()}", error_code="stage_latched")
                    return
                for ax in tp.MOTION_AXES:  # fresh, right before this leg: the previous leg moved the other axis
                    start[ax] = link.read(tp.VARS[ax]["APOS"])
                for ax in leg:
                    drift = self.monitor.pre_move(ax, start[ax], link.read(tp.VARS[ax]["CPOS"]))
                    if drift:
                        ctx.fail(f"{ax}: {drift}; the stage is now locked: {RECOVERY}; {ax} not moved",
                                 error_code="encoder_drift")
                        return
            if not self._move_leg(link, job, leg, now, start):
                return
        ctx.complete(self._position_now(link))

    # ------------------------------------------------------------------ Z focus and objective turret (ADR-0053)
    def _backend(self, device_id: str) -> TelemetryBackend:
        return next(b for b in self.backends if b.device_id == device_id)

    def _report_z(self, apos: int, moving: bool = False) -> Position:
        pos = Position(axes=("z",), values=(round(um_from_counts("Z", apos), 3),), unit="um", frame="focus")
        self._backend("motor.focus").report(position=pos, activity=Activity.MOVING if moving else Activity.IDLE,
                                            reason="focus moving" if moving else "focus move ended")
        return pos

    def _report_o(self, position: int, moving: bool = False) -> Position:
        pos = Position(axes=("o",), values=(float(position),), unit="position", frame="turret")
        self._backend("motor.objective_turret").report(
            position=pos, activity=Activity.MOVING if moving else Activity.IDLE,
            reason=f"objective {TURRET_OBJECTIVES.get(position, '?')}" if position else "turret between positions")
        return pos

    def _stop_requested(self, ctx=None) -> bool:
        return self._stop_request.is_set() or self._stop.is_set() or bool(getattr(ctx, "cancel_requested", False))

    def _z_move(self, link: DriveLink, target: int, ctx=None) -> str:
        """Z to `target` counts as Motor Gate 18 did (ADR-0053): target, ESTATE 8, completion on SRH bit 9 + ESTATEV 13 +
        CPOS = target, APOS within LCONVERGEBAND, then ESTATE 6 at rest. '' = done; otherwise why it stopped (Z's stop
        sent). Never writes speed or acceleration."""  # noqa: E501
        z = tp.VARS["Z"]
        st = {n: link.read(z[n]) for n in ("ESTATEV", "IINITONCEDONE", "APOS", "CPOS", "LCONVERGEBAND", "MER", "SRH")}
        band = st["LCONVERGEBAND"]
        if st["IINITONCEDONE"] != tp.INIT_DONE_MARKER:
            return "Z is not homed"
        if st["ESTATEV"] not in (0, 13) or not st["SRH"] & tp.SRH_MOTION_COMPLETE_BIT:
            return f"Z is not idle (state {st['ESTATEV']})"
        if st["MER"] & 0xFFFF:
            return f"Z drive error MER 0x{st['MER'] & 0xFFFF:04X}"
        if not 0 < band <= 50:
            return f"Z LCONVERGEBAND {band} not sane"
        start = st["APOS"]
        if abs(start - target) <= band:
            return ""
        if abs(start - st["CPOS"]) > max(band, DRIFT_MAX_COUNTS):
            return f"Z encoder {start} disagrees with its command {st['CPOS']}"
        link.write(tp.z_target(target))
        link.write(tp.Z_MOVE_FRAME)
        t0 = time.monotonic()
        deadline = t0 + abs(target - start) / Z_SPEED_COUNTS_S_MIN + Z_TIMEOUT_MARGIN_S
        why, last_report = "", 0.0
        while True:
            self._sleep(MOVE_POLL_S)
            if self.fast_log and time.monotonic() - self._last_fast >= FAST_PERIOD_S:
                self._fast_sample(link)
            if self._stop_requested(ctx):
                why = "stopped"
                break
            srh, est, cpos, apos = (link.read(z[n]) for n in ("SRH", "ESTATEV", "CPOS", "APOS"))
            if abs(target - apos) > abs(target - start) + Z_WRONG_WAY_COUNTS:
                why = f"Z moved away from its target ({start} -> {apos}, target {target})"
                break
            if srh & tp.SRH_MOTION_COMPLETE_BIT and est == 13 and cpos == target:
                break
            if time.monotonic() > deadline:
                why = f"Z did not reach {target} in time (at {apos})"
                break
            if time.monotonic() - last_report > 0.25:
                last_report = time.monotonic()
                self._report_z(apos, moving=True)
        if not why:
            apos = link.read(z["APOS"])
            if abs(apos - target) > band:
                why = f"Z ended at {apos}, target {target} (band {band})"
        link.write(tp.Z_STOP_FRAME)  # stop at rest (Gate 18) or the stop on a fault
        if why == "stopped" and self._stop_request.is_set():
            self._send_stop(link)  # X/Y too, and clears the request
        self._report_z(link.read(z["APOS"]))
        return why

    def _run_focus(self, link: DriveLink, job: _FocusJob) -> None:
        ctx = job.ctx
        if self._stop_requested(ctx):
            ctx.mark_cancelled(None)
            return
        ctx.mark_started()
        why = self._z_move(link, job.counts, ctx)
        pos = self._backend("motor.focus").read_position()
        if why == "stopped":
            ctx.mark_cancelled(pos)
        elif why:
            ctx.fail(f"{why}; Z stop sent", error_code="focus")
        else:
            ctx.complete(pos)

    def _run_turret(self, link: DriveLink, job: _TurretJob) -> None:
        """Objective change as Motor Gate 19 rev 2 did it (ADR-0053): refuse unless the stage is centred and the turret is
        idle, off, error-free and at a known position; retract Z to 0; ITARGETPOS n + ESTATE 3; poll fast; done when the
        sensor reads n with the program idle and the motor off. Stuck flag, a position off the decreasing path, Z moving,
        a timeout or STOP: the vendor's AXISOFF, once."""  # noqa: E501
        ctx, n = job.ctx, job.position
        if self._stop_requested(ctx):
            ctx.mark_cancelled(None)
            return
        cur, problems = self._turret_preconditions(link)
        if problems:
            ctx.fail("; ".join(problems) + "; nothing written", error_code="turret_precondition")
            return
        ctx.mark_started()
        if cur == n:
            ctx.complete(self._report_o(cur))
            return
        why = self._z_move(link, 0, ctx)  # fully retracted (Gate 18) before the objectives swing
        if why:
            if why == "stopped":
                ctx.mark_cancelled(self._backend("motor.objective_turret").read_position())
            else:
                ctx.fail(f"Z could not retract to 0 before the turret change: {why}; turret not moved", error_code="focus")  # noqa: E501
            return
        why = self._turret_turn(link, ctx, cur, n)
        if why:
            self._turret_abort(link, ctx, why)
            return
        ctx.complete(self._report_o(n))

    def _turret_preconditions(self, link: DriveLink) -> tuple[int, list[str]]:
        """The turret's sensor position and every reason not to turn it now (empty: go)."""
        o = tp.VARS["O"]
        st = {k: link.read(o[k]) for k in ("ESTATEV", "ICURRENTPOSHF", "IHF", "IPENDINGMOVES", "ISTATUSFLAGS", "SRL",
                                            "MER")}
        xy = {ax: um_from_counts(ax, link.read(tp.VARS[ax]["APOS"])) / 1000.0 for ax in ("X", "Y")}
        cur = st["ICURRENTPOSHF"]
        problems = [f"stage {ax} at {xy[ax]:.1f} mm, outside {lo:g}-{hi:g} mm: centre the stage before changing objectives"  # noqa: E501
                    for ax, (lo, hi) in TURRET_STAGE_BOX_MM.items() if not lo <= xy[ax] <= hi]
        if problems:  # can the stage reach the centre at all? (audit APP-07)
            lim_max = {ax: link.read(tp.VARS[ax]["LPOSMAX"]) for ax in ("X", "Y")}
            if any(v == tp.POWER_UP_LIMITS[1] for v in lim_max.values()):
                problems = [("the stage's calibrated travel limits are not loaded (drives on their power-up box, "
                             "0-22 mm), so it cannot reach the centre the turret needs (X 40-80, Y 20-60 mm): return "
                             "the stage to home and restart the app, which loads them (Gate 7, ADR-0056)")]
        if st["ESTATEV"] != 0 or st["IPENDINGMOVES"] != 0:
            problems.append(f"turret not idle (state {st['ESTATEV']}, pending {st['IPENDINGMOVES']})")
        if st["ISTATUSFLAGS"] & TURRET_STUCK_FLAG or st["MER"] & 0xFFFF:
            problems.append(f"turret fault flags (ISTATUSFLAGS 0x{st['ISTATUSFLAGS']:04X}, MER 0x{st['MER']:04X}); "
                            "power-cycle the instrument")
        if st["SRL"] & 0x8000:
            problems.append("turret motor is on at rest")
        if cur not in tp.O_POSITIONS and cur != 0:
            problems.append(f"turret position unknown (sensor {cur}); turn it onto an objective by hand")
        # cur == 0: between positions (a stalled or aborted turn, 2026-11-18). The drive's own GOTO_POS turns one way
        # until the sensor shows the target and stops itself if the sensor does not change (stuck flag); with the
        # program idle and the checks above, that is the recovery turn (operator: "rotate the motor at the pos")
        return cur, problems

    @staticmethod
    def _turret_path(cur: int, n: int) -> list[int]:
        """The positions the sensor passes from `cur` to `n`: they DECREASE as the turret turns (Gate 19)."""
        return [(cur - i - 2) % 5 + 1 for i in range((cur - n) % 5)]

    @staticmethod
    def _turret_off_path(pos: int, last: int, path: list[int]) -> bool:
        """True when a newly seen position is not on the path or goes back along it."""
        return pos not in path or (last in path and path.index(pos) < path.index(last))

    def _turret_turn(self, link: DriveLink, ctx, cur: int, n: int) -> str:
        """Start the turn from `cur` to `n` and poll it. '' = settled at n; otherwise why it was stopped."""
        o = tp.VARS["O"]
        z0 = link.read(tp.VARS["Z"]["APOS"])
        # from between positions (cur 0) the path starts at the first position the sensor shows; allow a full turn
        path = self._turret_path(cur, n) if cur else []
        link.write(tp.o_target(n))
        link.write(tp.O_GOTO_FRAME)
        t0 = time.monotonic()
        deadline = t0 + (len(path) if cur else len(tp.O_POSITIONS)) * TURRET_STEP_S_MAX + 5.0
        seen, done_polls = [cur], 0
        self._turret_trace: list[tuple[float, dict]] = [(0.0, {"ICURRENTPOSHF": cur})]  # every poll, for evidence
        self._report_o(cur, moving=True)
        while True:
            self._sleep(MOVE_POLL_S)
            if self._stop_requested(ctx):
                return "stopped"
            s = {k: link.read(o[k]) for k in ("ESTATEV", "ICURRENTPOSHF", "ISTATUSFLAGS", "IPENDINGMOVES", "SRL")}
            self._turret_trace.append((round(time.monotonic() - t0, 3), s))
            pos = s["ICURRENTPOSHF"]
            if pos in tp.O_POSITIONS and pos != seen[-1] and seen[-1] == 0 and not path:
                # the first position seen after starting between positions: the path runs from here
                path = [pos, *self._turret_path(pos, n)] if pos != n else [n]
            if pos in tp.O_POSITIONS and pos != seen[-1]:
                if self._turret_off_path(pos, seen[-1], path):
                    # the whole sensor sequence (2026-11-18 18:49: only the last reading was kept, so a sensor bounce
                    # could not be told from a turn going backwards)
                    seq = " -> ".join(str(p) for p in [*seen, pos])
                    return (f"turret reached position {pos}, not next on its path {cur} -> {path} "
                            f"(sensor sequence {seq})")
                seen.append(pos)
                self._report_o(pos, moving=True)
            if s["ISTATUSFLAGS"] & TURRET_STUCK_FLAG:
                return f"the drive flagged the turret stuck at position {pos}"
            if abs(link.read(tp.VARS["Z"]["APOS"]) - z0) > 5:
                return "Z moved during the turret change"
            done = pos == n and s["ESTATEV"] == 0 and s["IPENDINGMOVES"] == 0 and not s["SRL"] & 0x8000
            done_polls = done_polls + 1 if done else 0
            if done_polls >= 2:
                return ""
            if time.monotonic() > deadline:
                return f"turret did not settle at position {n} in time (sensor {pos})"

    def _write_turret_trace(self, why: str) -> str:
        """The aborted turn's every poll (time, sensor, state, flags) as evidence next to the stage monitor's; returns
        `why`, naming the file when one was written. Read-only bookkeeping: nothing goes to the drives."""
        trace = getattr(self, "_turret_trace", None)
        folder = getattr(self.monitor, "evidence_dir", None)
        if not trace or folder is None or why == "stopped":
            return why
        try:
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / f"turret-trace-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
            p.write_text(json.dumps({"why": why, "poll_s": MOVE_POLL_S, "polls": trace}, indent=1), encoding="utf-8")
            return f"{why}; trace: {p.name}"
        except OSError:
            return why

    def _turret_abort(self, link: DriveLink, ctx, why: str) -> None:
        """The vendor's AXISOFF, once (and the X/Y stop on an operator STOP); then cancel or fail the command."""
        link.write(tp.O_AXISOFF_FRAME)
        if why == "stopped" and self._stop_request.is_set():
            self._send_stop(link)
        why = self._write_turret_trace(why)
        pos = self._report_o(link.read(tp.VARS["O"]["ICURRENTPOSHF"]))
        if why == "stopped":
            ctx.mark_cancelled(pos)
        else:
            ctx.fail(f"{why}; turret motor switched off; power-cycle the instrument before the next change",
                     error_code="turret")

    def _run_carriage(self, link: DriveLink, job: _CarriageJob) -> None:
        """F to job.dest exactly as Motor Gates 11/12 did on the instrument (ADR-0051): refuse unless F is homed, idle,
        at rest, error-free and holds the reviewed stored positions and limits; write only the drive's own
        GOTO_CUBE / GOTO_MIRR; complete on ISTATUSFLAGS 0x4000 with APOS inside LCONVERGEBAND of the stored target;
        on a fault, STOP or timeout send F's own STATE_15_STOP. X and Y keep their ~20 Hz drift samples."""
        try:
            self._carriage_procedure(link, job)
        except (_LinkError, TransportError, OSError, tp.ProtocolRefused) as exc:
            job.ok, job.message = False, f"F: motor link error ({exc}); F stop not confirmed"
            raise

    def _carriage_procedure(self, link: DriveLink, job: _CarriageJob) -> None:
        f = tp.VARS["F"]
        if self._stop_request.is_set():
            self._send_stop(link)
            job.message = "F not moved: STOP"
            return
        names = ("ESTATEV", "IINITONCEDONE", "APOS", "CPOS", "LPOSMIN", "LPOSMAX", "LCUBE1POS", "LCUBE2POS",
                 "LCUBE3POS", "LCUBE4POS", "LMIRROR1POS", "LCONVERGEBAND", "MER", "ISTATUSFLAGS")
        base = {n: link.read(f[n]) for n in names}
        band = base["LCONVERGEBAND"]
        stored = {"slot1": base["LCUBE1POS"], "slot2": base["LCUBE2POS"], "slot3": base["LCUBE3POS"],
                  "slot4": base["LCUBE4POS"], "mirror": base["LMIRROR1POS"]}
        target = stored[job.dest]
        problems = self._carriage_problems(base, stored)
        if problems:
            job.message = "F not moved: " + "; ".join(problems)
            return
        self._carriage_move(link, job, base["APOS"], target, band)

    @staticmethod
    def _carriage_problems(base: dict, stored: dict) -> list[str]:
        """Every reason not to move F now (empty: go)."""
        band = base["LCONVERGEBAND"]
        problems = []
        if base["IINITONCEDONE"] != tp.INIT_DONE_MARKER:
            problems.append("F is not homed")
        if base["ESTATEV"] != 0:
            problems.append(f"F is not idle (state {base['ESTATEV']})")
        if stored != tp.F_EXPECTED_POS:
            problems.append("F's stored cube/mirror positions are not the reviewed ones")
        if (base["LPOSMIN"], base["LPOSMAX"]) != tp.F_EXPECTED_LIMITS:
            problems.append(f"F limits {base['LPOSMIN']}..{base['LPOSMAX']} are not the reviewed 0..128000")
        if not 0 < band <= 50:
            problems.append(f"F LCONVERGEBAND {band} not sane")
        if abs(base["APOS"] - base["CPOS"]) > max(band, 1):
            problems.append(f"F not at rest (APOS {base['APOS']}, CPOS {base['CPOS']})")
        if base["MER"] & 0xFFFF:
            problems.append(f"F error register MER 0x{base['MER'] & 0xFFFF:04X} not clear")
        return problems

    def _carriage_move(self, link: DriveLink, job: _CarriageJob, start: int, target: int, band: int) -> None:
        """F from `start` to the stored `target` of job.dest; the outcome in job.ok / job.message."""
        f = tp.VARS["F"]
        if abs(start - target) <= band:
            job.ok, job.message = True, f"F already at {job.dest} (APOS {start})"
            return
        for frame in tp.f_move_frames(job.dest):
            link.write(frame)
        t0 = time.monotonic()
        deadline = t0 + abs(target - start) / CARRIAGE_SPEED_COUNTS_S_MIN + CARRIAGE_TIMEOUT_MARGIN_S
        why = ""
        while True:
            self._sleep(CARRIAGE_POLL_S)
            if self.fast_log and time.monotonic() - self._last_fast >= FAST_PERIOD_S:
                self._fast_sample(link)
            if self._stop_request.is_set() or self._stop.is_set():
                why = "STOP"
                break
            flags, apos = link.read(f["ISTATUSFLAGS"]), link.read(f["APOS"])
            if abs(target - apos) > abs(target - start) + CARRIAGE_WRONG_WAY_COUNTS:
                why = f"F moved away from {job.dest} ({start} -> {apos}, target {target})"
                break
            if flags & tp.F_CONVERGED and abs(apos - target) <= band:
                break
            if time.monotonic() > deadline:
                why = f"F did not reach {job.dest} in time (APOS {apos}, target {target})"
                break
        if why:
            link.write(tp.F_STOP_FRAME)
            if self._stop_request.is_set():
                self._send_stop(link)
            job.message = f"{why}; F stop sent"
            return
        fin = {n: link.read(f[n]) for n in ("ESTATEV", "APOS", "MER")}
        if fin["ESTATEV"] != 0 or abs(fin["APOS"] - target) > band or fin["MER"] & 0xFFFF:
            job.message = f"F at {fin['APOS']} (target {target}) failed its final checks: {fin}"
            return
        job.ok = True
        job.message = f"F at {job.dest}: APOS {fin['APOS']} in {time.monotonic() - t0:.1f} s"

    def _settle_between_legs(self, link: DriveLink, ctx) -> None:
        """Fast X/Y samples for the monitor's settle time before the next leg takes the still axis' reference.
        A drive reports motion complete inside its 5-count band, and the axis keeps converging: on COM3 (2026-11-16)
        X went -19 -> -1 -> 0 after its go-to leg, while Y's leg had already started. A reference taken mid-settle
        would read that settling as drift and lock the stage. Ends early on a stop, a cancel or a latch."""
        end = time.monotonic() + SETTLE_S
        while (now := time.monotonic()) < end:
            if self._stop_request.is_set() or getattr(ctx, "cancel_requested", False) or self.monitor.latched:
                return
            self._sleep(max(0.0, min(end - now, FAST_PERIOD_S - (now - self._last_fast))))
            if time.monotonic() - self._last_fast >= FAST_PERIOD_S:
                self._fast_sample(link)

    def _move_leg(self, link: DriveLink, job: _MoveJob, leg: list[str], now: dict, start: dict) -> bool:
        """Move the axes in `leg` (one, under SEQUENTIAL_XY) to their targets. True when done and verified; else the
        command has been failed or cancelled and False is returned. Steps (audit APP-16): speed, target, start,
        supervise to motion complete, the drive's flags, the still axis' encoder, the landing, the stop at rest."""
        preset = self.speed_preset if self.speed_control else None
        speed = self._leg_speeds(link, leg, now, preset)
        for ax in leg:
            link.write(tp.move_target(ax, job.targets[ax]))
        self.monitor.begin_move({ax: job.targets[ax] for ax in leg}, start)
        # runaway watchdog, shadow mode (ADR-0047): watches and records, never acts
        label = preset if preset is not None else "fast"  # the power-up speed is the fast preset (ADR-0032)
        run = _Leg(job.ctx, leg, job.targets, start, speed,
                   {ax: watchdog.LegWatch(ax, start[ax], job.targets[ax], label, time.monotonic()) for ax in leg})
        try:
            for ax in leg:
                link.write(tp.move_request(ax))
            if not (self._supervise_leg(link, run) and self._leg_flags_ok(link, run)
                    and self._leg_still_axis_ok(link, run) and self._leg_landed(link, run)):
                return False
            for ax in leg:  # the watchdog's cross-check at rest: POSERR against CPOS - APOS (ADR-0047)
                run.rest[ax] = {n: link.read(tp.VARS[ax][n]) for n in ("POSERR", "CPOS", "APOS")}
            self._stop_axes(link, leg)  # back to drive state 0 with the verified stop at rest (as the Motor Gates do)
            run.outcome = "done"
            return True
        finally:
            self.monitor.end_move(leg, run.outcome)
            self._watch_end(run.watches, run.outcome, run.rest)

    def _leg_speeds(self, link: DriveLink, leg: list[str], now: dict, preset: str | None) -> dict[str, float]:
        """The selected speed (ADR-0032), written only when the drive holds another value; each axis' speed in mm/s."""
        speed = {}
        for ax in leg:
            if preset is not None and now[ax]["CSPDCMD"] != tp.speed_raw(preset):
                link.write(tp.speed_write(ax, preset))
                now[ax]["CSPDCMD"] = tp.speed_raw(preset)
                self._speed_changed = True
            speed[ax] = max(now[ax]["CSPDCMD"], 1) / 65536 / tp.SPEED_SAMPLE_S / tp.COUNTS_PER_MM[ax]  # mm/s
        return speed

    @staticmethod
    def _stop_axes(link: DriveLink, axes: list[str]) -> None:
        """The verified controlled stop on these axes only."""
        for ax in axes:
            for frame in tp.stop_frames(ax):
                link.write(frame)

    def _supervise_leg(self, link: DriveLink, run: _Leg) -> bool:
        """Poll until every axis of the leg reports motion complete. False: stopped or failed (the command ended)."""
        longest_s = max(abs(run.targets[a] - run.start[a]) / tp.COUNTS_PER_MM[a] / run.speed[a] for a in run.axes)
        run.deadline = time.monotonic() + longest_s * 1.2 + 3.0
        run.live, tick = dict(run.start), time.monotonic()
        while len(run.done) < len(run.axes):
            self._sleep(max(0.0, MOVE_POLL_S - (time.monotonic() - tick)))
            tick = time.monotonic()
            if not self._leg_tick(link, run, tick):
                return False
        return True

    def _leg_tick(self, link: DriveLink, run: _Leg, tick: float) -> bool:
        """One poll of a moving leg: STOP/cancel, the drift monitor, the watchdog's signals, the wrong-way guard,
        completion, the live position and the timeout. False: the command has ended."""
        ctx = run.ctx
        if self._stop_request.is_set() or getattr(ctx, "cancel_requested", False):
            run.outcome = "stopped"
            self._send_stop(link)
            ctx.mark_cancelled(self._position_now(link))
            return False
        run.live["X"], run.live["Y"] = self._fast_sample(link)  # both encoders, every tick (~20 Hz)
        if self.monitor.latched:  # the still axis changed while this leg moved (or anything else tripped)
            run.outcome = "stopped by the drift monitor"
            self._stop_axes(link, run.axes)
            rule = self.monitor.latch.rule if self.monitor.latch else "stage_latched"
            ctx.fail(f"{rule}: {self.monitor.refusal()}; controlled stop sent", error_code=rule)
            return False
        for ax in run.axes:  # the watchdog's signals: encoder every tick, POSERR and IQ on alternate ticks
            if ax not in run.done:
                run.watches[ax].apos(run.live[ax], tick)
                name = next(run.extra)
                getattr(run.watches[ax], name.lower())(link.read(tp.VARS[ax][name]), time.monotonic())
        for ax in run.axes:
            if ax in run.done:
                continue
            if self._leg_wrong_way(link, run, ax):
                return False
            if link.read(tp.VARS[ax]["SRH"]) & tp.SRH_MOTION_COMPLETE_BIT:
                run.done.add(ax)
        if time.monotonic() - run.last_report > 0.25:  # live position while moving (the poll is paused)
            run.last_report = time.monotonic()
            self._report_live(run.live)
        if len(run.done) < len(run.axes) and time.monotonic() > run.deadline:
            run.outcome = "timeout"
            self._send_stop(link)
            ctx.fail(f"{', '.join(a for a in run.axes if a not in run.done)} did not complete in time; controlled "
                     "stop sent", error_code="timeout")
            return False
        return True

    def _leg_wrong_way(self, link: DriveLink, run: _Leg, ax: str) -> bool:
        """Wrong-way guard (2026-11-13: Y commanded to 0 ran to the far end, 77.7 mm). Moving AWAY from the target by
        more than WRONG_WAY_COUNTS means the axis is not under control: stop now and latch. True: it tripped."""
        target, apos, start = run.targets[ax], run.live[ax], run.start[ax]
        if abs(target - apos) <= abs(target - start) + WRONG_WAY_COUNTS:
            return False
        run.outcome = "wrong direction"
        self._send_stop(link)
        self.monitor.trip("wrong_direction", ax,
                          f"{ax} moved away from its target ({start} -> {apos}, target {target})",
                          {"start": start, "apos": apos, "target": target})
        self._on_latch(link)
        run.ctx.fail(f"wrong_direction: {ax} moved away from its target ({start} -> {apos}, target {target}); "
                     f"controlled stop sent; stage locked: {RECOVERY}", error_code="wrong_direction")
        return True

    def _leg_flags_ok(self, link: DriveLink, run: _Leg) -> bool:
        """The drive's own record of this move (plan §6.7), before the stop at rest. False: latched and failed."""
        for ax in run.axes:
            if self.monitor.after_move(ax, link.read(tp.VARS[ax]["ISTATUSFLAGS"]),
                                       link.read(tp.VARS[ax]["SRL"])) is not None:
                run.outcome = "drive flags"
                self._on_latch(link)
                self._send_stop(link)
                rule = self.monitor.latch.rule if self.monitor.latch else "stage_latched"
                run.ctx.fail(f"{rule}: {self.monitor.refusal()}; stop sent", error_code=rule)
                return False
        return True

    def _leg_still_axis_ok(self, link: DriveLink, run: _Leg) -> bool:
        """Backstop: the still axis' encoder against where it was before this leg. False: latched and failed."""
        for other in tp.MOTION_AXES:
            if other in run.axes:
                continue
            o_apos, o_start = link.read(tp.VARS[other]["APOS"]), run.start[other]
            if abs(o_apos - o_start) > DRIFT_MAX_COUNTS:
                run.outcome = "cross-axis drift"
                self.monitor.trip("cross_axis_encoder_drift", other,
                                  f"{other} changed {o_apos - o_start:+d} counts while {'+'.join(run.axes)} moved",
                                  {"start": o_start, "apos": o_apos, "moving": run.axes})
                self._on_latch(link)
                self._stop_axes(link, run.axes)
                run.ctx.fail(f"cross_axis_encoder_drift: {self.monitor.refusal()}",
                             error_code="cross_axis_encoder_drift")
                return False
        return True

    def _leg_landed(self, link: DriveLink, run: _Leg) -> bool:
        """Each moved axis' encoder against its target. False: no motion at all (failed), or a landing error (latched
        and failed)."""
        ctx = run.ctx
        for ax in run.axes:
            target, start = run.targets[ax], run.start[ax]
            apos = link.read(tp.VARS[ax]["APOS"])
            if abs(apos - target) > SETTLE_BAND_COUNTS and abs(apos - start) <= SETTLE_BAND_COUNTS:
                # Commanded, but the encoder never moved, with the power stage reading on (the flags above). Not
                # latched (operator decision 2026-11-14); the status poll latches if the power stage turns off.
                run.outcome = "no motion"
                self._send_stop(link)
                ctx.fail(f"{ax} did not move at all (still at {apos}, target {target}): its power stage may be "
                         "off after a drive protection trip; power-cycle the instrument to recover",
                         error_code="no_motion")
                return False
            if abs(apos - target) > SETTLE_BAND_COUNTS:  # landing error (plan §6.3): latch
                run.outcome = "landing error"
                self.monitor.trip("landing_error", ax, f"{ax} ended at {apos}, {apos - target:+d} counts from its "
                                                       f"target {target} (band {SETTLE_BAND_COUNTS})",
                                  {"start": start, "apos": apos, "target": target})
                self._on_latch(link)
                self._send_stop(link)
                ctx.fail(f"{ax} ended at {apos}, target {target} (band {SETTLE_BAND_COUNTS}); stop sent; stage "
                         f"locked: {RECOVERY}", error_code="readback")
                return False
        return True

    def _watch_end(self, watches: dict, outcome: str, rest: dict) -> None:
        """One motion-profile row per leg (ADR-0047 Phase 1): kept in memory, appended to the profile file on the
        real port only, and any would-trip noted on the monitor. Nothing is stopped or latched by the watchdog."""
        for ax, w in watches.items():
            row = w.row(outcome, rest.get(ax))
            self.watch_rows.append(row)
            del self.watch_rows[:-50]
            for wt in w.would_trip:
                self.monitor.note(f"watchdog (shadow, not acted on): {ax} {wt}")
            if self.monitor.evidence_dir is not None:
                try:
                    watchdog.append(self.monitor.evidence_dir, row)
                except OSError as exc:
                    self.monitor.note(f"could not write the motion profile: {exc}")

    def _report_live(self, counts: dict) -> None:
        pos = Position(axes=("x", "y"), values=(round(um_from_counts("X", counts["X"]), 3),
                                                round(um_from_counts("Y", counts["Y"]), 3)), unit="um", frame="stage")
        self.backends[0].report(position=pos, activity=Activity.MOVING, reason="moving")
    def _position_now(self, link: DriveLink) -> Position:
        """Fresh encoder read of X/Y; also refreshes the stage backend's read_position(), which the router
        uses to verify a move before it can succeed."""
        x, y = (link.read(tp.VARS[a]["APOS"]) for a in ("X", "Y"))
        pos = Position(axes=("x", "y"), values=(round(um_from_counts("X", x), 3), round(um_from_counts("Y", y), 3)),
                       unit="um", frame="stage")
        stage = self.backends[0]
        with stage._lock:
            stage._position = pos
        return pos


def observation(spec: DeviceSpec, values: dict[str, dict[str, int]], *, controllable: bool = False) -> dict:
    """Map one poll's raw values to the core's three state axes, a position and scalar attributes."""
    axes = spec.axes
    moving = any(not values[a]["SRH"] & tp.SRH_MOTION_COMPLETE_BIT for a in axes)
    homed = all(values[a].get("IINITONCEDONE") == tp.INIT_DONE_MARKER for a in axes if a in CLOSED_LOOP)
    in_mm = all(a in tp.COUNTS_PER_MM for a in axes)
    attrs: dict = {"axes": axes, "counts": tuple(values[a]["APOS"] for a in axes),
                   "commanded_counts": tuple(values[a]["CPOS"] for a in axes),
                   "drive_state": tuple(values[a]["ESTATEV"] for a in axes),
                   "status_high": tuple(values[a]["SRH"] for a in axes), "read_only": not controllable}
    if any(a in CLOSED_LOOP for a in axes):
        attrs["homed"] = homed
    if all("LPOSMIN" in values[a] for a in axes):
        lim = tuple((values[a]["LPOSMIN"], values[a]["LPOSMAX"]) for a in axes)
        attrs["limits_counts"] = lim
        if in_mm:
            attrs["limits_mm"] = tuple((round(lo / tp.COUNTS_PER_MM[a], 3), round(hi / tp.COUNTS_PER_MM[a], 3))
                                       for a, (lo, hi) in zip(axes, lim, strict=True))
    if axes == ("O",) and "ICURRENTPOSHF" in values["O"]:
        # the turret's position is its 5-position sensor; its APOS stays 0 while it turns (Gate 19)
        p = values["O"]["ICURRENTPOSHF"]
        attrs["objective_position"] = p
        position = Position(axes=("o",), values=(float(p),), unit="position", frame="turret")
    else:
        position = Position(axes=tuple(a.lower() for a in axes),
                            values=tuple(round(um_from_counts(a, values[a]["APOS"]) if a in tp.COUNTS_PER_MM
                                               else float(values[a]["APOS"]), 3) for a in axes),
                            unit="um" if in_mm else "counts", frame=spec.frame)
    return {"connection": Connection.CONNECTED, "health": Health.NORMAL,
            "activity": Activity.MOVING if moving else Activity.IDLE,
            "firmware_mode": FirmwareMode.APPLICATION, "position": position, "attributes": attrs,
            "reason": "telemetry"}


def create_telemetry(options: dict) -> TelemetryService:
    """TELEMETRY factory. options: {"port": "COM3" | "auto" | "sim", "poll_interval_s": float,
    "motion": bool (ADR-0024: X/Y stage control; default False = read-only),
    "monitor_dir": folder for the drift monitor's latch and evidence (ADR-0040; None = memory only),
    "stage_log": bool (also record every fast X/Y sample to a CSV there), "fast_log": bool (default = motion),
    "power_cycled": evidence str when the launch found the drives freshly powered up and homed (clears a latch carried
    over from an earlier session, StageMonitor.power_cycle_seen)}."""
    port = str(options.get("port", "auto"))
    interval = float(options.get("poll_interval_s", POLL_INTERVAL_S))
    motion = bool(options.get("motion", False))
    box, why = (calibrated_travel_box(options["calibration"]) if options.get("calibration") else (None, ""))
    monitor = StageMonitor(options.get("monitor_dir"), record=bool(options.get("stage_log")))
    if options.get("power_cycled") and port.lower() != "sim":
        monitor.power_cycle_seen(str(options["power_cycled"]))
    kw = {"poll_interval_s": interval, "motion": motion, "travel_box_um": box, "monitor": monitor,
          "fast_log": bool(options.get("fast_log", motion))}
    locked = [str(a).upper() for a in options.get("locked_axes") or ()]
    if port.lower() == "sim":
        chain = SimulatedDriveChain(allow_motion=motion)
        if options.get("sim_realtime"):  # the app's demo stage travels at the drives' ~8.3 mm/s
            chain.speed_counts_s = SPEED_MM_S * tp.COUNTS_PER_MM["X"]
            if box:  # demo: behave as if Gate 7 had loaded the calibrated limits into the drives
                for ax in ("X", "Y"):
                    lo, hi = box[ax.lower()]
                    chain.values[(ax, "LPOSMIN")] = counts_from_um(ax, lo)
                    chain.values[(ax, "LPOSMAX")] = counts_from_um(ax, hi) + round(2.0 * tp.COUNTS_PER_MM[ax])
        svc = TelemetryService(lambda: chain, "SIM", **kw)
        svc.simulated_chain = chain  # type: ignore[attr-defined]
        svc.emulated = True
    elif port.lower() == "auto":
        from openevos.backends.technosoft.transport import ftdi_com_ports

        found = ftdi_com_ports()
        if len(found) != 1:
            def _none() -> Port:
                raise TransportError(f"expected exactly one FTDI 0403:6001 port, found {len(found)}")
            svc = TelemetryService(_none, "auto", **kw)
        else:
            com = found[0]
            svc = TelemetryService(lambda: WinSerialPort(com, allow_motion=motion), com, **kw)
    else:
        svc = TelemetryService(lambda: WinSerialPort(port, allow_motion=motion), port, **kw)
    svc.travel_box_note = why or ("calibrated envelope" if box else "power-up box 0-22 mm")  # type: ignore[attr-defined]
    svc.speed_control = bool(options.get("speed_control")) and motion
    # ADR-0053: Z focus and the objective turret from the app, with motor control (set before the devices register,
    # so their capability records offer the commands)
    svc.zo_control = motion and bool(options.get("focus_turret", True))
    # ADR-0056: with motor control, reload the calibrated limits after power-up homing (e.g. a power cycle mid-session)
    if motion and options.get("calibration") and options.get("limits_reload", True):
        lim, _why = calibrated_limits_counts(options["calibration"])
        svc.calibrated_limits = lim
    for ax in locked:  # operator lock for this session (e.g. Y while its hardware fault is investigated)
        if ax in tp.MOTION_AXES:
            svc._drift_locked[ax] = "locked by the operator for this session (--lock-axis)"
    return svc
