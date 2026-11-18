"""CoreGateway: the controller-facing command port, backed by the command core.

    UI / MicroscopeController / AcquisitionEngine
          │  execute("xy.move_relative", dx_um=100)          read(Role.XY_STAGE).get_position_um()
          ▼
    CoreGateway  ── parse parameters (names, types, units) ──▶ typed Command (core.models)
          │
          ▼
    CommandRouter ── STOP latch · SafetyManager (state, firmware, limits, confirmation,
          │          interlocks on observed state) · atomic resource reservation
          ▼
    DeviceWorker ──▶ HalDeviceBackend ──▶ simulated HAL device ──▶ readback-verified completion

It keeps the call surface the controller and UI were written against (execute / read / stop_all
/ exclusive / audit), so the switch to the core needed no change in the pages. It decides
nothing about safety itself: every state change is admitted or refused by the router.

Reads (`read(role)`) go straight to the device's read-only methods, as before.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from openevos.core.application import CoreServices
from openevos.core.audit import AuditEvent, AuditTrail, utc_now_iso
from openevos.core.command_language import (
    AVAILABLE_MODES,
    DEFAULT_LIMITS,
    Actor,
    CommandAborted,
    CommandFailed,
    CommandRejected,
    CommandSpec,
    CommandTimeout,
    ModeNotAvailable,
    OperationalMode,
    PolicyError,
    build_catalog,
    coerce_param,
)
from openevos.core.event_bus import CommandLifecycleEvent
from openevos.core.models import (
    AllIlluminationOff,
    CommandOrigin,
    CommandStatus,
    HomeFocus,
    HomeStage,
    MoveFocusAbsolute,
    MoveStageAbsolute,
    SelectCarriagePosition,
    SetCameraExposure,
    SetCameraGain,
    SetCameraLive,
    SetIllumination,
    SetIncubatorControlEnabled,
    SetIncubatorSetpoint,
    SetTurretPosition,
    SnapImage,
)
from openevos.core.models import (
    DeviceSnapshot as CoreSnapshot,
)
from openevos.hal.adapter import FOCUS_FRAME, STAGE_FRAME, UNIT
from openevos.hal.device_registry import DeviceRegistry
from openevos.hal.interfaces import HardwareDevice, read_methods_for
from openevos.hal.types import DeviceIdentity, DeviceStatus, Role
from openevos.safety.firmware import operation_refusal
from openevos.safety.interlocks import ResourceClaim, resource

log = logging.getLogger(__name__)
S = CommandStatus


class ReadOnlyView:
    """Exposes only the read methods of a device (see `interfaces.READ_METHODS`)."""

    __slots__ = ("_allowed", "_device", "_role")

    def __init__(self, role: Role, device: HardwareDevice):
        object.__setattr__(self, "_device", device)
        object.__setattr__(self, "_allowed", frozenset(read_methods_for(device)))
        object.__setattr__(self, "_role", role)

    def __getattr__(self, name: str) -> Any:
        if name in self._allowed:
            return getattr(self._device, name)
        raise AttributeError(f"'{name}' is not a read-only operation of {self._role.value}; "
                             "state changes go through the command core")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("read-only view")

    def __dir__(self) -> list[str]:
        return sorted(self._allowed)


@dataclass(frozen=True)
class CommandResult:
    command: str
    role: Role
    value: Any
    args: dict[str, Any]
    duration_ms: float
    completed: bool
    audit_seq: int


@dataclass(frozen=True)
class StopReport:
    halted: list[str]
    errors: dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0.0


@dataclass(frozen=True)
class DeviceSnapshot:
    """Identity + HAL status of one device, for display (authoritative state: `core_snapshot()`)."""

    role: Role
    identity: DeviceIdentity
    status: DeviceStatus

ORIGIN = {Actor.OPERATOR: CommandOrigin.UI, Actor.ACQUISITION: CommandOrigin.ACQUISITION,
          Actor.SYSTEM: CommandOrigin.SYSTEM}
#: Router rejection check -> the check name the controller/UI already understand.
CHECK_NAMES = {"reserved": "exclusive", "resource": "busy"}

# Commands whose device must finish before execute() returns even without wait=True: they are
# quick, and a device error must reach the caller as an exception (as with the old gateway).
_MOTION = {"xy.move_absolute", "xy.move_relative", "xy.home", "focus.move_absolute",
           "focus.move_relative", "focus.home", "objective.select", "phase.select", "carriage.select"}


def _role_or_none(device_id: str) -> Role | None:
    try:
        return Role(device_id)
    except ValueError:
        return None


class PolicyView:
    """What the UI reads from 'the policy': mode, limits, latch, reservation owner."""

    def __init__(self, gw: CoreGateway, mode: OperationalMode, limits: dict[str, float]):
        self._gw, self.mode, self.limits = gw, mode, limits

    @property
    def stop_latched(self) -> bool:
        return self._gw.core.router.stop_latched

    @property
    def stop_reason(self) -> str:
        return self._gw.core.router.stop_reason

    @property
    def exclusive_owner(self) -> str | None:
        return self._gw._exclusive_owner

    def set_mode(self, mode: OperationalMode) -> None:
        mode = OperationalMode(mode)
        if mode not in AVAILABLE_MODES:
            raise ModeNotAvailable(f"{mode.display} mode needs a validated real backend; not available in this build")
        self.mode = mode


@dataclass(frozen=True)
class _Pending:
    name: str
    actor: Actor
    owner: str | None
    params: dict[str, Any]
    t0: float
    confirmed: bool


class CoreGateway:
    def __init__(self, core: CoreServices, registry: DeviceRegistry, *,
                 mode: OperationalMode = OperationalMode.SIMULATION,
                 limits: dict[str, float] | None = None, audit: AuditTrail | None = None,
                 config_ref: str = "", wait_timeout_s: float | None = None):
        self.core, self.registry = core, registry
        lim = dict(DEFAULT_LIMITS)
        lim.update(limits or {})
        self.policy = PolicyView(self, mode, lim)
        self.audit = audit if audit is not None else AuditTrail()
        self.catalog: dict[str, CommandSpec] = build_catalog()  # parameter language + reference text
        self.config_ref = config_ref
        self._wait_extra = wait_timeout_s
        self._exclusive_owner: str | None = None
        self._stop_generation = 0
        self._stop_listeners: list[Callable[[str], None]] = []
        self._async: dict[str, _Pending] = {}
        self._async_lock = threading.Lock()
        #: ADR-0024: set by build_instrument only when a telemetry source runs with motion enabled
        self.motor_control_device: str | None = None
        core.bus.subscribe(CommandLifecycleEvent, self._on_lifecycle)

    # ------------------------------------------------------------------------------ reads
    @property
    def mode(self) -> OperationalMode:
        return self.policy.mode

    @property
    def stop_latched(self) -> bool:
        return self.core.router.stop_latched

    @property
    def stop_generation(self) -> int:
        return self._stop_generation

    def roles(self) -> list[Role]:
        return self.registry.roles()

    def has(self, role: Role) -> bool:
        return role in self.registry

    def read(self, role: Role) -> ReadOnlyView:
        return ReadOnlyView(Role(role), self.registry.require(role))

    def identity(self, role: Role) -> DeviceIdentity:
        return self.registry.require(role).identify()

    def status(self, role: Role) -> DeviceStatus:
        return self.registry.require(role).get_status()

    def snapshot(self) -> list[DeviceSnapshot]:
        return [DeviceSnapshot(role, dev.identify(), dev.get_status()) for role, dev in self.registry.items()]

    def core_snapshot(self, refresh: bool = True) -> dict[str, CoreSnapshot]:
        """Authoritative runtime state (connection / health / activity / firmware mode / recovery)."""
        if refresh:
            for device_id in self.core.devices.device_ids():
                rec = self.core.devices.get(device_id)
                observe = getattr(rec.backend, "observe", None) if rec else None
                if observe is not None:
                    try:
                        observe(reason="refresh")
                    except Exception:
                        log.exception("refresh of %s failed", device_id)
        return self.core.store.snapshot_all()

    def command_specs(self) -> list[CommandSpec]:
        return list(self.catalog.values())

    def recover(self, role: Role, *, actor: Actor = Actor.OPERATOR) -> None:
        """Explicit operator recovery after a timeout or readback mismatch."""
        self.core.router.recover_device(Role(role).value, reason=f"{Actor(actor).value} recovery")
        self.note("device.recover", actor=actor, details={"role": Role(role).value})

    # ------------------------------------------------------------------------------ commands
    def execute(self, command: str, *, actor: Actor = Actor.OPERATOR, owner: str | None = None,
                wait: bool = False, confirmed: bool = False, **params: Any) -> CommandResult:
        t0 = time.monotonic()
        actor = Actor(actor)
        pending = _Pending(command, actor, owner, dict(params), t0, confirmed)
        refusal = operation_refusal(command)  # ADR-0009, before anything else
        if refusal:
            raise self._reject(pending, None, "firmware", refusal)
        spec = self.catalog.get(command)
        if spec is None:
            raise self._reject(pending, None, "catalog", "unknown command")
        if actor is Actor.AI:
            raise self._reject(pending, spec, "actor", "AI may not issue device commands (ADR-0003, plan §13.3)")
        if self.mode is not OperationalMode.SIMULATION:
            raise self._reject(pending, spec, "mode", f"state-changing commands are not permitted in {self.mode.display} mode")  # noqa: E501
        role, inputs = self._parse(pending, spec)

        if command in ("xy.stop", "focus.stop"):
            self.core.router.stop_device(role.value)
            seq = self._record(pending, spec, role, "allowed", "ok", "", inputs)
            return CommandResult(command, role, None, inputs, (time.monotonic() - t0) * 1000, True, seq)

        self._refresh(role.value)  # validate against what the device reports now, via the store
        work = dict(inputs)
        typed = self._build(command, role, work, spec, actor, owner, confirmed, pending)
        resolved = {k: v for k, v in work.items() if k not in inputs}
        args = resolved or inputs  # the device arguments (relative moves: the absolute target)
        audit_params: dict[str, Any] = dict(inputs) | ({"resolved": resolved} if resolved else {})
        ticket = self.core.router.submit(typed)
        if ticket.status is S.REJECTED:
            rec = ticket.record
            check = CHECK_NAMES.get(rec.check or "", rec.check or "safety")
            raise self._reject(pending, spec, check, rec.message or "rejected", audit_params)
        args_for_audit = audit_params

        must_finish = wait or command not in _MOTION
        limit = typed.timeout_s + (self._wait_extra if self._wait_extra is not None else 5.0)
        status = ticket.wait(limit) if must_finish else ticket.wait_for({S.RUNNING}, limit)
        if not status.is_terminal and must_finish:
            ticket.cancel()
            status = ticket.wait(1.0)
        if not must_finish and status is S.RUNNING:
            with self._async_lock:
                self._async[typed.command_id] = pending
            if ticket.status.is_terminal:  # finished before we registered: settle it now
                self._settle_async(typed.command_id, ticket.status, ticket.record.message)
            seq = self._record(pending, spec, role, "allowed", "ok", "started", args_for_audit)
            return CommandResult(command, role, None, args, (time.monotonic() - t0) * 1000, False, seq)
        message = ticket.record.message or ""
        if status is S.SUCCEEDED:
            seq = self._record(pending, spec, role, "allowed", "ok", "", args_for_audit)
            return CommandResult(command, role, ticket.payload, args, (time.monotonic() - t0) * 1000, True, seq)
        if status is S.CANCELLED:
            self._record(pending, spec, role, "allowed", "aborted", message or "cancelled", args_for_audit)
            raise CommandAborted(command, "STOP was pressed" if self.stop_latched else message or "cancelled")
        if status is S.TIMED_OUT:
            self._record(pending, spec, role, "allowed", "timeout", message, args_for_audit)
            raise CommandTimeout(command, message)
        self._record(pending, spec, role, "allowed", "failed", message, args_for_audit)
        raise CommandFailed(command, message or f"ended {status.value}")

    # ------------------------------------------------------------------------------ physical stage (ADR-0024)
    #: the only step sizes the operator can jog the physical stage by, in um
    MOTOR_JOG_STEPS_UM = (100.0, 1000.0, 5000.0)

    def motor_jog(self, axis: str, step_um: float, *, actor: Actor = Actor.OPERATOR) -> CommandResult:
        """Jog the physical X/Y stage one fixed step on one axis. Resolved from the store's observed
        position; validated by the SafetyManager (verified capabilities, travel box, idle, homed, firmware,
        STOP latch) and executed by the Technosoft backend. Returns once the move has started."""
        t0 = time.monotonic()
        pending = _Pending("motor.stage.jog", Actor(actor), None, {"axis": axis, "step_um": step_um}, t0, False)
        if pending.actor is Actor.AI:
            raise self._reject(pending, None, "actor", "AI may not issue device commands (ADR-0003, plan §13.3)")
        device = self.motor_control_device
        if device is None:
            raise self._reject(pending, None, "mode", "physical stage control is not enabled in this session")
        if axis not in ("x", "y"):
            raise self._reject(pending, None, "params", "axis must be 'x' or 'y'")
        if abs(step_um) not in self.MOTOR_JOG_STEPS_UM:
            raise self._reject(pending, None, "params", f"step must be ±one of {self.MOTOR_JOG_STEPS_UM} um")
        snap = self.core.store.get(device)
        pos = snap.observed_position if snap else None
        if pos is None or pos.unit != "um" or not {"x", "y"} <= set(pos.axes):
            raise self._reject(pending, None, "params", "stage position is unknown; cannot resolve a jog")
        target = {a: pos.get(a) for a in ("x", "y")}
        target[axis] += step_um
        audit = {"axis": axis, "step_um": step_um, "device": device, "resolved": {k: round(v, 3) for k, v in target.items()}}  # noqa: E501
        cmd = MoveStageAbsolute(device_id=device, origin=ORIGIN[pending.actor], owner=None, confirmed=False,
                                timeout_s=15.0, x=target["x"], y=target["y"], unit=UNIT, frame=STAGE_FRAME)
        ticket = self.core.router.submit(cmd)
        if ticket.status is S.REJECTED:
            rec = ticket.record
            raise self._reject(pending, None, CHECK_NAMES.get(rec.check or "", rec.check or "safety"),
                               rec.message or "rejected", audit)
        status = ticket.wait_for({S.RUNNING}, 5.0)
        if status is S.RUNNING or not status.is_terminal:
            with self._async_lock:
                self._async[cmd.command_id] = pending
            if ticket.status.is_terminal:
                self._settle_async(cmd.command_id, ticket.status, ticket.record.message)
            seq = self._record(pending, None, None, "allowed", "ok", "started", audit)
            return CommandResult(pending.name, None, None, audit, (time.monotonic() - t0) * 1000, False, seq)
        if status is S.SUCCEEDED:
            seq = self._record(pending, None, None, "allowed", "ok", "", audit)
            return CommandResult(pending.name, None, ticket.payload, audit, (time.monotonic() - t0) * 1000, True, seq)
        message = ticket.record.message or status.value
        self._record(pending, None, None, "allowed", "failed", message, audit)
        raise CommandFailed(pending.name, message)

    def motor_stop(self, *, actor: Actor = Actor.OPERATOR) -> None:
        """Stop the physical stage: cancel its commands and run the drive's controlled stop on X and Y."""
        pending = _Pending("motor.stage.stop", Actor(actor), None, {}, time.monotonic(), False)
        if pending.actor is Actor.AI:
            raise self._reject(pending, None, "actor", "AI may not stop or start instrument activity (plan §13.3)")
        device = self.motor_control_device
        if device is None:
            raise self._reject(pending, None, "mode", "physical stage control is not enabled in this session")
        self.core.router.stop_device(device)
        self._record(pending, None, None, "allowed", "ok", "", {"device": device}, checks=["local_stop"])

    def _refresh(self, device_id: str) -> None:
        rec = self.core.devices.get(device_id)
        observe = getattr(rec.backend, "observe", None) if rec else None
        if observe is not None:
            try:
                observe(reason="pre-admission refresh")
            except Exception:
                log.exception("refresh of %s failed", device_id)

    def _parse(self, p: _Pending, spec: CommandSpec) -> tuple[Role, dict[str, Any]]:
        raw = p.params
        known = {x.name for x in spec.params} | ({spec.role_param} if spec.role_param else set())
        unknown = sorted(set(raw) - known)
        if unknown:
            raise self._reject(p, spec, "params", f"unknown parameter(s): {', '.join(unknown)}")
        if spec.role is not None:
            role = spec.role
        else:
            try:
                role = Role(raw.get(spec.role_param or ""))
            except ValueError:
                raise self._reject(p, spec, "params", f"'{spec.role_param}' must name a device role, "
                                   f"got {raw.get(spec.role_param or '')!r}") from None
        if role not in self.registry:
            raise self._reject(p, spec, "device", f"no device for role '{role.value}'")
        args: dict[str, Any] = {}
        for ps in spec.params:
            if ps.name not in raw:
                if ps.required:
                    raise self._reject(p, spec, "params", f"missing parameter '{ps.name}'")
                args[ps.name] = ps.default
                continue
            try:
                v = coerce_param(ps, raw[ps.name])
            except (TypeError, ValueError) as exc:
                raise self._reject(p, spec, "params", f"{ps.name}: {exc}") from None
            # static bounds only (e.g. the largest jog step); device ranges are the core's job
            if (ps.minimum is not None and v < ps.minimum) or (ps.maximum is not None and v > ps.maximum):
                raise self._reject(p, spec, "params", f"{ps.name}={v:g} is outside [{ps.minimum:g}, {ps.maximum:g}]")
            args[ps.name] = v
        return role, args

    def _build(self, name: str, role: Role, a: dict[str, Any], spec: CommandSpec, actor: Actor,
               owner: str | None, confirmed: bool, p: _Pending):
        kw = {"device_id": role.value, "origin": ORIGIN[actor], "owner": owner, "confirmed": confirmed,
              "timeout_s": spec.timeout_s}
        if name in ("xy.move_absolute", "xy.move_relative"):
            if name == "xy.move_relative":
                x, y = self._observed(p, spec, role, ("x", "y"))
                a.update(x_um=x + a["dx_um"], y_um=y + a["dy_um"])
            return MoveStageAbsolute(x=a["x_um"], y=a["y_um"], unit=UNIT, frame=STAGE_FRAME, **kw)
        if name in ("focus.move_absolute", "focus.move_relative"):
            if name == "focus.move_relative":
                (z,) = self._observed(p, spec, role, ("z",))
                a.update(z_um=z + a["dz_um"])
            return MoveFocusAbsolute(z=a["z_um"], unit=UNIT, frame=FOCUS_FRAME, **kw)
        builders: dict[str, Callable[[], Any]] = {
            "xy.home": lambda: HomeStage(**kw),
            "focus.home": lambda: HomeFocus(**kw),
            "objective.select": lambda: SetTurretPosition(position=a["position"], **kw),
            "phase.select": lambda: SetTurretPosition(position=a["position"], **kw),
            "carriage.select": lambda: SelectCarriagePosition(position=a["position"], **kw),
            "led.set": lambda: SetIllumination(channel=a["channel_id"], enabled=a["enabled"],
                                               intensity_percent=a["intensity_pct"], **kw),
            "led.all_off": lambda: AllIlluminationOff(**kw),
            "camera.set_exposure": lambda: SetCameraExposure(exposure_ms=a["exposure_ms"], **kw),
            "camera.set_gain": lambda: SetCameraGain(gain_db=a["gain_db"], **kw),
            "camera.snap": lambda: SnapImage(**kw),
            "camera.start_live": lambda: SetCameraLive(live=True, **kw),
            "camera.stop_live": lambda: SetCameraLive(live=False, **kw),
            "incubator.set_temperature": lambda: SetIncubatorSetpoint(control="temperature", value=a["temperature_c"], **kw),  # noqa: E501
            "incubator.set_co2": lambda: SetIncubatorSetpoint(control="co2", value=a["co2_pct"], **kw),
            "incubator.set_o2": lambda: SetIncubatorSetpoint(control="o2", value=a["o2_pct"], **kw),
            "incubator.set_enabled": lambda: SetIncubatorControlEnabled(control=a["control"], enabled=a["enabled"], **kw),  # noqa: E501
        }
        build = builders.get(name)
        if build is None:
            raise self._reject(p, spec, "catalog", "no core command for this operation")
        try:
            return build()
        except ValueError as exc:  # pydantic: non-finite numbers, bad types
            raise self._reject(p, spec, "params", str(exc)) from None

    def _observed(self, p: _Pending, spec: CommandSpec, role: Role, axes: tuple[str, ...]) -> tuple[float, ...]:
        """Relative moves resolve against the store's observed position, never a fresh device read."""
        snap = self.core.store.get(role.value)
        pos = snap.observed_position if snap else None
        if pos is None or any(ax not in pos.axes for ax in axes):
            raise self._reject(p, spec, "params", "current position is unknown; cannot resolve a relative move")
        return tuple(pos.get(ax) for ax in axes)

    def _on_lifecycle(self, ev: CommandLifecycleEvent) -> None:
        if ev.status.is_terminal:
            self._settle_async(ev.command_id, ev.status, ev.message)

    def _settle_async(self, command_id: str, status: CommandStatus, message: str | None) -> None:
        with self._async_lock:
            p = self._async.pop(command_id, None)
        if p is None or status is S.SUCCEEDED:
            return
        outcome = {S.CANCELLED: "aborted", S.TIMED_OUT: "timeout"}.get(status, "failed")
        spec = self.catalog.get(p.name)
        role = spec.role if spec else None
        self._record(p, spec, role, "allowed", outcome, f"on completion: {message or status.value}", p.params)

    # ------------------------------------------------------------------------------ STOP
    def add_stop_listener(self, cb: Callable[[str], None]) -> None:
        self._stop_listeners.append(cb)

    def remove_stop_listener(self, cb: Callable[[str], None]) -> None:
        if cb in self._stop_listeners:
            self._stop_listeners.remove(cb)

    def stop_all(self, *, actor: Actor = Actor.OPERATOR, reason: str = "operator STOP") -> StopReport:
        t0 = time.monotonic()
        p = _Pending("system.stop_all", Actor(actor), None, {"reason": reason}, t0, False)
        if p.actor is Actor.AI:
            raise self._reject(p, None, "actor", "AI may not stop or start instrument activity (plan §13.3)")
        self._stop_generation += 1
        for cb in list(self._stop_listeners):  # long tasks first, so they stop issuing commands
            try:
                cb(reason)
            except Exception:
                log.exception("stop listener failed")
        halted, errors = self.core.router.stop_all(reason)
        self._record(p, None, None, "allowed", "ok" if not errors else "failed",
                     "; ".join(f"{k}: {v}" for k, v in errors.items()), {"reason": reason, "halted": halted},
                     checks=["local_stop"])
        return StopReport(halted=halted, errors=errors, duration_ms=(time.monotonic() - t0) * 1000)

    def clear_stop(self, *, actor: Actor = Actor.OPERATOR) -> None:
        p = _Pending("system.clear_stop", Actor(actor), None, {}, time.monotonic(), False)
        if p.actor is Actor.AI:
            raise self._reject(p, None, "actor", "AI may not clear a STOP (plan §13.3)")
        self.core.router.clear_stop()
        self._record(p, None, None, "allowed", "ok", "", {}, checks=["operator"])

    # ------------------------------------------------------------------------------ reservation
    @contextmanager
    def exclusive(self, owner: str) -> Iterator[str]:
        """Reserve every instrument device except the incubator for `owner` (an acquisition).
        Commands carrying `owner=` run under it; anyone else's are rejected (check "exclusive")."""
        ids = [r.value for r in self.registry.roles() if r is not Role.INCUBATOR]
        conflicts = self.core.router.reserve(owner, ResourceClaim(exclusive=frozenset(resource(i) for i in ids)))
        if conflicts:
            raise PolicyError(f"cannot reserve the instrument for {owner}: {'; '.join(conflicts)}")
        self._exclusive_owner = owner
        try:
            yield owner
        finally:
            self._exclusive_owner = None
            self.core.router.release(owner)

    # ------------------------------------------------------------------------------ lifecycle
    def connect_all(self, *, actor: Actor = Actor.SYSTEM) -> dict[str, str]:
        out: dict[str, str] = {}
        for device_id in self.core.devices.device_ids():
            t0 = time.monotonic()
            p = _Pending("device.connect", Actor(actor), None, {}, t0, False)
            role = _role_or_none(device_id)
            if self.mode is OperationalMode.INVENTORY:
                self._record(p, None, role, "rejected", "rejected", "Inventory mode does not connect devices", {},
                             checks=["mode"])
                out[device_id] = "rejected"
                continue
            try:
                self.core.devices.connect(device_id)
                self._record(p, None, role, "allowed", "ok", "", {}, checks=["mode"])
                out[device_id] = "ok"
            except Exception as exc:  # noqa: BLE001
                self._record(p, None, role, "allowed", "failed", str(exc), {}, checks=["mode"])
                out[device_id] = f"failed: {exc}"
        return out

    def disconnect_all(self, *, actor: Actor = Actor.SYSTEM) -> None:
        for device_id in self.core.devices.device_ids():
            p = _Pending("device.disconnect", Actor(actor), None, {}, time.monotonic(), False)
            rec = self.core.devices.get(device_id)
            try:
                if rec is not None:
                    rec.backend.disconnect()
                self._record(p, None, _role_or_none(device_id), "allowed", "ok", "", {"device_id": device_id})
            except Exception as exc:  # noqa: BLE001
                self._record(p, None, _role_or_none(device_id), "allowed", "failed", str(exc), {"device_id": device_id})

    def note(self, event: str, *, actor: Actor = Actor.SYSTEM, details: dict[str, Any] | None = None,
             reason: str = "") -> int:
        p = _Pending(event, Actor(actor), None, dict(details or {}), time.monotonic(), False)
        return self._record(p, None, None, "allowed", "ok", reason, dict(details or {}), state_changing=False)

    # ------------------------------------------------------------------------------ audit
    def _reject(self, p: _Pending, spec: CommandSpec | None, check: str, reason: str,
                args: dict[str, Any] | None = None) -> CommandRejected:
        role = spec.role if spec is not None else None
        self._record(p, spec, role, "rejected", "rejected", reason, args if args is not None else p.params,
                     checks=[check])
        return CommandRejected(p.name, reason, check)

    def _record(self, p: _Pending, spec: CommandSpec | None, role: Role | None, decision: str, outcome: str,
                reason: str, params: dict[str, Any], *, checks: list[str] | None = None,
                state_changing: bool = True) -> int:
        dev = self.registry.get(role) if role is not None else None
        ident = dev.identify() if dev is not None else None
        clean = {k: (v if isinstance(v, (int, float, str, bool, list, dict)) or v is None else repr(v)[:80])
                 for k, v in params.items()}
        seq = self.audit.next_seq()
        self.audit.record(AuditEvent(
            seq=seq, timestamp=utc_now_iso(), monotonic_s=time.monotonic(),
            duration_ms=round((time.monotonic() - p.t0) * 1000.0, 3), actor=p.actor.value, owner=p.owner,
            mode=self.mode.value, command=p.name, role=role.value if role is not None else None,
            device=ident.display_name if ident else None, backend=ident.backend if ident else None,
            simulated=ident.simulated if ident else None, params=clean, state_changing=state_changing,
            risk=spec.risk.value if spec else None, decision=decision, outcome=outcome, reason=reason,
            checks=list(checks or (["catalog", "actor", "mode", "params", "core"]
                                   if decision == "allowed" and spec else [])),
            confirmed=p.confirmed, config_ref=self.config_ref))
        return seq
