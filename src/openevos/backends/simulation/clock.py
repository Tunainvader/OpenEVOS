"""Time source for simulated devices.

`time_scale` stretches simulated physical durations (motion, exposure): 1.0 is real time, 0.0
makes every motion and exposure complete instantly (used by fast tests). `FakeClock` gives tests
full control over time.
"""

from __future__ import annotations

import time


class SimClock:
    def __init__(self, time_scale: float = 1.0):
        if time_scale < 0:
            raise ValueError("time_scale must be >= 0")
        self.time_scale = float(time_scale)

    def now(self) -> float:
        return time.monotonic()

    def duration(self, physical_s: float) -> float:
        """Wall-clock seconds that a physical duration takes in this simulation."""
        return max(0.0, physical_s) * self.time_scale

    def sleep(self, physical_s: float) -> None:
        d = self.duration(physical_s)
        if d > 0:
            time.sleep(d)


class FakeClock(SimClock):
    """Manually advanced clock for deterministic tests."""

    def __init__(self, start: float = 1000.0, time_scale: float = 1.0):
        super().__init__(time_scale)
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def sleep(self, physical_s: float) -> None:
        self.advance(self.duration(physical_s))
