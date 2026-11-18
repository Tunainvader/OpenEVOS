"""Physical X/Y stage Stop and jog through the command core (ADR-0024). Simulated drive chain only."""

from __future__ import annotations

import time

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft.transport import SimulatedDriveChain, check_write
from openevos.core.command_language import Actor, CommandRejected
from openevos.core.instrument import InstrumentConfig, build_instrument
from openevos.maintenance import motor_gate3_stop as g3
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate6_y_move as g6


def test_motion_frames_are_the_gate_frames():
    assert tp.move_target("X", 2268) == g4.M1_TARGET_OUT and tp.move_target("X", 0) == g4.M2_TARGET_HOME
    assert tp.move_request("X") == g4.M3_ESTATE_MOVE
    assert tp.stop_frames("X") == tuple(f for _n, f in g3.WRITES)
    assert tp.move_target("Y", 27213) == g6.Y1_TARGET_OUT and tp.move_request("Y") == g6.Y3_ESTATE_MOVE
    assert tp.stop_frames("Y") == tuple(f for _n, f in g6.Y_STOP)


@pytest.mark.parametrize("bad", [
    bytes.fromhex("06 00 40 21 B3 00 08 22"),  # move request on Z
    bytes.fromhex("06 00 10 21 B3 00 09 F3"),  # halt state
    bytes.fromhex("04 00 10 00 02 16"),  # AXISOFF
    bytes.fromhex("08 00 10 25 B6 00 00 00 00 EB"),  # limit write
    bytes.fromhex("08 00 10 25 D6 00 00 00 20 2B"),  # speed write
])
def test_only_reviewed_motion_writes(bad):
    assert not tp.is_motion_write(bad)
    with pytest.raises(tp.ProtocolRefused):
        check_write(bad, True)


def test_motion_writes_need_the_opt_in():
    frame = tp.move_request("X")
    with pytest.raises(tp.ProtocolRefused):
        check_write(frame, False)
    check_write(frame, True)
    with pytest.raises(tp.ProtocolRefused):
        tp.move_target("X", 400000)  # outside the encodable bounds
    with pytest.raises(tp.ProtocolRefused):
        SimulatedDriveChain().write(frame)


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _instrument(motion=True, **chain_kw):
    cfg = InstrumentConfig(backend_options={"seed": 1, "time_scale": 0.0},
                           telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": motion}})
    inst = build_instrument(cfg)
    svc = inst.telemetry[0]
    for k, v in chain_kw.items():
        setattr(svc.simulated_chain, k, v)
    assert _wait(lambda: (s := inst.gateway.core_snapshot().get("motor.stage")) is not None
                 and s.observed_position is not None)
    return inst, svc, svc.simulated_chain


def test_read_only_session_rejects_jogs():
    inst, _svc, chain = _instrument(motion=False)
    try:
        assert inst.gateway.motor_control_device is None
        with pytest.raises(CommandRejected, match="not enabled"):
            inst.gateway.motor_jog("x", 1000.0)
        with pytest.raises(CommandRejected):
            inst.gateway.motor_stop()
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes)
    finally:
        inst.close()


def test_jog_x_one_mm_through_the_core():
    inst, _svc, chain = _instrument()
    try:
        assert inst.gateway.motor_control_device == "motor.stage"
        snap = inst.gateway.core_snapshot()["motor.stage"]
        assert snap.attr("read_only") is False
        inst.gateway.motor_jog("x", 1000.0)
        assert _wait(lambda: chain.values[("X", "APOS")] == 2268)
        assert _wait(lambda: chain.values[("X", "ESTATEV")] == 0)  # verified stop at rest afterwards
        written = [w for w in chain.writes if w not in tp.ALLOWED_REQUESTS]
        assert written == [tp.move_target("X", 2268), tp.move_request("X"), *tp.stop_frames("X")]
        assert _wait(lambda: abs(inst.gateway.core_snapshot()["motor.stage"].observed_position.get("x") - 1000.0) < 1)
        events = [e for e in inst.gateway.audit.events() if e.command == "motor.stage.jog"]
        assert events and events[-1].outcome == "ok"
    finally:
        inst.close()


def test_jog_outside_the_box_rejected_by_the_safety_manager():
    inst, _svc, chain = _instrument()
    try:
        with pytest.raises(CommandRejected, match="outside"):
            inst.gateway.motor_jog("x", -1000.0)  # from 0 -> -1 mm, outside [0, 22000] um
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes)
    finally:
        inst.close()


@pytest.mark.parametrize("axis, step, why", [("z", 1000.0, "axis"), ("x", 2000.0, "step"), ("x", 100000.0, "step")])
def test_bad_jog_parameters(axis, step, why):
    inst, _svc, _chain = _instrument()
    try:
        with pytest.raises(CommandRejected, match=why):
            inst.gateway.motor_jog(axis, step)
    finally:
        inst.close()


def test_ai_may_not_jog_or_stop():
    inst, _svc, _chain = _instrument()
    try:
        with pytest.raises(CommandRejected, match="AI"):
            inst.gateway.motor_jog("x", 1000.0, actor=Actor.AI)
        with pytest.raises(CommandRejected, match="AI"):
            inst.gateway.motor_stop(actor=Actor.AI)
    finally:
        inst.close()


def test_unhomed_stage_rejected():
    inst, _svc, chain = _instrument()
    try:
        chain.values[("X", "IINITONCEDONE")] = 0
        assert _wait(lambda: inst.gateway.core_snapshot()["motor.stage"].attr("homed") is False)
        with pytest.raises(CommandRejected):
            inst.gateway.motor_jog("x", 1000.0)
    finally:
        inst.close()


def test_global_stop_halts_a_running_move():
    inst, svc, chain = _instrument(moves_complete=False)
    try:
        inst.gateway.motor_jog("y", 5000.0)
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 13)  # moving (never completes in this model)
        inst.gateway.stop_all()
        assert _wait(lambda: svc.stops_sent >= 1)
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 0)
        written = [w for w in chain.writes if w not in tp.ALLOWED_REQUESTS]
        assert written[-6:] == [*tp.stop_frames("X"), *tp.stop_frames("Y")]
    finally:
        inst.close()


def test_stage_stop_button_path():
    inst, svc, _chain = _instrument()
    try:
        inst.gateway.motor_stop()
        assert _wait(lambda: svc.stops_sent >= 1)
        assert any(e.command == "motor.stage.stop" for e in inst.gateway.audit.events())
    finally:
        inst.close()


def test_drifted_axis_is_refused_before_any_write():
    from openevos.core.command_language import CommandFailed
    inst, _svc, chain = _instrument()
    try:
        chain.values[("Y", "APOS")] = -8  # encoder 8 counts off its command (seen after X-only moves)
        before = len(chain.writes)
        with pytest.raises((CommandRejected, CommandFailed), match="drift"):
            inst.gateway.motor_jog("y", 1000.0)
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes[before:])
    finally:
        inst.close()


def _jog(inst, axis, step):
    """motor_jog returns once a move has started; a failure after that settles in the audit, not as an exception.
    Returns the refusal/failure text raised before the start, else ''."""
    from openevos.core.command_language import CommandFailed
    try:
        inst.gateway.motor_jog(axis, step)
    except (CommandRejected, CommandFailed) as exc:
        return str(exc)
    return ""


def _audited(inst, text, timeout=5.0):
    return _wait(lambda: any(text in str(e.__dict__) for e in inst.gateway.audit.events()), timeout)


def test_cross_axis_drift_stops_the_move_and_latches_both_axes():
    """Plan v3 §6.7 fault injection: Y's count jumps while only X moves -> latch within one sample; X stopped."""
    inst, svc, chain = _instrument()
    try:
        chain.drift_on_move = {"Y": -15}  # beyond the tracked-knock bound (10, 2026-11-19)
        _jog(inst, "x", 1000.0)
        assert _wait(lambda: svc.monitor.latched)
        assert _audited(inst, "idle_encoder_drift")
        assert svc.monitor.latch.rule == "idle_encoder_drift" and svc.monitor.latch.axis == "Y"
        assert "-15" in svc.monitor.latch.message and "while X moved" in svc.monitor.latch.message
        assert svc.monitor.extra["Y"]["ISTATUSFLAGS"] == 0xD000  # the drive's record, read at the latch
        chain.drift_on_move = {}
        chain.values[("Y", "APOS")] = 0  # even back on its command, the stage stays locked (the phase may be wrong)
        for axis in ("y", "x"):  # every X/Y move, not only the drifted axis
            before = len(chain.writes)
            assert "locked" in _jog(inst, axis, 1000.0)
            assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes[before:])
        assert _wait(lambda: inst.gateway.core_snapshot()["motor.stage"].attr("drift_lock") is True)
        assert "power-cycle" in inst.gateway.core_snapshot()["motor.stage"].attr("drift_lock_reason")
    finally:
        inst.close()


def test_latch_clears_only_after_x_and_y_are_seen_to_re_home():
    from openevos.core.models import Health
    inst, svc, chain = _instrument()
    try:
        chain.drift_on_move = {"Y": -15}  # beyond the tracked-knock bound (10, 2026-11-19)
        _jog(inst, "x", 1000.0)
        assert _wait(lambda: svc.monitor.latched)
        chain.drift_on_move = {}
        chain.values[("X", "IINITONCEDONE")] = 0  # only X re-initialises: not enough
        assert _wait(lambda: not svc.monitor.homed["X"])
        chain.values[("X", "IINITONCEDONE")] = tp.INIT_DONE_MARKER
        assert _wait(lambda: svc.monitor.homed["X"])
        assert svc.monitor.latched
        for ax in ("X", "Y"):  # power-on homing of both, as after a power cycle
            chain.values[(ax, "IINITONCEDONE")] = 0
        assert _wait(lambda: not svc.monitor.homed["Y"])
        for ax in ("X", "Y"):
            chain.values[(ax, "APOS")] = chain.values[(ax, "CPOS")] = 0
            chain.values[(ax, "IINITONCEDONE")] = tp.INIT_DONE_MARKER
        assert _wait(lambda: not svc.monitor.latched)
        assert _wait(lambda: svc.monitor.ref["Y"] is not None, 3.0)  # watching again after the settle time
        assert _wait(lambda: (s := inst.gateway.core_snapshot()["motor.stage"]).health is Health.NORMAL
                     and s.observed_position.get("x") == 0.0)
        assert _jog(inst, "y", 1000.0) == ""
        assert _wait(lambda: chain.values[("Y", "APOS")] == 2268)
    finally:
        inst.close()


def test_wrong_way_move_is_stopped_and_latched():
    inst, svc, chain = _instrument()
    try:
        assert _jog(inst, "y", 5000.0) == ""  # to 5 mm
        assert _wait(lambda: chain.values[("Y", "APOS")] == 11339 and chain.values[("Y", "ESTATEV")] == 0)
        assert _wait(lambda: abs(inst.gateway.core_snapshot()["motor.stage"].observed_position.get("y") - 5000) < 1)
        chain.wrong_way = {"Y"}
        _jog(inst, "y", -1000.0)  # toward 0, but the axis runs up
        assert _wait(lambda: svc.monitor.latched) and _audited(inst, "wrong_direction")
        assert svc.monitor.latch.rule == "wrong_direction"
        chain.wrong_way = set()
        assert "locked" in _jog(inst, "y", -1000.0)
    finally:
        inst.close()


@pytest.mark.parametrize("var, value, rule", [("SRL", 0x4400, "power_stage_off"),  # Gate 9 read after Y's trip
                                              ("ISTATUSFLAGS", 0x1001, "overcurrent_trip"),  # likewise
                                              ("ISTATUSFLAGS", 0xF000, "oscillation_axis_off"),
                                              ("ISTATUSFLAGS", 0xD010, "comm_error_abort")])
def test_drive_flags_latch_at_rest(var, value, rule):
    inst, svc, chain = _instrument()
    try:
        chain.values[("Y", var)] = value
        assert _wait(lambda: svc.monitor.latched)
        assert svc.monitor.latch.rule == rule and svc.monitor.latch.axis == "Y"
        assert "locked" in _jog(inst, "x", 1000.0)
    finally:
        inst.close()


def test_drive_flags_after_a_move_latch_and_fail_it():
    inst, svc, chain = _instrument()
    try:
        real = chain._motion

        def trips(data):  # Y's over-current handler: the move "completes" with bit 0 set and the axis off
            real(data)
            if data == tp.move_request("Y"):
                chain.values[("Y", "ISTATUSFLAGS")] = 0x1001
                chain.values[("Y", "SRL")] = 0x4400

        chain._motion = trips
        _jog(inst, "y", 1000.0)
        assert _wait(lambda: svc.monitor.latched) and _audited(inst, "overcurrent_trip")
        assert svc.monitor.latch.rule == "overcurrent_trip"
        assert svc.monitor.extra["Y"]["ISTATUSFLAGS"] == 0x1001  # read before anything else was sent
    finally:
        inst.close()


def test_fast_encoder_log_runs_at_about_20_hz():
    inst, svc, _chain = _instrument()
    try:
        n0, t0 = svc.monitor.sample_count, time.monotonic()
        time.sleep(1.0)
        rate = (svc.monitor.sample_count - n0) / (time.monotonic() - t0)
        assert 12 <= rate <= 25, rate
        assert svc.monitor.ref["X"] == 0 and svc.monitor.ref["Y"] == 0
    finally:
        inst.close()


def test_a_link_failure_mid_move_fails_the_command_with_nothing_more_written():
    """Plan v3 §6.7: stale telemetry inhibits motion. Every move re-reads the drives first; if the line dies, the
    command fails at once instead of waiting for the router's timeout."""
    inst, _svc, chain = _instrument()
    try:
        chain.read = lambda n: b""  # the line goes silent
        before = len(chain.writes)
        t0 = time.monotonic()
        assert _jog(inst, "x", 1000.0)  # refused or failed before the start
        assert time.monotonic() - t0 < 5.0
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes[before:])
    finally:
        inst.close()


def test_live_position_is_reported_while_moving():
    inst, svc, chain = _instrument()
    try:
        chain.speed_counts_s = 8.27 * tp.COUNTS_PER_MM["X"]
        svc.poll_interval_s = 5.0  # the regular poll would be far too slow to show progress
        seen = []
        inst.gateway.motor_jog("x", 5000.0)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            p = inst.gateway.core_snapshot()["motor.stage"].observed_position.get("x")
            if 100 < p < 4900 and p not in seen:
                seen.append(p)
            time.sleep(0.05)
        assert len(seen) >= 2  # intermediate positions arrived during the ~0.6 s move
    finally:
        inst.close()


def test_operator_axis_lock_refuses_y_but_allows_x():
    from openevos.backends.technosoft.telemetry import create_telemetry
    from openevos.core.command_language import CommandFailed
    svc = create_telemetry({"port": "sim", "motion": True, "locked_axes": ["y"]})
    assert "Y" in svc._drift_locked and "X" not in svc._drift_locked
    cfg = InstrumentConfig(backend_options={"seed": 1, "time_scale": 0.0},
                           telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True,
                                                     "locked_axes": ["Y"]}})
    inst = build_instrument(cfg)
    try:
        chain = inst.telemetry[0].simulated_chain
        assert _wait(lambda: (s := inst.gateway.core_snapshot().get("motor.stage")) is not None
                     and s.observed_position is not None)
        with pytest.raises((CommandRejected, CommandFailed), match="locked"):
            inst.gateway.motor_jog("y", 1000.0)
        inst.gateway.motor_jog("x", 1000.0)
        assert _wait(lambda: chain.values[("X", "APOS")] == 2268)
    finally:
        inst.close()


def test_an_axis_that_does_not_move_fails_clearly_without_a_lock():
    from openevos.core.command_language import CommandFailed
    inst, svc, chain = _instrument()
    try:
        chain.moves_complete = True
        real = chain._motion

        def dead_y(data):  # Y's power stage is off: the move "completes" but the encoder never changes
            real(data)
            if data == tp.move_request("Y"):
                chain.values[("Y", "APOS")] = 0
                chain.values[("Y", "SRH")] = 0x033F

        chain._motion = dead_y
        try:
            inst.gateway.motor_jog("y", 1000.0)
        except CommandFailed as exc:
            assert "did not move" in str(exc)
        assert _wait(lambda: any("did not move at all" in str(e.__dict__) for e in inst.gateway.audit.events()))
        assert "Y" not in svc._drift_locked and not svc.monitor.latched  # power stage reads on: not latched
    finally:
        inst.close()
