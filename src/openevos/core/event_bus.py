"""Application-scoped event bus with structured, immutable event payloads.

* One EventBus per application instance; nothing global (handoff §13, §16.6).
* Event timestamps are created per instance (`default_factory`) — fixes the handoff §13.2 bug.
* Payloads hold only immutable values (frozen snapshots, scalars, tuples).
* Delivery: by default subscribers run synchronously on the publishing thread, which may be a
  device worker or timer thread. A UI subscriber MUST pass a `dispatcher` that marshals the call
  onto the Qt main thread (e.g. a queued Qt signal). The bus never guesses threads.
* A failing subscriber never breaks the publisher: exceptions are caught and reported to
  `error_handler` (default: logging).
* Subscribers must not block: publishing may happen while core locks are held.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from openevos.core.models import CommandStatus, DeviceSnapshot, Position, utcnow

log = logging.getLogger(__name__)


def _ts() -> datetime:
    return utcnow()


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class DeviceStateChanged:
    device_id: str
    previous: DeviceSnapshot | None
    current: DeviceSnapshot
    reason: str | None = None
    timestamp: datetime = field(default_factory=_ts)


@dataclass(frozen=True)
class CommandLifecycleEvent:
    command_id: str
    device_id: str
    status: CommandStatus
    previous_status: CommandStatus | None
    message: str | None = None
    result: Position | None = None
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=_ts)


@dataclass(frozen=True)
class HardwareFaultEvent:
    device_id: str
    message: str
    severity: Severity
    error_code: str | None = None
    command_id: str | None = None
    timestamp: datetime = field(default_factory=_ts)


@dataclass(frozen=True)
class StorageFaultEvent:
    acquisition_id: str
    message: str
    frame_index: int | None = None
    timestamp: datetime = field(default_factory=_ts)


@dataclass(frozen=True)
class AcquisitionStatusEvent:
    acquisition_id: str
    state: str  # "started" | "completed" | "faulted" | "aborted"
    accepted: int
    written: int
    failed: int
    rejected_at_ingress: int
    expected: int | None = None
    message: str | None = None
    timestamp: datetime = field(default_factory=_ts)


E = TypeVar("E")
Dispatcher = Callable[[Callable[[], None]], None]


class Subscription(Generic[E]):
    def __init__(self, bus: EventBus, event_type: type[E], callback: Callable[[E], None],
                 dispatcher: Dispatcher | None):
        self._bus, self.event_type, self.callback, self.dispatcher = bus, event_type, callback, dispatcher
        self.active = True

    def unsubscribe(self) -> None:
        self._bus._remove(self)


class EventBus:
    def __init__(self, error_handler: Callable[[BaseException, object], None] | None = None):
        self._subs: tuple[Subscription, ...] = ()
        self._lock = threading.Lock()
        self._error_handler = error_handler or (lambda exc, ev: log.exception("event subscriber failed for %r", ev, exc_info=exc))  # noqa: E501
        self.delivery_errors = 0

    def subscribe(self, event_type: type[E], callback: Callable[[E], None], *,
                  dispatcher: Dispatcher | None = None) -> Subscription[E]:
        sub = Subscription(self, event_type, callback, dispatcher)
        with self._lock:
            self._subs = (*self._subs, sub)  # copy-on-write: publish never holds the lock
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            sub.active = False
            self._subs = tuple(s for s in self._subs if s is not sub)

    def publish(self, event: object) -> None:
        for sub in self._subs:
            if not sub.active or not isinstance(event, sub.event_type):
                continue
            if sub.dispatcher is None:
                self._deliver(sub, event)
            else:
                try:
                    sub.dispatcher(functools.partial(self._deliver, sub, event))
                except BaseException as exc:  # noqa: BLE001
                    self._fail(exc, event)

    def _deliver(self, sub: Subscription, event: object) -> None:
        if not sub.active:
            return
        try:
            sub.callback(event)
        except BaseException as exc:  # noqa: BLE001 — subscriber errors never reach publishers
            self._fail(exc, event)

    def _fail(self, exc: BaseException, event: object) -> None:
        self.delivery_errors += 1
        try:
            self._error_handler(exc, event)
        except Exception:
            log.exception("event error handler failed")
