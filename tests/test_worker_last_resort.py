"""Audit APP-04: an unexpected exception must not kill the motor service or the LED worker silently."""

from __future__ import annotations

import threading
import time

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.core.models import CommandStatus, Health

TELE = {"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}}


def _wait(cond, t=5.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_injected_error_in_the_poll_loop_reports_fault_fails_moves_and_reconnects(monkeypatch):
    s = build_sim_session(ThreadClock(), fast=True, telemetry=TELE, cameras=None, illumination=None)
    try:
        svc, ctl = s.telemetry[0], s.controller
        assert _wait(lambda: ctl.snapshot(s.profile.stage_device).observed_position is not None)
        real = svc.monitor.status
        calls = {"n": 0}

        def boom(values):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("injected")
            return real(values)

        monkeypatch.setattr(svc.monitor, "status", boom)
        stops = svc.stops_sent
        assert _wait(lambda: ctl.snapshot(s.profile.stage_device).health is Health.FAULT)
        snap = ctl.snapshot(s.profile.stage_device)
        assert snap.recovery_required and "internal error" in (snap.note or "") + str(svc.last_error)
        assert svc.stops_sent > stops  # the controlled stop was attempted
        assert svc._thread.is_alive()  # the service carries on (reconnects after the pause)
        # a move now is refused (recovery required), it does not hang
        t = ctl.move_stage(1000.0, 0.0)
        assert t.wait(5) in (CommandStatus.REJECTED, CommandStatus.FAILED)
        # recover: the next poll reports the drive's real state
        s.core.router.recover_device(s.profile.stage_device)
        assert _wait(lambda: not ctl.snapshot(s.profile.stage_device).recovery_required, 10)
    finally:
        s.shutdown()


def test_led_worker_survives_a_job_that_raises():
    from openevos.backends.ledboard import illumination as il

    dev = il.RealLedIllumination.__new__(il.RealLedIllumination)
    dev._jobs = __import__("queue").Queue()
    dev._pending = None
    seen = []
    dev._fail_safe = lambda why: seen.append(why) or why
    t = threading.Thread(target=dev._run, daemon=True)
    t.start()

    def bad():
        raise TypeError("injected")
    done = threading.Event()
    dev._jobs.put((bad, done))
    assert done.wait(2) and t.is_alive() and "injected" in seen[0]
    ok = threading.Event()
    dev._jobs.put((lambda: ok.set(), None))  # the next job is still served
    assert ok.wait(2)
    dev._jobs.put(None)
