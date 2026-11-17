"""Injected time source for timers and simulated latency.

Simulated devices and command timeouts schedule work with `clock.call_later()` instead of
sleeping, so no worker is ever blocked (handoff §22). Tests use `ManualClock` and advance time
explicitly, making lifecycle tests deterministic; the application uses `ThreadClock`.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Callable
from typing import Protocol


class TimerHandle:
    def __init__(self) -> None:
        self._cancelled = False
        self._fired = False
        self._lock = threading.Lock()

    def cancel(self) -> bool:
        """Cancel if not yet fired. Returns True if this call prevented the callback."""
        with self._lock:
            if self._fired or self._cancelled:
                return False
            self._cancelled = True
            return True

    def _claim(self) -> bool:
        with self._lock:
            if self._cancelled or self._fired:
                return False
            self._fired = True
            return True


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def call_later(self, delay: float, fn: Callable[[], None]) -> TimerHandle: ...


class ManualClock:
    """Deterministic clock: callbacks run synchronously inside `advance()`, in time order."""

    def __init__(self, start: float = 0.0):
        self._now = start
        self._heap: list[tuple[float, int, TimerHandle, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def call_later(self, delay: float, fn: Callable[[], None]) -> TimerHandle:
        h = TimerHandle()
        with self._lock:
            heapq.heappush(self._heap, (self._now + max(0.0, delay), next(self._seq), h, fn))
        return h

    def advance(self, dt: float) -> int:
        """Advance time by dt seconds, running every callback that becomes due. Returns count run."""
        with self._lock:
            target = self._now + dt
        ran = 0
        while True:
            with self._lock:
                if not self._heap or self._heap[0][0] > target:
                    self._now = target
                    return ran
                when, _, h, fn = heapq.heappop(self._heap)
                self._now = when
            if h._claim():
                fn()
                ran += 1

    def pending(self) -> int:
        with self._lock:
            return sum(1 for _, _, h, _ in self._heap if not h._cancelled and not h._fired)


class ThreadClock:
    """Real-time clock with one scheduler thread. Callbacks must be short and non-blocking."""

    def __init__(self, name: str = "openevos-clock"):
        self._heap: list[tuple[float, int, TimerHandle, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def monotonic(self) -> float:
        return time.monotonic()

    def call_later(self, delay: float, fn: Callable[[], None]) -> TimerHandle:
        h = TimerHandle()
        with self._cv:
            heapq.heappush(self._heap, (time.monotonic() + max(0.0, delay), next(self._seq), h, fn))
            self._cv.notify()
        return h

    def stop(self, timeout: float = 2.0) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify()
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stopped and (not self._heap or self._heap[0][0] > time.monotonic()):
                    wait = None if not self._heap else max(0.0, self._heap[0][0] - time.monotonic())
                    self._cv.wait(wait)
                if self._stopped:
                    return
                _, _, h, fn = heapq.heappop(self._heap)
            if h._claim():
                try:
                    fn()
                except Exception:
                    import logging

                    logging.getLogger(__name__).exception("clock callback failed")
