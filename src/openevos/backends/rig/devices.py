"""Simulated discrete devices: objective turret and LED illumination with a filter-cube carriage.

Both report MOVING while they change state (the turret rotates; the carriage travels when the
channel changes, as the real F axis does). They implement `verify_result()` so the router only
reports SUCCEEDED once the device reads back the commanded state.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Iterable

from openevos.core.clock import Clock, TimerHandle
from openevos.core.models import (
    Activity,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    Health,
    Position,
    SetIllumination,
    SetTurretPosition,
)
from openevos.hal.backend import ExecutionContext, Reporter
from openevos.safety.capabilities import (
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
    ParameterRange,
)


class _SimDiscrete:
    backend_kind = BackendKind.SIMULATED
    kind: DeviceKind

    def __init__(self, device_id: str, clock: Clock):
        self.device_id, self.clock = device_id, clock
        self.physical_id = f"sim:{device_id}"
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._report: Reporter | None = None
        self._pending: tuple[str, ExecutionContext, TimerHandle | None] | None = None
        self.fail_next: str | None = None  # fault injection

    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def _obs(self, **fields) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), **fields)

    def connect(self) -> None:
        self._obs(connection=Connection.CONNECTING, activity=Activity.INITIALIZING, reason="simulator connecting")
        self._obs(connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE,
                  position=self.read_position(), attributes=self._state_attributes(), reason="simulator connected")

    def disconnect(self) -> None:
        self._abort_pending()
        self._obs(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN, reason="disconnected")

    def _abort_pending(self) -> tuple[str, ExecutionContext, TimerHandle | None] | None:
        with self._lock:
            p, self._pending = self._pending, None
        if p and p[2]:
            p[2].cancel()
        return p

    def _begin(self, command: Command, ctx: ExecutionContext, delay: float, apply, reason: str) -> None:
        with self._lock:
            if self._pending is not None:
                ctx.fail("simulator busy", "BUSY")
                return
            fail, self.fail_next = self.fail_next, None
            self._pending = (command.command_id, ctx, None)
        self._obs(activity=Activity.MOVING if delay > 0 else Activity.IDLE, reason=reason)
        ctx.mark_started()

        def finish() -> None:
            with self._lock:
                if self._pending is None or self._pending[0] != command.command_id:
                    return
                self._pending = None
                if fail is None:
                    apply()
            if fail is not None:
                self._obs(activity=Activity.IDLE, health=Health.FAULT, reason=fail, fault=fail)
                ctx.fail(fail, "SIM_FAULT")
                return
            self._obs(activity=Activity.IDLE, position=self.read_position(), attributes=self._state_attributes(),
                      reason="state reached")
            ctx.complete(self.read_position())

        timer = self.clock.call_later(max(delay, 0.001), finish)
        with self._lock:
            if self._pending is not None and self._pending[0] == command.command_id:
                self._pending = (command.command_id, ctx, timer)

    def cancel(self, command_id: str) -> None:
        with self._lock:
            p = self._pending
            if p is None or p[0] != command_id:
                return
        self._abort_pending()
        self._obs(activity=Activity.IDLE, reason="cancelled (state unchanged)")
        p[1].mark_cancelled(self.read_position())

    def recover(self) -> None:
        self._abort_pending()
        self._obs(health=Health.NORMAL, activity=Activity.IDLE, position=self.read_position(), reason="recovered")

    @property
    def busy(self) -> bool:
        return self._pending is not None

    def _state_attributes(self) -> dict | None:
        """Facts reported with each reached state (merged into the snapshot's attributes); None = nothing."""
        return None


class SimTurret(_SimDiscrete):
    kind = DeviceKind.TURRET

    def __init__(self, device_id: str, clock: Clock, positions: Iterable[int] = (1, 2, 3, 4, 5), *,
                 seconds_per_slot: float = 0.25, start: int = 1):
        super().__init__(device_id, clock)
        self.positions = tuple(sorted(positions))
        self.position = start
        self.seconds_per_slot = seconds_per_slot

    def start(self, command: Command, ctx: ExecutionContext) -> None:
        if not isinstance(command, SetTurretPosition):
            ctx.fail(f"turret cannot execute {command.kind}", "UNSUPPORTED")
            return
        n = len(self.positions)
        steps = min((command.position - self.position) % n, (self.position - command.position) % n)
        target = command.position

        def apply() -> None:
            self.position = target

        self._begin(command, ctx, 0.0 if steps == 0 else 0.15 + steps * self.seconds_per_slot, apply,
                    f"rotating to slot {target}")

    def read_position(self) -> Position | None:
        return Position(axes=("slot",), values=(float(self.position),), unit="slot", frame=self.device_id)

    def verify_result(self, command: Command) -> tuple[bool, str]:
        if isinstance(command, SetTurretPosition) and self.position != command.position:
            return False, f"turret reads slot {self.position}, commanded {command.position}"
        return True, ""

    def capabilities(self, installed: Iterable[int] | None = None) -> DeviceCapabilities:
        return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.SIMULATED,
                                  source=CapabilitySource.SIMULATED,
                                  supported_commands=frozenset({"turret.set_position"}),
                                  turret_positions=tuple(installed or self.positions),
                                  note="Simulated turret; real turret (O axis) is open-loop and locked.")


class SimIllumination(_SimDiscrete):
    """LED light source + cube carriage. Changing channel moves the carriage (latency)."""

    kind = DeviceKind.ILLUMINATION

    def __init__(self, device_id: str, clock: Clock, channels: Iterable[str], *, carriage_s: float = 0.45,
                 start_channel: str | None = None):
        super().__init__(device_id, clock)
        self.channels = tuple(channels)
        self.channel = start_channel or self.channels[0]
        self.intensity_percent = 0.0
        self.enabled = False
        self.carriage_s = carriage_s

    def start(self, command: Command, ctx: ExecutionContext) -> None:
        if not isinstance(command, SetIllumination):
            ctx.fail(f"illumination cannot execute {command.kind}", "UNSUPPORTED")
            return
        moving = command.channel != self.channel
        ch, pct, en = command.channel, command.intensity_percent, command.enabled

        def apply() -> None:
            self.channel, self.intensity_percent, self.enabled = ch, pct, en

        self._begin(command, ctx, self.carriage_s if moving else 0.0, apply,
                    f"carriage to {ch}" if moving else f"{ch} {'on' if en else 'off'} {pct:g}%")

    def halt(self) -> None:
        """Operator STOP (router `stop_all` / `stop_device`): switch the LED off at once and abandon any
        carriage move. The router cancels the unfinished command first; a move still pending here is
        failed with code HALTED, state unchanged (the router only accepts a cancellation it requested).
        Never waits, and is safe when idle, faulted or disconnected. The intensity is kept so 'Light on'
        after Resume restores the previous level."""
        p = self._abort_pending()
        with self._lock:
            was_on = self.enabled
            self.enabled = False
        self._obs(activity=Activity.IDLE, position=self.read_position(), attributes=self._state_attributes(),
                  reason="halted: LED off" if was_on else "halted (LED already off)")
        if p is not None:
            p[1].fail("halted before the carriage move finished; state unchanged", "HALTED")

    def _state_attributes(self) -> dict:
        # the LED state in the snapshot, so observers (InstrumentController) see an off that no command made
        return {"channel": self.channel, "led_enabled": self.enabled, "intensity_percent": self.intensity_percent}

    @property
    def emitting(self) -> bool:  # dark while the carriage travels
        return self.enabled and not self.busy and self.intensity_percent > 0

    def read_position(self) -> Position | None:
        return Position(axes=("carriage",), values=(float(self.channels.index(self.channel)),), unit="index",
                        frame=self.device_id)

    def verify_result(self, command: Command) -> tuple[bool, str]:
        if isinstance(command, SetIllumination):
            if self.channel != command.channel:
                return False, f"carriage at {self.channel}, commanded {command.channel}"
            if self.enabled != command.enabled or abs(self.intensity_percent - command.intensity_percent) > 1e-6:
                return False, "LED state does not match the command"
        return True, ""

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.SIMULATED,
                                  source=CapabilitySource.SIMULATED,
                                  supported_commands=frozenset({"illumination.set"}),
                                  illumination_channels=self.channels,
                                  ranges=tuple(ParameterRange(name=f"intensity:{ch}", limits=AxisLimits(
                                      minimum=0.0, maximum=100.0, unit="%")) for ch in self.channels),
                                  note="Simulated LED board and cube carriage.")
