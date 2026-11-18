"""A still axis knocked a few counts by the other axis' move is tracked and put back, not latched (operator
2026-11-19: "when we already know the creep, can't we keep track and correct it"). On the unit, Y lost 2-6 counts each
time X passed ~33-41 mm at full speed (2026-11-18 18:56, 19:26, 20:09). Beyond CROSS_AXIS_TOLERANCE_COUNTS, or at rest,
the monitor still latches."""

from __future__ import annotations

import threading
import time

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.core.models import MoveStageAbsolute

X0, Y0 = 1000, 40000


class Ctx:
    def __init__(self):
        self.status, self.message = None, ""
        self.done = threading.Event()
        self.cancel_requested = False

    def mark_started(self):
        pass

    def report_progress(self, fraction, message=None):
        pass

    def complete(self, result=None, payload=None):
        self.status = "ok"
        self.done.set()

    def fail(self, message, error_code=None):
        self.status, self.message = "failed", message
        self.done.set()

    def mark_cancelled(self, result=None):
        self.status = "cancelled"
        self.done.set()


def _setup():
    chain = SimulatedDriveChain({("X", "LPOSMIN"): -1000, ("X", "LPOSMAX"): 269000, ("Y", "LPOSMIN"): -1500,
                                 ("Y", "LPOSMAX"): 175600, ("X", "APOS"): X0, ("X", "CPOS"): X0, ("Y", "APOS"): Y0,
                                 ("Y", "CPOS"): Y0}, allow_motion=True)
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True,
                              sleep=lambda s: time.sleep(0.001), monitor=sm.StageMonitor(None))
    svc.start()
    deadline = time.monotonic() + 5
    while not svc.monitor.status_seen and time.monotonic() < deadline:
        time.sleep(0.02)
    return chain, svc


def _move_x(svc, x_counts: int) -> Ctx:
    ctx = Ctx()
    stage = next(b for b in svc.backends if b.device_id == "motor.stage")
    stage.start(MoveStageAbsolute(device_id="motor.stage", origin="test", x=tm.um_from_counts("X", x_counts),
                                  y=tm.um_from_counts("Y", Y0), unit="um", frame="stage"), ctx)
    assert ctx.done.wait(20)
    return ctx


def test_a_small_knock_is_put_back_without_a_lock():
    chain, svc = _setup()
    try:
        chain.knock = {"Y": -6}  # as at 20:09 on the unit
        ctx = _move_x(svc, 30000)
        assert ctx.status == "ok", ctx.message
        assert not svc.monitor.latched
        assert abs(chain.values[("X", "APOS")] - 30000) <= tm.SETTLE_BAND_COUNTS
        assert abs(chain.values[("Y", "APOS")] - Y0) <= tm.SETTLE_BAND_COUNTS  # put back
        notes = " ".join(c[1] for c in svc.monitor.commands)
        assert "knocked -6" in notes and "putting Y back" in notes
        # put back by approaching: a real Y leg past the target, then onto it
        head = tp.move_target("Y", 0)[:5]
        y_targets = [w for w in chain.writes if len(w) > 5 and w[:5] == head]
        assert y_targets == [tp.move_target("Y", Y0 + tm.CORRECT_APPROACH_COUNTS), tp.move_target("Y", Y0)]
    finally:
        svc.stop()


def test_a_go_to_finishes_when_the_x_leg_knocks_y():
    """X then Y in one go-to: Y's leg starts off its command by the tracked knock, moves, and lands (used to lock)."""
    chain, svc = _setup()
    try:
        chain.knock = {"Y": -9}
        ctx = Ctx()
        stage = next(b for b in svc.backends if b.device_id == "motor.stage")
        stage.start(MoveStageAbsolute(device_id="motor.stage", origin="test", x=tm.um_from_counts("X", 30000),
                                      y=tm.um_from_counts("Y", 45000), unit="um", frame="stage"), ctx)
        assert ctx.done.wait(20)
        assert ctx.status == "ok", ctx.message
        assert not svc.monitor.latched
        assert abs(chain.values[("Y", "APOS")] - 45000) <= tm.SETTLE_BAND_COUNTS
    finally:
        svc.stop()


def test_a_large_knock_still_latches():
    chain, svc = _setup()
    try:
        chain.knock = {"Y": -(sm.CROSS_AXIS_TOLERANCE_COUNTS + 5)}
        ctx = _move_x(svc, 30000)
        assert ctx.status == "failed" and svc.monitor.latched
    finally:
        svc.stop()


def test_drift_at_rest_still_latches_at_two_counts():
    mon = sm.StageMonitor(None)
    mon.status({ax: {"IINITONCEDONE": tp.INIT_DONE_MARKER, "APOS": 0, "CPOS": 0} for ax in sm.AXES})
    mon.settle_until = dict.fromkeys(sm.AXES, 0.0)
    mon.fast(0, 0)
    assert mon.fast(0, -3) is not None and mon.latch.rule == "idle_encoder_drift"  # nothing moving: no tolerance
