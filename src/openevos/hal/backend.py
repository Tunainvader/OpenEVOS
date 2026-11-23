"""Device backend contract (HAL). Simulated and, later, physical backends implement this.

Contract rules:
* `start()` must return promptly. It must call `ctx.mark_started()` only when the operation has
  really started, then later exactly one of `ctx.complete()`, `ctx.fail()` or `ctx.mark_cancelled()`.
* Backends report *observed* state through the `Reporter` given in `attach()`, with their own
  strictly increasing sequence numbers. They never write requested state.
* `read_position()` is the readback used to verify motion before a command can SUCCEED.
* `cancel()` and `read_position()` may be called from any thread and must be thread-safe.
* `physical_id` identifies the physical device for the single-owner rule (simulators use "sim:…").
* Backends report `firmware_mode` (ADR-0009). A device seen in BOOTLOADER is quarantined; a
  physical backend must report APPLICATION before any command is accepted. Backends must never
  implement firmware download/flash/erase or bootloader-transition operations.
* Optional `halt()`: bring the device to a safe state at once, for the router's STOP. It must not
  block on anything slow. An incubator's halt deliberately does nothing (ADR-0006).
* Optional `attributes` in observations: other facts interlocks rely on (homed, carriage
  position, live), so validation reads the store and never queries a device.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from openevos.core.models import (
    Activity,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    FirmwareMode,
    Health,
    Position,
)


class Reporter(Protocol):
    def __call__(self, *, seq: int, connection: Connection | None = None, health: Health | None = None,
                 activity: Activity | None = None, position: Position | None = ...,
                 firmware_mode: FirmwareMode | None = None, attributes: dict | None = None,
                 reason: str | None = None, fault: str | None = None) -> bool: ...


class ExecutionContext(Protocol):
    """Backend-facing view of one accepted command."""

    command_id: str

    @property
    def cancel_requested(self) -> bool: ...
    def mark_started(self) -> None: ...
    def report_progress(self, fraction: float, message: str | None = None) -> None: ...
    def complete(self, result: Position | None = None, payload: object | None = None) -> None:
        """`payload` carries a non-position result (e.g. a snapped Frame) to the ticket."""
    def fail(self, message: str, error_code: str | None = None) -> None: ...
    def mark_cancelled(self, result: Position | None = None) -> None: ...


@runtime_checkable
class DeviceBackend(Protocol):
    device_id: str
    kind: DeviceKind
    backend_kind: BackendKind
    physical_id: str

    def attach(self, reporter: Reporter) -> None: ...
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def start(self, command: Command, ctx: ExecutionContext) -> None: ...
    def cancel(self, command_id: str) -> None: ...
    def read_position(self) -> Position | None: ...
    def recover(self) -> None: ...
