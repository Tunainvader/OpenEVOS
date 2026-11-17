"""Runaway watchdog, Phase 1 shadow mode (ADR-0047): it measures and records, and never acts."""

from __future__ import annotations

import threading
import time

from test_technosoft_telemetry import _SlowLineChain

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft import watchdog as wd
from openevos.backends.technosoft.stage_monitor import StageMonitor
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.maintenance import motor_gate9_drive_status as g9

AXISOFF_FRAMES = {bytes.fromhex("04 00 10 00 02 16"), bytes.fromhex("04 00 20 00 02 26")}


class _Ctx:
    def __init__(self):
        self.done = threading.Event()
        self.result = self.message = None
        self.cancel_requested = False

    def mark_started(self):
        pass

    def complete(self, pos):
        self.result = "ok"
        self.done.set()

    def fail(self, message, error_code=""):
        self.result, self.message = error_code or "failed", message
        self.done.set()

    def mark_cancelled(self, pos):
        self.result = "cancelled"
        self.done.set()


def _move(svc, x=None, y=None):
    ctx = _Ctx()
    last = svc._last or {}
    targets = {"X": x if x is not None else last.get("X", {}).get("APOS", 0),
               "Y": y if y is not None else last.get("Y", {}).get("APOS", 0)}
    svc.submit(tm._MoveJob("c1", ctx, targets, (0.0, 0.0)))
    assert ctx.done.wait(15)
    return ctx


def _service(chain, folder=None, **kw):
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True, fast_log=True,
                              monitor=StageMonitor(folder), **kw)
    svc.start()
    end = time.monotonic() + 5
    while svc._last is None and time.monotonic() < end:
        time.sleep(0.02)
    return svc


# ---------------------------------------------------------------- the leg watch itself

def test_poserr_is_gate9s_signed_read():
    for ax in ("X", "Y"):
        v = tp.VARS[ax]["POSERR"]
        assert v.bits == 16 and v.address == 0x022A == dict(g9.STATUS_VARS)["POSERR"]
        assert v.frame == g9.STATUS[ax]["POSERR"].frame


def test_overshoot_and_return_is_not_against_the_command():
    w = wd.LegWatch("X", 0, 1000, "fast", 0.0)
    for i, a in enumerate((0, 300, 700, 1000, 1020, 1010, 1000)):  # 20 counts past the target, then back
        w.apos(a, i * 0.05)
    assert w.back_max == 20 and not w.would_trip


def test_each_provisional_rule_only_notes():
    w = wd.LegWatch("Y", 0, 1000, "slow", 0.0)
    w.apos(400, 0.05)
    w.apos(349, 0.10)  # 51 counts back from the closest approach
    assert w.would_trip[0].startswith("runaway_against_command:")
    w.iq(wd.IQ_LIMIT + 1, 0.10)  # one read over the limit: a spike, not a trip
    w.iq(100, 0.23)
    assert not any(t.startswith("runaway_overcurrent") for t in w.would_trip)
    w.iq((-(wd.IQ_LIMIT + 5)) & 0xFFFF, 0.36)  # signed: negative current counts too
    w.iq(wd.IQ_LIMIT + 9, 0.49)
    assert any(t.startswith("runaway_overcurrent") for t in w.would_trip)
    assert w.iq_over_limit_s == 0.13
    w.poserr((-1001) & 0xFFFF, 0.5)
    assert any(t.startswith("runaway_following_error: POSERR -1001") for t in w.would_trip)


def test_row_trusts_poserr_only_when_it_matches_the_command_at_rest():
    w = wd.LegWatch("X", 0, 1000, "medium", 0.0)
    w.apos(500, 0.5)
    w.poserr(40, 0.5)  # the drive lags behind a + move: same sign as the direction
    ok = w.row("done", {"POSERR": 2, "CPOS": 1000, "APOS": 998})
    assert ok["fe_trusted"] is True and ok["fe_sign_vs_direction"] == "+" and ok["fe_max_abs"] == 40
    bad = w.row("done", {"POSERR": 0xFFF0, "CPOS": 1000, "APOS": 998})  # -16 against +2
    assert bad["fe_trusted"] is False
    assert w.row("wrong direction")["fe_trusted"] is False  # no rest reads: never trusted


# ---------------------------------------------------------------- in the app's move loop

def test_a_move_records_one_profile_row_and_writes_nothing_new(tmp_path):
    chain = SimulatedDriveChain(allow_motion=True)
    chain.speed_counts_s = 20000
    svc = _service(chain, tmp_path)
    try:
        assert _move(svc, x=4000).result == "ok"
    finally:
        svc.stop()
    rows = wd.profile(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["axis"] == "X" and r["speed_preset"] == "fast" and r["distance_counts"] == "4000"
    assert r["outcome"] == "done" and r["fe_trusted"] == "True" and r["would_trip"] == ""
    assert int(r["fe_reads"]) >= 1 and int(r["iq_reads"]) >= 1
    sent = [w for w in chain.writes if w not in tp.ALLOWED_REQUESTS]
    assert sent == [tp.move_target("X", 4000), tp.move_request("X"), *tp.stop_frames("X"),
                    *(f for ax in tp.MOTION_AXES for f in tp.stop_frames(ax))]  # the last: app closing
    assert tp.VARS["X"]["POSERR"].frame in chain.writes and tp.VARS["X"]["IQ"].frame in chain.writes


def test_a_runaway_is_noted_and_left_to_the_existing_guard(tmp_path):
    chain = SimulatedDriveChain(allow_motion=True)
    chain.wrong_way = {"Y"}
    svc = _service(chain, tmp_path)
    try:
        ctx = _move(svc, y=2000)
        notes = " ".join(text for _, text in svc.monitor.commands)
    finally:
        svc.stop()
    assert ctx.result == "wrong_direction"  # the 100-count guard, as before: stop and latch
    assert svc.monitor.latch.rule == "wrong_direction"
    r = wd.profile(tmp_path)[0]
    assert r["outcome"] == "wrong direction" and "runaway_against_command" in r["would_trip"]
    assert "watchdog (shadow, not acted on): Y runaway_against_command" in notes
    assert not AXISOFF_FRAMES & set(chain.writes)


def test_a_simulated_stage_keeps_rows_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    svc = _service(SimulatedDriveChain(allow_motion=True))
    try:
        assert _move(svc, x=500).result == "ok"
    finally:
        svc.stop()
    assert svc.watch_rows and svc.watch_rows[-1]["axis"] == "X"
    assert not list(tmp_path.rglob(wd.PROFILE_FILE))


def test_drift_sampling_stays_near_15_hz_during_a_move_with_real_read_times():
    """Spec §4.5: the extra read per tick costs ~25 % of the in-leg sample rate (~20 Hz -> ~15 Hz on COM3's ~15 ms
    reads). The rate is the leg's own ticks; the reads before and after a leg are unchanged and not sampled."""
    chain = _SlowLineChain(allow_motion=True)
    chain.speed_counts_s = 2000  # a 2 s leg
    svc = _service(chain)
    try:
        assert _move(svc, x=4000).result == "ok"
        row = svc.watch_rows[-1]
    finally:
        svc.stop()
    assert float(row["tick_hz"]) >= 13.5, row
