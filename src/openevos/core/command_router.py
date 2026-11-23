"""Command intake and lifecycle (handoff §16.1–16.4).

Lifecycle:  QUEUED → VALIDATING → ACCEPTED → RUNNING → SUCCEEDED
                         └→ REJECTED     ├→ CANCELLING → CANCELLED
                                         ├→ FAILED
                                         └→ TIMED_OUT (reservation kept; device needs recovery)

Guarantees:
* Validation and resource reservation happen atomically under one admission lock, so two
  commands can never both pass against the same idle snapshot.
* RUNNING is emitted only when the backend confirms the operation started (`mark_started`).
* SUCCEEDED for motion requires a readback within the capability's tolerance. A mismatch fails
  the command and marks the device recovery-required.
* Asynchronous backend failures reach FAILED through the execution context.
* Terminal states are sticky: late, duplicate or out-of-order backend callbacks are ignored and
  counted in `stale_backend_events`.
* Rejected commands never reach a backend.

Thread rules: no core lock is held while calling into a backend. Command records are immutable
and replaced atomically, so reading a ticket's status never blocks.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from openevos.core.clock import Clock, TimerHandle
from openevos.core.command_scheduler import ResourceTable
from openevos.core.device_manager import DeviceManager, DeviceRecord
from openevos.core.event_bus import CommandLifecycleEvent, EventBus, HardwareFaultEvent, Severity
from openevos.core.models import (
    ALLOWED_TRANSITIONS,
    MOTION_COMMANDS,
    Command,
    CommandRecord,
    CommandStatus,
    Position,
    is_safe_direction,
)
from openevos.core.runtime_state_store import RuntimeStateStore
from openevos.safety.command_validator import SafetyManager
from openevos.safety.interlocks import ResourceClaim, claim_for_command

log = logging.getLogger(__name__)
S = CommandStatus


class _Entry:
    def __init__(self, command: Command):
        self.command = command
        self.cv = threading.Condition(threading.RLock())
        self.record = CommandRecord(command=command, status=S.QUEUED)
        self.history: list[CommandStatus] = [S.QUEUED]
        self.cancel_requested = False
        self.dispatched = False
        self.reserved = False
        self.timeout: TimerHandle | None = None
        self.payload: object | None = None

    @property
    def status(self) -> CommandStatus:
        return self.record.status


class CommandTicket:
    """Caller-facing handle: observe and cancel. It cannot drive the lifecycle."""

    def __init__(self, router: CommandRouter, entry: _Entry):
        self._router, self._entry = router, entry
        self.command_id = entry.command.command_id
        self.command = entry.command

    @property
    def status(self) -> CommandStatus:
        return self._entry.status

    @property
    def record(self) -> CommandRecord:
        return self._entry.record

    @property
    def history(self) -> tuple[CommandStatus, ...]:
        return tuple(self._entry.history)

    @property
    def payload(self) -> object | None:
        """Non-position result handed over on completion (e.g. a snapped Frame)."""
        return self._entry.payload

    def wait(self, timeout: float | None = None) -> CommandStatus:
        """Block (not on the UI thread!) until a terminal state or timeout; returns the status."""
        return self.wait_for(TERMINAL, timeout)

    def wait_for(self, statuses: Iterable[CommandStatus], timeout: float | None = None) -> CommandStatus:
        wanted = set(statuses)
        with self._entry.cv:
            self._entry.cv.wait_for(lambda: self._entry.status in wanted or self._entry.status.is_terminal, timeout)
            return self._entry.status

    def cancel(self) -> bool:
        return self._router._cancel(self._entry)


TERMINAL = frozenset(s for s in CommandStatus if s.is_terminal)


class _Context:
    """Backend-facing handle for one accepted command (implements hal.backend.ExecutionContext)."""

    def __init__(self, router: CommandRouter, entry: _Entry, rec: DeviceRecord):
        self._router, self._entry, self._rec = router, entry, rec
        self.command_id = entry.command.command_id

    @property
    def cancel_requested(self) -> bool:
        return self._entry.cancel_requested

    def mark_started(self) -> None:
        self._router._transition(self._entry, S.RUNNING, "backend confirmed start", from_backend=True)

    def report_progress(self, fraction: float, message: str | None = None) -> None:
        if self._entry.status is S.RUNNING:
            self._router.bus.publish(CommandLifecycleEvent(
                self.command_id, self._entry.command.device_id, S.RUNNING, S.RUNNING,
                message or f"progress {max(0.0, min(1.0, fraction)):.0%}",
                correlation_id=self._entry.command.correlation_id))

    def complete(self, result: Position | None = None, payload: object | None = None) -> None:
        if payload is not None and not self._entry.status.is_terminal:
            self._entry.payload = payload
        self._router._on_complete(self._entry, self._rec, result)

    def fail(self, message: str, error_code: str | None = None) -> None:
        self._router._on_fail(self._entry, message, error_code)

    def mark_cancelled(self, result: Position | None = None) -> None:
        self._router._transition(self._entry, S.CANCELLED, "backend confirmed cancellation", result=result,
                                 from_backend=True)


class CommandRouter:
    def __init__(self, devices: DeviceManager, safety: SafetyManager, resources: ResourceTable,
                 bus: EventBus, store: RuntimeStateStore, clock: Clock):
        self.devices, self.safety, self.resources = devices, safety, resources
        self.bus, self.store, self.clock = bus, store, clock
        self._admission = threading.Lock()
        self._entries_lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._uncertain: dict[str, list[str]] = {}  # device_id -> timed-out command IDs holding resources
        self._long: set[str] = set()  # owners of long-lived reservations (acquisitions)
        self._stop_latched = False
        self._stop_reason = ""
        self.stale_backend_events = 0

    # ================================================================== public API
    def submit(self, command: Command) -> CommandTicket:
        entry = _Entry(command)
        with self._entries_lock:
            if command.command_id in self._entries:
                raise ValueError(f"duplicate command_id {command.command_id}")
            self._entries[command.command_id] = entry
        ticket = CommandTicket(self, entry)
        self._publish(entry, None)  # QUEUED

        with self._admission:
            self._transition(entry, S.VALIDATING)
            safe = is_safe_direction(command)
            if self._stop_latched and not safe:
                self._transition(entry, S.REJECTED, f"STOP is engaged ({self._stop_reason}); clear it before "
                                 "issuing commands", check="stop_latch")
                return ticket
            rec = self.devices.get(command.device_id)
            snap = self.store.get(command.device_id)
            decision = self.safety.validate(command, rec.capabilities if rec else None, snap,
                                            self.store.snapshot_all())
            if not decision.allowed:
                self._transition(entry, S.REJECTED, decision.message, check=decision.first_check or "safety")
                return ticket
            if rec is None:  # the safety check refuses a device with no capability record; this keeps it so here too
                self._transition(entry, S.REJECTED, f"{command.device_id} is not registered", check="state")
                return ticket
            claim = claim_for_command(command)
            covered = command.owner is not None and command.owner in self._long and self._covers(command.owner, claim)
            if not covered:
                conflicts = self.resources.try_reserve(command.command_id, claim)
                if conflicts and safe:
                    # light off / live off must never wait for a reservation; the device's own
                    # worker still serialises it behind any running command
                    conflicts = []
                elif conflicts:
                    held_long = [o for res in claim.exclusive | claim.shared
                                 for o in self.resources.owners_of(res) if o in self._long]
                    check = "reserved" if held_long else "resource"
                    self._transition(entry, S.REJECTED, "resource conflict: " + "; ".join(conflicts), check=check)
                    return ticket
                else:
                    entry.reserved = True
            self._transition(entry, S.ACCEPTED, "validated and resources reserved" if entry.reserved
                             else "validated (runs under an existing reservation)")

        if isinstance(command, MOTION_COMMANDS):
            self.store.set_requested(command.device_id, command.target(), reason=f"accepted {command.command_id}")
        entry.timeout = self.clock.call_later(command.timeout_s, lambda: self._on_timeout(entry))
        self.devices.worker(command.device_id).submit(lambda: self._run(entry, rec))
        return ticket

    def get(self, command_id: str) -> CommandRecord | None:
        with self._entries_lock:
            e = self._entries.get(command_id)
        return e.record if e else None

    def reserve(self, owner: str, claim: ResourceClaim) -> list[str]:
        """Long-lived reservation (e.g. an acquisition), atomic with command admission.

        Commands that carry `owner=<owner>` run under it without reserving again; commands from
        anyone else that need those resources are rejected with check "reserved"."""
        with self._admission:
            conflicts = self.resources.try_reserve(owner, claim)
            if not conflicts:
                self._long.add(owner)
            return conflicts

    def release(self, owner: str) -> bool:
        with self._admission:
            self._long.discard(owner)
            return self.resources.release(owner)

    def long_reservations(self) -> tuple[str, ...]:
        with self._admission:
            return tuple(sorted(self._long))

    def _covers(self, owner: str, claim: ResourceClaim) -> bool:
        held = self.resources.holders().get(owner)
        return held is not None and claim.exclusive <= held.exclusive and claim.shared <= (held.exclusive | held.shared)

    # ------------------------------------------------------------------ STOP (ADR-0006)
    @property
    def stop_latched(self) -> bool:
        return self._stop_latched

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    def stop_all(self, reason: str = "operator STOP") -> tuple[list[str], dict[str, str]]:
        """Local STOP. Latch first (nothing new is admitted), cancel every unfinished command,
        then halt every device directly. Never waits for a device worker or anything remote.

        An incubator backend's `halt()` deliberately does nothing: STOP leaves environmental
        control running (ADR-0006). Returns (halted device IDs, errors by device ID)."""
        with self._admission:
            self._stop_latched, self._stop_reason = True, reason
        with self._entries_lock:
            live = [e for e in self._entries.values() if not e.status.is_terminal]
        for e in live:
            try:
                self._cancel(e)
            except Exception:
                log.exception("cancel during STOP failed")
        halted: list[str] = []
        errors: dict[str, str] = {}
        for device_id in self.devices.device_ids():
            rec = self.devices.get(device_id)
            halt = getattr(rec.backend, "halt", None) if rec else None
            if halt is None:
                continue
            try:
                halt()
                halted.append(device_id)
            except Exception as exc:  # noqa: BLE001 - keep halting the remaining devices
                errors[device_id] = f"{exc.__class__.__name__}: {exc}"
        return halted, errors

    def cancel_all(self) -> int:
        """Request cancellation of every unfinished command, without latching and without halting idle
        devices (a workflow's "cancel"). The operator STOP is `stop_all()`. Returns how many were asked."""
        with self._entries_lock:
            live = [e for e in self._entries.values() if not e.status.is_terminal]
        return sum(1 for e in live if self._cancel(e))

    def stop_device(self, device_id: str) -> None:
        """Stop one device (e.g. an XY "stop" button): cancel its unfinished commands, then halt
        it directly. Safe-direction, so it is never refused; it does not latch."""
        with self._entries_lock:
            live = [e for e in self._entries.values()
                    if e.command.device_id == device_id and not e.status.is_terminal]
        for e in live:
            self._cancel(e)
        rec = self.devices.get(device_id)
        halt = getattr(rec.backend, "halt", None) if rec else None
        if halt is not None:
            halt()

    def clear_stop(self) -> None:
        with self._admission:
            self._stop_latched, self._stop_reason = False, ""

    def recover_device(self, device_id: str, *, reason: str = "operator-initiated recovery") -> None:
        """Explicit recovery after a timeout or readback mismatch. The backend must re-establish
        observed state (stop, re-read position); only then are stale reservations released."""
        rec = self.devices.get(device_id)
        if rec is None:
            raise KeyError(device_id)
        rec.backend.recover()
        with self._admission:
            for cid in self._uncertain.pop(device_id, []):
                self.resources.release(cid)
        self.store.set_requested(device_id, None, reason=reason)
        self.store.set_recovery_required(device_id, False, reason=reason)

    # ================================================================== internals
    def _run(self, entry: _Entry, rec: DeviceRecord) -> None:
        with entry.cv:
            if entry.status is not S.ACCEPTED:
                return  # cancelled or timed out while queued
            entry.dispatched = True
        try:
            rec.backend.start(entry.command, _Context(self, entry, rec))
        except Exception as exc:  # noqa: BLE001
            self._on_fail(entry, f"backend start failed: {exc.__class__.__name__}: {exc}", "START_FAILED")

    def _cancel(self, entry: _Entry) -> bool:
        call_backend = False
        with entry.cv:
            st = entry.status
            if st.is_terminal:
                return False
            entry.cancel_requested = True
            if st is S.CANCELLING:
                return True
            if st in (S.QUEUED, S.ACCEPTED) and not entry.dispatched:
                self._transition(entry, S.CANCELLED, "cancelled before execution started")
                return True
            if st in (S.ACCEPTED, S.RUNNING):
                self._transition(entry, S.CANCELLING, "cancellation requested")
                call_backend = True
        if call_backend:
            rec = self.devices.get(entry.command.device_id)
            if rec is not None:
                try:
                    rec.backend.cancel(entry.command.command_id)
                except Exception as exc:  # noqa: BLE001
                    self._on_fail(entry, f"backend cancel failed: {exc}", "CANCEL_FAILED")
        return True

    def _on_complete(self, entry: _Entry, rec: DeviceRecord, result: Position | None) -> None:
        st = entry.status
        if st.is_terminal:
            self._stale(entry, "complete")
            return
        if st is S.ACCEPTED:
            self._on_fail(entry, "backend reported completion without confirming start", "PROTOCOL")
            return
        ok, readback, why = self._verify(entry.command, rec)
        if ok:
            self._transition(entry, S.SUCCEEDED, "completed and verified by readback" if readback else "completed",
                             result=readback or result, from_backend=True)
        else:
            if self._transition(entry, S.FAILED, f"verification failed: {why}", result=readback, from_backend=True):
                self.store.set_recovery_required(entry.command.device_id, True, reason=f"readback mismatch: {why}")
                self.bus.publish(HardwareFaultEvent(entry.command.device_id, f"readback mismatch: {why}",
                                                    Severity.ERROR, "READBACK_MISMATCH", entry.command.command_id))

    def _verify(self, command: Command, rec: DeviceRecord) -> tuple[bool, Position | None, str]:
        if not isinstance(command, MOTION_COMMANDS):
            # Non-motion commands: a backend may offer `verify_result(command) -> (ok, why)` to confirm
            # the device reached the commanded state (turret slot, light channel, exposure, gain).
            verifier = getattr(rec.backend, "verify_result", None)
            if verifier is None:
                return True, None, ""
            try:
                ok, why = verifier(command)
            except Exception as exc:  # noqa: BLE001
                return False, None, f"state verification raised {exc.__class__.__name__}: {exc}"
            return bool(ok), None, why or ""
        try:
            readback = rec.backend.read_position()
        except Exception as exc:  # noqa: BLE001
            return False, None, f"readback raised {exc.__class__.__name__}: {exc}"
        if readback is None:
            return False, None, "backend provided no readback"
        target = command.target()
        if readback.unit != target.unit or readback.frame != target.frame:
            return False, readback, f"readback in {readback.unit}/{readback.frame}, target in {target.unit}/{target.frame}"  # noqa: E501
        for axis, want in target.as_dict().items():
            ax = rec.capabilities.axis(axis)
            if ax is None or ax.readback_tolerance is None or axis not in readback.axes:
                return False, readback, f"cannot verify axis {axis}"
            got = readback.get(axis)
            if abs(got - want) > ax.readback_tolerance:
                return False, readback, f"axis {axis} at {got}, target {want}, tolerance {ax.readback_tolerance}"
        return True, readback, ""

    def _on_fail(self, entry: _Entry, message: str, error_code: str | None) -> None:
        if entry.status.is_terminal:
            self._stale(entry, "fail")
            return
        if self._transition(entry, S.FAILED, message, from_backend=True):
            # a backend's precondition refusal (error code '..._precondition': nothing was written or moved) is a
            # warning, not a hardware fault (audit APP-07)
            sev = Severity.WARNING if (error_code or "").lower().endswith("precondition") else Severity.ERROR
            self.bus.publish(HardwareFaultEvent(entry.command.device_id, message, sev, error_code,
                                                entry.command.command_id))

    def _on_timeout(self, entry: _Entry) -> None:
        cmd = entry.command
        if not self._transition(entry, S.TIMED_OUT, f"no completion within {cmd.timeout_s}s; device state uncertain"):
            return
        with self._admission:
            self._uncertain.setdefault(cmd.device_id, []).append(cmd.command_id)
        self.store.set_recovery_required(cmd.device_id, True, reason=f"command {cmd.command_id} timed out")
        self.bus.publish(HardwareFaultEvent(cmd.device_id, f"command {cmd.kind} timed out", Severity.ERROR,
                                            "TIMEOUT", cmd.command_id))
        rec = self.devices.get(cmd.device_id)
        if rec is not None:
            try:
                rec.backend.cancel(cmd.command_id)  # best-effort stop; later callbacks are ignored
            except Exception:
                log.exception("best-effort cancel after timeout failed")

    def _transition(self, entry: _Entry, new: CommandStatus, message: str | None = None,
                    result: Position | None = None, *, from_backend: bool = False,
                    check: str | None = None) -> bool:
        with entry.cv:
            old = entry.status
            if new not in ALLOWED_TRANSITIONS[old]:
                if from_backend:
                    self._stale(entry, f"{old.value}->{new.value}")
                return False
            entry.record = CommandRecord(command=entry.command, status=new, message=message,
                                         result=result if result is not None else entry.record.result,
                                         check=check)
            entry.history.append(new)
            if new.is_terminal:
                # release BEFORE announcing, so a subscriber may chain the next command immediately
                self._finish(entry, new)
            self._publish(entry, old)
            entry.cv.notify_all()
        return True

    def _finish(self, entry: _Entry, status: CommandStatus) -> None:
        if entry.timeout is not None and status is not S.TIMED_OUT:
            entry.timeout.cancel()
        if status is S.TIMED_OUT:
            return  # keep reservation and requested state until recover_device()
        if entry.reserved:
            self.resources.release(entry.command.command_id)
            entry.reserved = False
        if isinstance(entry.command, MOTION_COMMANDS) and status is not S.REJECTED:
            try:
                self.store.set_requested(entry.command.device_id, None, reason=f"{entry.command.command_id} {status.value}")  # noqa: E501
            except KeyError:
                pass

    def _publish(self, entry: _Entry, previous: CommandStatus | None) -> None:
        r = entry.record
        self.bus.publish(CommandLifecycleEvent(entry.command.command_id, entry.command.device_id, r.status,
                                               previous, r.message, r.result, entry.command.correlation_id))

    def _stale(self, entry: _Entry, what: str) -> None:
        self.stale_backend_events += 1
        log.info("ignored stale backend event %s for %s (status %s)", what, entry.command.command_id,
                 entry.status.value)
