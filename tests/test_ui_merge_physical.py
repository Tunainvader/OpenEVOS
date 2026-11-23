"""UI merge, phase 3: the new app's session with the physical motor drives (simulated drive chain only)."""

from __future__ import annotations

import time

import pytest

from openevos.acquisition.session import PHYSICAL_STAGE_DEVICE, build_sim_session
from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.core.clock import ThreadClock
from openevos.core.models import CommandStatus, Connection
from openevos.instrument.readiness import Readiness, readiness_for


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _session(motion: bool, tmp_path=None):
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02,
                                                                            "motion": motion}},
                          audit_path=(tmp_path / "physical_audit.jsonl") if tmp_path else None)
    assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None
                 and st.connection is Connection.CONNECTED)
    return s


def test_simulation_only_session_is_unchanged():
    s = build_sim_session(ThreadClock(), fast=True)
    try:
        assert s.telemetry == [] and not s.physical_stage and s.profile.stage_device != PHYSICAL_STAGE_DEVICE
        assert s.core.store.get("motor.stage") is None
    finally:
        s.shutdown()


def test_read_only_telemetry_keeps_the_simulated_stage():
    s = _session(motion=False)
    try:
        assert not s.physical_stage and s.profile.stage_device != PHYSICAL_STAGE_DEVICE
        chain = s.telemetry[0].simulated_chain
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes)
    finally:
        s.shutdown()
    assert not any(t.is_alive() for t in __import__("threading").enumerate() if t.name == "technosoft-telemetry")


def test_motor_control_drives_the_real_stage_through_the_router(tmp_path):
    s = _session(motion=True, tmp_path=tmp_path)
    try:
        assert s.physical_stage and s.profile.stage_device == PHYSICAL_STAGE_DEVICE
        chain = s.telemetry[0].simulated_chain
        t = s.controller.move_stage(1000.0, 0.0)  # the new UI's stage path, now the physical device
        assert t.wait(10) is CommandStatus.SUCCEEDED, t.record.message
        assert chain.values[("X", "APOS")] == 2268
        assert s.controller.stage_xy() == pytest.approx((1000.0, 0.0), abs=1.0)
        statuses = [r["status"] for r in s.physical_audit if r["device"] == PHYSICAL_STAGE_DEVICE]
        assert statuses[-1] == "succeeded"
        assert (tmp_path / "physical_audit.jsonl").read_text(encoding="utf-8").count("motor.stage") >= len(statuses)
    finally:
        s.shutdown()


def test_real_stage_targets_outside_the_box_are_rejected():
    s = _session(motion=True)
    try:
        chain = s.telemetry[0].simulated_chain
        t = s.controller.move_stage(-5000.0, 0.0)  # e.g. a vessel-map click in the simulator's frame
        assert t.wait(5) is CommandStatus.REJECTED and "outside" in (t.record.message or "")
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes)  # nothing was written to the drives
    finally:
        s.shutdown()


def test_cancel_all_stops_a_running_real_move():
    s = _session(motion=True)
    try:
        chain = s.telemetry[0].simulated_chain
        chain.moves_complete = False
        t = s.controller.move_stage(0.0, 5000.0)
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 13)
        s.core.router.cancel_all()  # the new UI's "Stop all"
        assert t.wait(5) is CommandStatus.CANCELLED
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 0)
    finally:
        s.shutdown()


def test_relative_moves_on_the_physical_stage_are_refused_not_clamped():
    from openevos.acquisition.controller import CommandError

    s = _session(motion=True)
    try:
        chain = s.telemetry[0].simulated_chain
        with pytest.raises(CommandError, match="outside"):
            s.controller.move_stage_by(-100.0, 0.0)  # from 0: the simulator would clamp; the real stage refuses
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes)
        t = s.controller.move_stage_by(100.0, 0.0)
        assert t.wait(10) is CommandStatus.SUCCEEDED
        assert chain.values[("X", "APOS")] == 227  # 0.1 mm
    finally:
        s.shutdown()


def test_new_ui_shows_the_physical_stage_panel_and_diagnostics(qtbot=None):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _session(motion=True)
    try:
        model = SessionModel(s)
        win = MainWindow(model)
        hw = win.pages["hardware"]
        assert hw.phys_step.count() == 6 and hw.phys_step.currentText() == "0.1 mm"
        hw._phys_jog(1, 0)
        assert _wait(lambda: s.telemetry[0].simulated_chain.values[("X", "APOS")] == 227)
        hw._phys_stop()
        assert _wait(lambda: s.telemetry[0].stops_sent >= 1)
        badge = win.mode_badge.text if isinstance(win.mode_badge.text, str) else win.mode_badge.text()
        # the drive emulator is named as such (audit APP-02), never PHYSICAL
        assert "diagnostics" in win.pages and badge == "SIMULATION + EMULATED DRIVES"
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


def test_hold_to_move_runs_to_the_box_edge_and_release_stops(qtbot=None):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _session(motion=True)
    try:
        win = MainWindow(SessionModel(s))
        hw = win.pages["hardware"]
        chain = s.telemetry[0].simulated_chain
        chain.moves_complete = False  # the move keeps running until it is stopped
        hw.phys_hold_buttons["up"].pressed.emit()
        edge = round(hw._phys_box["y"][1] / 1000.0 * tp.COUNTS_PER_MM["Y"])
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 13 and abs(chain.values[("Y", "CPOS")] - edge) <= 1)
        assert chain.values[("X", "CPOS")] == 0  # only the held axis moves
        hw.phys_hold_buttons["up"].released.emit()
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 0)  # controlled stop on release
        assert not s.core.router.stop_latched
        chain.moves_complete = True
        hw.phys_goto_x.setValue(2.0)
        hw.phys_goto_y.setValue(0.0)
        hw._phys_goto()
        assert _wait(lambda: abs(chain.values[("X", "APOS")] - round(2.0 * tp.COUNTS_PER_MM["X"])) <= 1)
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


_SYN_X = (-4000, -4000 + round(119.1 * 2880 / 1.27))  # synthetic calibration (not the unit's values)
_SYN_Y = (-4000, -4000 + round(78.1 * 2880 / 1.27))


def _cal(tmp_path, x=_SYN_X, y=_SYN_Y):
    import json

    p = tmp_path / "led-application-stepd-20990101T000000-parsed.json"
    p.write_text(json.dumps({"motor_base": {"XAxisPositionLimits.lMin": x[0], "XAxisPositionLimits.lMax": x[1],
                                            "YAxisPositionLimits.lMin": y[0], "YAxisPositionLimits.lMax": y[1]}}),
                 encoding="utf-8")
    return p, x, y


def test_calibrated_travel_box_and_fallbacks(tmp_path):
    from openevos.backends.technosoft.telemetry import (
        CONTROL_BOX_UM,
        calibrated_travel_box,
        um_from_counts,
    )

    p, x, y = _cal(tmp_path)
    box, why = calibrated_travel_box(p)
    assert why == "" and box["x"] == (0.0, round(um_from_counts("X", x[1]) - 2000.0, 1))
    assert box["y"][1] == round(um_from_counts("Y", y[1]) - 2000.0, 1)
    bad, _x, _y = _cal(tmp_path, x=(-4000, 100000))
    assert calibrated_travel_box(bad)[0] is None
    s = build_sim_session(ThreadClock(), fast=True,
                          telemetry={"technosoft": {"port": "sim", "motion": True, "calibration": str(bad)}})
    try:
        assert s.telemetry[0].travel_box_um == {"x": CONTROL_BOX_UM, "y": CONTROL_BOX_UM}  # fallback: 22 mm box
    finally:
        s.shutdown()


def test_wide_moves_need_the_calibrated_limits_in_the_drives(tmp_path):
    p, x, _y = _cal(tmp_path)
    # the service's own reload after homing (ADR-0056 amendment) is off here: this test is about the refusal
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.02, "motion": True, "calibration": str(p), "limits_reload": False}})
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        chain = s.telemetry[0].simulated_chain
        t = s.controller.move_stage(30000.0, 0.0)  # 30 mm: inside the PC box, beyond the power-up drive limits
        assert t.wait(10) is CommandStatus.FAILED and "Gate 7" in (t.record.message or "")
        assert all(w in tp.ALLOWED_REQUESTS for w in chain.writes)  # refused before writing anything
        chain.values[("X", "LPOSMIN")], chain.values[("X", "LPOSMAX")] = x  # as after Gate 7
        assert _wait(lambda: s.core.store.get("motor.stage").attr("limits_counts")[0] == x)
        t = s.controller.move_stage(30000.0, 0.0)
        assert t.wait(10) is CommandStatus.SUCCEEDED, t.record.message
        assert chain.values[("X", "APOS")] == round(30.0 * 2880 / 1.27)
    finally:
        s.shutdown()


def test_the_service_loads_the_calibrated_limits_itself_after_homing(tmp_path):
    """ADR-0056 amendment: with the reload on (the default), a drive on its power-up limits at home gets the
    calibrated limits from the service, and the same 30 mm move then succeeds without a manual Gate 7."""
    p, x, y = _cal(tmp_path)
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.02, "motion": True, "calibration": str(p)}})
    try:
        chain = s.telemetry[0].simulated_chain
        assert _wait(lambda: (chain.values[("X", "LPOSMIN")], chain.values[("X", "LPOSMAX")]) == x)
        assert (chain.values[("Y", "LPOSMIN")], chain.values[("Y", "LPOSMAX")]) == y
        assert _wait(lambda: s.core.store.get("motor.stage").attr("limits_counts")[0] == x)
        t = s.controller.move_stage(30000.0, 0.0)
        assert t.wait(10) is CommandStatus.SUCCEEDED, t.record.message
    finally:
        s.shutdown()


def test_readiness_matches_the_gate_evidence():
    assert readiness_for("XY stage").readiness is Readiness.VERIFIED_LIMITED
    for sub in ("Focus drive", "Filter-cube carriage", "LED board", "Calibration record (lico)"):
        assert readiness_for(sub).readiness is Readiness.READ_ONLY, sub
    assert readiness_for("Objective turret").readiness is Readiness.LOCKED


def test_realtime_sim_stage_travels_and_hold_release_stops_part_way():
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.02, "motion": True, "sim_realtime": True}})
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        chain = s.telemetry[0].simulated_chain
        assert chain.speed_counts_s and chain.speed_counts_s > 10000
        s.controller.move_stage(20000.0, 0.0, timeout_s=15.0)  # 20 mm: ~2.4 s at the drives' speed
        assert _wait(lambda: 0 < chain.values[("X", "APOS")] < round(20 * tp.COUNTS_PER_MM["X"]), 2.0)
        s.core.router.stop_device(s.profile.stage_device)  # like releasing a hold button
        assert _wait(lambda: chain.values[("X", "ESTATEV")] == 0)
        stopped = chain.values[("X", "APOS")]
        assert 0 < stopped < round(20 * tp.COUNTS_PER_MM["X"])  # stopped part way, not at the target
        time.sleep(0.3)
        assert chain.values[("X", "APOS")] == stopped  # and stays there
    finally:
        s.shutdown()


def test_jog_after_hold_never_moves_the_other_axis_back(qtbot=None):
    """2026-11-13: X/Y seemed to swap. The untouched axis got a stale telemetry position as its target."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 1.0, "motion": True, "sim_realtime": True}})  # slow poll: stale store
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        win = MainWindow(SessionModel(s))
        hw = win.pages["hardware"]
        chain = s.telemetry[0].simulated_chain
        hw.phys_hold_buttons["up"].pressed.emit()
        time.sleep(0.6)
        hw.phys_hold_buttons["up"].released.emit()
        hw._phys_jog(1, 0)  # pressed while Y is still stopping: must be ignored, not sent with a stale Y
        assert "ignored" in hw.phys_msg.text() or chain.values[("Y", "ESTATEV")] == 0
        assert _wait(lambda: hw._phys_ticket.status.is_terminal)
        y_stopped = chain.values[("Y", "APOS")]
        assert y_stopped > 1000
        hw.phys_step.setCurrentIndex(2)  # 1 mm
        hw._phys_jog(1, 0)
        assert _wait(lambda: chain.values[("X", "APOS")] >= round(1.0 * tp.COUNTS_PER_MM["X"]) - 1)
        assert _wait(lambda: hw._phys_ticket.status.is_terminal)
        assert abs(chain.values[("Y", "APOS")] - y_stopped) <= 1  # Y untouched
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


def test_capture_joystick_holds_and_releases_on_the_physical_stage(qtbot=None):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.05, "motion": True, "sim_realtime": True}})
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        win = MainWindow(SessionModel(s))
        cap = win.pages["capture"]
        chain = s.telemetry[0].simulated_chain
        cap.joy.jog.emit(0.1, -0.9)  # knob pushed up = Y+ (away)
        cap.joy.jog.emit(0.1, -0.9)  # repeated ticks while held: still one move
        assert _wait(lambda: chain.values[("Y", "APOS")] > 1000)
        assert chain.values[("X", "APOS")] == 0
        cap.joy.released.emit()
        assert _wait(lambda: chain.values[("Y", "ESTATEV")] == 0)
        y = chain.values[("Y", "APOS")]
        time.sleep(0.3)
        assert chain.values[("Y", "APOS")] == y  # stopped
        assert not cap.jog_speed.isEnabled()
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


def test_new_direction_right_after_release_is_not_refused_as_moving(qtbot=None):
    """2026-11-13: after releasing, the store still said MOVING for up to a poll, so the next push was refused."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 3.0, "motion": True, "sim_realtime": True}})  # very slow poll
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        win = MainWindow(SessionModel(s))
        cap = win.pages["capture"]
        chain = s.telemetry[0].simulated_chain
        cap.joy.jog.emit(0.0, -0.9)  # Y+
        assert _wait(lambda: chain.values[("Y", "APOS")] > 1000)
        cap.joy.released.emit()
        hw = win.pages["hardware"]
        assert _wait(lambda: hw._phys_ticket.status.is_terminal)
        assert _wait(lambda: s.core.store.get("motor.stage").activity.value == "idle", 1.0)  # not after a 3 s poll
        cap.joy.jog.emit(0.9, 0.0)  # X+ straight away
        assert _wait(lambda: chain.values[("X", "APOS")] > 1000, 3.0)
        cap.joy.released.emit()
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


def test_press_while_stopping_is_queued_then_runs(qtbot=None):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.05, "motion": True, "sim_realtime": True}})
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        win = MainWindow(SessionModel(s))
        hw = win.pages["hardware"]
        chain = s.telemetry[0].simulated_chain
        hw._phys_hold_start(0, 1)
        assert _wait(lambda: chain.values[("Y", "APOS")] > 1000)
        hw._phys_hold_end()
        hw._phys_hold_start(1, 0)  # immediately, while Y is still stopping
        end = time.monotonic() + 3
        while time.monotonic() < end and chain.values[("X", "APOS")] < 1000:
            app.processEvents()
            time.sleep(0.02)
        assert chain.values[("X", "APOS")] >= 1000  # the queued press ran once Y was at rest
        hw._phys_hold_end()
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


def test_joystick_direction_mapping():
    from openevos.ui.pages.capture import joystick_direction
    assert joystick_direction(0.1, 0.1) is None
    assert joystick_direction(0.9, 0.0) == (1, 0) and joystick_direction(-0.9, 0.1) == (-1, 0)
    assert joystick_direction(0.0, -0.9) == (0, 1) and joystick_direction(0.1, 0.9) == (0, -1)  # up = Y+
    # no diagonals (ADR-0040, plan v3 ground rule): the dominant axis only
    assert joystick_direction(0.7, -0.6) == (1, 0) and joystick_direction(-0.5, 0.6) == (0, -1)
    assert joystick_direction(0.9, -0.2) == (1, 0)


def test_a_go_to_changing_x_and_y_moves_x_then_y():
    """Y-axis plan v3 ground rule: X and Y never move together; one go-to runs as two legs, X first."""
    s = _session(motion=True)
    try:
        chain = s.telemetry[0].simulated_chain
        t = s.controller.move_stage(1000.0, 2000.0)
        assert t.wait(10) is CommandStatus.SUCCEEDED, t.record.message
        written = [w for w in chain.writes if w not in tp.ALLOWED_REQUESTS]
        assert written == [tp.move_target("X", 2268), tp.move_request("X"), *tp.stop_frames("X"),
                           tp.move_target("Y", 4535), tp.move_request("Y"), *tp.stop_frames("Y")]
        assert not s.telemetry[0].monitor.latched
    finally:
        s.shutdown()


class _SettlingX(SimulatedDriveChain):
    """X reports motion complete 4 counts short of its target and gets there 0.15 s after its stop at rest, as the
    real X did after its go-to leg on COM3 (2026-11-16: -19 in the move, -1, then 0)."""

    creep_at = None

    def _motion(self, data):
        super()._motion(data)
        if data == tp.stop_frames("X")[2] and self.values[("X", "CPOS")] != 0:  # the stop at rest after X's leg
            self.values[("X", "APOS")] -= 4
            self.creep_at = time.monotonic() + 0.15

    def _advance(self):
        super()._advance()
        if self.creep_at is not None and time.monotonic() >= self.creep_at:
            self.values[("X", "APOS")] += 4
            self.creep_at = None


def test_a_go_to_lets_x_settle_before_the_y_leg_takes_its_reference():
    """X still converging when its leg ends is not drift: Y's leg starts only after the monitor's settle time, with
    the encoders sampled throughout, so X's reference is where it came to rest."""
    s = _session(motion=True)
    try:
        svc = s.telemetry[0]
        svc.simulated_chain.__class__ = _SettlingX
        t0 = time.monotonic()
        t = s.controller.move_stage(1000.0, 2000.0)
        assert t.wait(10) is CommandStatus.SUCCEEDED, t.record.message
        took = time.monotonic() - t0
        time.sleep(0.3)  # samples after the go-to: X's reference holds
        assert not svc.monitor.latched, svc.monitor.refusal()
        assert svc.monitor.ref["X"] == 2268
        assert took >= 0.5  # the settle between the legs
    finally:
        s.shutdown()


def test_a_go_to_stops_before_y_when_y_drifts_during_the_x_leg():
    s = _session(motion=True)
    try:
        svc = s.telemetry[0]
        chain = svc.simulated_chain
        chain.drift_on_move = {"Y": -15}  # Y's count jumps while X moves, beyond the tracked-knock bound (10)
        t = s.controller.move_stage(1000.0, 2000.0)
        assert t.wait(10) is CommandStatus.FAILED
        assert "idle_encoder_drift" in t.record.message
        assert not any(w in (tp.move_target("Y", 4535), tp.move_request("Y")) for w in chain.writes)  # Y never sent
        assert svc.monitor.latched and svc.monitor.latch.axis == "Y"
    finally:
        s.shutdown()


def test_joystick_diagonal_push_moves_one_axis_and_reversal_while_held(qtbot=None):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.05, "motion": True, "sim_realtime": True}})
    try:
        assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
        win = MainWindow(SessionModel(s))
        cap = win.pages["capture"]
        chain = s.telemetry[0].simulated_chain
        cap.joy.jog.emit(0.7, -0.6)  # up-right, mostly right: X+ only (no diagonals, ADR-0040)
        assert _wait(lambda: chain.values[("X", "APOS")] > 2000)
        assert chain.values[("Y", "APOS")] == 0
        cap.joy.jog.emit(-0.9, 0.0)  # dragged to the left while held: stop, then X- once at rest
        x_peak = chain.values[("X", "APOS")]
        end = time.monotonic() + 4
        while time.monotonic() < end and chain.values[("X", "APOS")] >= x_peak:
            app.processEvents()
            time.sleep(0.02)
        assert chain.values[("X", "APOS")] < x_peak  # now moving left
        cap.joy.released.emit()
        assert _wait(lambda: chain.values[("X", "ESTATEV")] == 0 and chain.values[("Y", "ESTATEV")] == 0)
        win.close()
        app.processEvents()
    finally:
        s.shutdown()


def test_speed_box_sets_the_drive_speed_only_with_speed_control(qtbot=None):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    for enabled in (False, True):
        s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
            "port": "sim", "poll_interval_s": 0.05, "motion": True, "speed_control": enabled}})
        try:
            assert _wait(lambda s=s: (st := s.core.store.get("motor.stage")) is not None
                         and st.observed_position is not None)
            win = MainWindow(SessionModel(s))
            cap = win.pages["capture"]
            chain = s.telemetry[0].simulated_chain
            assert cap.jog_speed.isEnabled() is enabled
            assert cap.jog_speed.current() == "medium"  # the saved default
            if enabled:
                assert cap.lbl_speed.text() == "4 mm/s for every stage move"
                cap.jog_speed.buttons["slow"].click()
                assert cap.lbl_speed.text() == "1 mm/s for every stage move"
                assert s.settings.basic.stage_speed == "slow"  # remembered for the next launch
            else:
                assert cap.lbl_speed.text().startswith("Fixed at the drives' power-up speed")
            t = s.controller.move_stage(1000.0, 0.0)
            assert t.wait(10).value == "succeeded", t.record.message
            want = tp.speed_raw("slow") if enabled else tp.POWER_UP_SPEED_RAW
            assert chain.values[("X", "CSPDCMD")] == want
            assert (tp.speed_write("X", "slow") in chain.writes) is enabled
            win.close()
            app.processEvents()
        finally:
            s.shutdown()
        if enabled:  # restored to the power-up speed on exit
            assert chain.values[("X", "CSPDCMD")] == tp.POWER_UP_SPEED_RAW


def test_closing_during_a_long_move_stops_the_stage_and_exits_fast():
    s = build_sim_session(ThreadClock(), fast=True, telemetry={"technosoft": {
        "port": "sim", "poll_interval_s": 0.05, "motion": True, "sim_realtime": True}})
    assert _wait(lambda: (st := s.core.store.get("motor.stage")) is not None and st.observed_position is not None)
    chain = s.telemetry[0].simulated_chain
    s.controller.move_stage(20000.0, 0.0, timeout_s=15.0)  # ~2.4 s of travel
    assert _wait(lambda: chain.values[("X", "APOS")] > 1000)
    t0 = time.monotonic()
    s.shutdown()
    assert time.monotonic() - t0 < 2.0  # did not wait for the move to finish
    assert chain.values[("X", "ESTATEV")] == 0  # controlled stop sent on the way out
    assert chain.values[("X", "APOS")] < round(20 * tp.COUNTS_PER_MM["X"])
    assert not any(t.is_alive() for t in __import__("threading").enumerate() if t.name == "technosoft-telemetry")
