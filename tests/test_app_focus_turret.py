"""Z focus and the objective turret from the app (ADR-0053), on the simulated drive chain only.

Proves: the app builds exactly Motor Gate 18's Z frames and Gate 19 rev 2's turret frames; a focus move is target, move,
stop at rest; a turret change refuses unless the stage is centred, retracts Z to 0 first, accepts only the decreasing
path, and switches the turret off (AXISOFF) on a fault; the session routes the UI's focus and objective controls to the
real drives only with motor control.
"""

from __future__ import annotations

import threading
import time

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.core.models import MoveFocusAbsolute, SetTurretPosition
from openevos.maintenance import motor_gate18_z_move as g18
from openevos.maintenance import motor_gate19_o_turret as g19

CENTRE = {("X", "APOS"): round(59.5 * tp.COUNTS_PER_MM["X"]), ("Y", "APOS"): round(39.0 * tp.COUNTS_PER_MM["Y"])}


class Ctx:
    def __init__(self):
        self.status, self.message, self.code, self.result = None, "", "", None
        self.done = threading.Event()

    def mark_started(self):
        pass

    def report_progress(self, fraction, message=None):
        pass

    def complete(self, result=None, payload=None):
        self.status, self.result = "ok", result
        self.done.set()

    def fail(self, message, error_code=None):
        self.status, self.message, self.code = "failed", message, error_code
        self.done.set()

    def mark_cancelled(self, result=None):
        self.status = "cancelled"
        self.done.set()


def _service(chain):
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=True, sleep=lambda s: time.sleep(0.001))  # noqa: E501
    svc.zo_control = True
    svc.start()
    return svc


def _backend(svc, device_id):
    return next(b for b in svc.backends if b.device_id == device_id)


def _run(svc, device_id, command):
    ctx = Ctx()
    _backend(svc, device_id).start(command, ctx)
    assert ctx.done.wait(10)
    return ctx


def _writes(chain, axis_id):
    return [w for w in chain.writes if len(w) > 2 and w[2] == axis_id << 4 and w not in tp.ALLOWED_REQUESTS]


def test_frames_are_the_gate_18_and_19_frames():
    assert tp.z_target(8200) == g18.Z1_TARGET_OUT and tp.z_target(41000) == g18.Z5_TARGET_OUT
    assert tp.z_target(0) == g18.Z2_TARGET_HOME
    assert tp.Z_MOVE_FRAME == g18.Z3_ESTATE_MOVE and tp.Z_STOP_FRAME == g18.Z4_ESTATE_STOP
    assert all(tp.o_target(n) == g19.target_frame(n) for n in tp.O_POSITIONS)
    assert tp.O_GOTO_FRAME == g19.O_GOTO and tp.O_AXISOFF_FRAME == g19.O_AXISOFF
    assert tp.is_motion_write(tp.z_target(12345)) and not tp.is_motion_write(
        bytes.fromhex("08 00 40 25 B3 FF FF FF FF 9C"))  # -1: below home, refused
    for bad in (-1, 66001, 70000):
        try:
            tp.z_target(bad)
        except tp.ProtocolRefused:
            continue
        raise AssertionError(bad)


def test_focus_move_target_move_stop():
    chain = SimulatedDriveChain(allow_motion=True)
    svc = _service(chain)
    try:
        caps = _backend(svc, "motor.focus").capabilities()
        assert "focus.move_absolute" in caps.supported_commands and caps.axis("z").limits.minimum == 0.0
        z_um = round(tm.um_from_counts("Z", 8200), 3)
        ctx = _run(svc, "motor.focus", MoveFocusAbsolute(device_id="motor.focus", origin="test", z=z_um, unit="um",
                                                          frame="focus"))
        assert ctx.status == "ok", ctx.message
        assert chain.values[("Z", "APOS")] == 8200
        assert _writes(chain, tp.Z_ID) == [tp.z_target(8200), tp.Z_MOVE_FRAME, tp.Z_STOP_FRAME]
        assert abs(_backend(svc, "motor.focus").read_position().get("z") - z_um) < 0.01
    finally:
        svc.stop()


def test_turret_change_retracts_z_then_turns_the_decreasing_way():
    chain = SimulatedDriveChain(CENTRE | {("Z", "APOS"): 8200, ("Z", "CPOS"): 8200}, allow_motion=True)
    chain.o_steps_per_read = 2
    svc = _service(chain)
    try:
        ctx = _run(svc, "motor.objective_turret", SetTurretPosition(device_id="motor.objective_turret", origin="test",
                                                                    position=4))
        assert ctx.status == "ok", ctx.message
        assert chain.values[("Z", "APOS")] == 0  # retracted before the turn
        assert chain.values[("O", "ICURRENTPOSHF")] == 4  # 1 -> 5 -> 4
        assert _writes(chain, tp.O_ID) == [tp.o_target(4), tp.O_GOTO_FRAME]
        assert _backend(svc, "motor.objective_turret").verify_result(
            SetTurretPosition(device_id="motor.objective_turret", origin="test", position=4))[0]
    finally:
        svc.stop()


def test_turret_refused_unless_the_stage_is_centred():
    # stage at its homed corner 0, 0 (the Gate 19 run 1 collision), calibrated limits loaded: "centre the stage"
    chain = SimulatedDriveChain({("X", "LPOSMAX"): 266000, ("Y", "LPOSMAX"): 173000}, allow_motion=True)
    svc = _service(chain)
    try:
        ctx = _run(svc, "motor.objective_turret", SetTurretPosition(device_id="motor.objective_turret", origin="test",
                                                                    position=2))
        assert ctx.status == "failed" and "centre the stage" in ctx.message
        assert _writes(chain, tp.O_ID) == [] and _writes(chain, tp.Z_ID) == []
    finally:
        svc.stop()
    # on the power-up limits the stage cannot reach the centre at all: the refusal names the cause (audit APP-07)
    chain = SimulatedDriveChain(allow_motion=True)
    svc = _service(chain)
    try:
        ctx = _run(svc, "motor.objective_turret", SetTurretPosition(device_id="motor.objective_turret", origin="test",
                                                                    position=2))
        assert ctx.status == "failed" and "limits are not loaded" in ctx.message and "Gate 7" in ctx.message
        assert ctx.code == "turret_precondition"
        assert _writes(chain, tp.O_ID) == [] and _writes(chain, tp.Z_ID) == []
    finally:
        svc.stop()


def test_stuck_turret_gets_axisoff():
    chain = SimulatedDriveChain(CENTRE, allow_motion=True)
    chain.o_stuck = True
    svc = _service(chain)
    try:
        ctx = _run(svc, "motor.objective_turret", SetTurretPosition(device_id="motor.objective_turret", origin="test",
                                                                    position=3))
        assert ctx.status == "failed" and "stuck" in ctx.message
        assert _writes(chain, tp.O_ID)[-1] == tp.O_AXISOFF_FRAME
    finally:
        svc.stop()


def test_session_routes_focus_and_objectives_to_the_drives_only_with_motor_control():
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import AppSettings

    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "motion": True}})
    try:
        assert s.profile.focus_device == "motor.focus" and s.profile.turret_device == "motor.objective_turret"
        assert [o.magnification for o in s.profile.objectives] == [4, 10, 20, 40, 60]
        chain = s.telemetry[0].simulated_chain
        chain.values.update(CENTRE)
        ctl = s.controller
        t = s.change_objective(2)
        from openevos.core.models import CommandStatus

        assert t.wait(15) is CommandStatus.SUCCEEDED
        assert chain.values[("O", "ICURRENTPOSHF")] == 2
        t = ctl.move_focus(500.0)
        assert t.wait(15) is CommandStatus.SUCCEEDED and chain.values[("Z", "APOS")] == tm.counts_from_um("Z", 500.0)
    finally:
        s.shutdown()
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "motion": False}})
    try:
        assert s.profile.focus_device != "motor.focus"
    finally:
        s.shutdown()
