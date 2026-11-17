"""F cube carriage in the app (ADR-0051): a cube channel drives F to its cube before the light goes on.

Proves, on the simulated drive chain and a fake LED board only:
* the app's F frames are byte-identical to the ones Motor Gates 11 and 12 sent on the instrument;
* F moves only with motor control and carriage control, only from the reviewed stored positions/limits, with the
  drive's own GOTO_CUBE / GOTO_MIRR; a fault or timeout sends F's own STOP;
* the LED device switches everything off before the move, then requires the board's cube sensor to agree;
* the app session links the two only when the real LED board and real motor control are both present.
"""

from __future__ import annotations

import time
from typing import ClassVar

import pytest

from openevos.backends.ledboard import illumination as il
from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.maintenance import motor_gate11_f_cube as g11
from openevos.maintenance import motor_gate12_f_mirror as g12
from tests.test_ledboard_illumination import FakeBoard, make, run, writes


def test_f_frames_are_the_gate_11_and_12_frames():
    assert tp.F_GOTO_CUBE_FRAME == g11.GOTO_FRAME and tp.F_STOP_FRAME == g11.STOP_FRAME
    assert tp.F_GOTO_MIRROR_FRAME == g12.GOTO_MIRR_FRAME
    assert all(tp.f_slot_target(n) == g11.slot_frame(n) for n in tp.F_SLOTS)
    assert tp.F_EXPECTED_POS == {**{f"slot{n}": p for n, p in g11.EXPECTED_CUBE_POS.items()},
                                 "mirror": g12.EXPECTED_MIRROR_POS}
    assert tp.F_EXPECTED_LIMITS == g12.EXPECTED_LIMITS
    for name in ("ISTATUSFLAGS", "MER", "LCONVERGEBAND", "LCUBE1POS", "LCUBE4POS", "LMIRROR1POS"):
        assert (tp.VARS["F"][name].bits, tp.VARS["F"][name].address) == (g11.FR[name].bits, g11.FR[name].address)
    for bad in ("slot0", "slot5", "home", "slotx"):
        with pytest.raises(tp.ProtocolRefused):
            tp.f_move_frames(bad)
    assert all(tp.is_motion_write(f) for f in (tp.F_GOTO_CUBE_FRAME, tp.F_GOTO_MIRROR_FRAME, tp.F_STOP_FRAME))
    assert not tp.is_motion_write(bytes.fromhex("08 00 50 25 b7 00 05 00 00 39"))  # slot 5: not reviewed


def _service(chain, carriage=True, motion=True):
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.05, motion=motion)
    svc.carriage_control = carriage
    svc.start()
    return svc


def _f_writes(chain):
    return [w for w in chain.writes if len(w) > 2 and w[2] == tp.F_ID << 4 and w not in tp.ALLOWED_REQUESTS]


def test_carriage_moves_to_a_slot_and_the_mirror():
    chain = SimulatedDriveChain(allow_motion=True)
    svc = _service(chain)
    try:
        ok, msg = svc.move_carriage("slot3")
        assert ok, msg
        assert chain.values[("F", "APOS")] == 68000
        assert _f_writes(chain) == [g11.slot_frame(3), g11.GOTO_FRAME]
        ok, msg = svc.move_carriage("mirror")
        assert ok and chain.values[("F", "APOS")] == 128000
        n = len(_f_writes(chain))
        ok, msg = svc.move_carriage("mirror")  # already there: nothing written
        assert ok and "already" in msg and len(_f_writes(chain)) == n
    finally:
        svc.stop()


def test_carriage_refused_without_control_or_with_unreviewed_positions():
    chain = SimulatedDriveChain(allow_motion=True)
    svc = _service(chain, carriage=False)
    try:
        assert svc.move_carriage("slot3")[0] is False
    finally:
        svc.stop()
    chain = SimulatedDriveChain({("F", "LCUBE3POS"): 70000}, allow_motion=True)
    svc = _service(chain)
    try:
        ok, msg = svc.move_carriage("slot3")
        assert not ok and "stored cube/mirror positions" in msg
        chain.values[("F", "LCUBE3POS")] = 68000
        chain.values[("F", "IINITONCEDONE")] = 0
        ok, msg = svc.move_carriage("slot3")
        assert not ok and "not homed" in msg
        assert _f_writes(chain) == []
    finally:
        svc.stop()


def test_carriage_timeout_sends_f_stop(monkeypatch):
    monkeypatch.setattr(tm, "CARRIAGE_TIMEOUT_MARGIN_S", 0.3)
    monkeypatch.setattr(tm, "CARRIAGE_SPEED_COUNTS_S_MIN", 10**9)
    chain = SimulatedDriveChain(allow_motion=True)
    chain.f_moves_complete = False
    svc = _service(chain)
    try:
        t0 = time.monotonic()
        ok, msg = svc.move_carriage("slot4")
        assert not ok and "in time" in msg and "F stop sent" in msg and time.monotonic() - t0 < 5
        assert _f_writes(chain)[-1] == g11.STOP_FRAME
    finally:
        svc.stop()


# ------------------------------------------------------------------ LED device + carriage
class Carriage:
    """Fake stage service: moving F changes the cube the board's sensor reports."""

    SLOT_CUBE: ClassVar[dict[str, str]] = {"slot1": "RFP", "slot2": "NONE", "slot3": "DAPI", "slot4": "GFP"}

    def __init__(self, board, ok=True, cube_after=None):
        self.board, self.ok, self.cube_after = board, ok, cube_after
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, dest):
        self.calls.append((dest, self.board.bf, self.board.epi))  # lights at the moment F is asked to move
        if not self.ok:
            return False, "F not moved: F is not idle (state 2)"
        self.board.cube = self.cube_after or self.SLOT_CUBE[dest]
        return True, f"F at {dest}"


def test_cube_click_moves_f_then_lights_the_cube():
    board = FakeBoard(cube="RFP")
    dev, _ = make(board)
    dev.carriage = car = Carriage(board)
    dev.connect()
    run(dev, "trans", True)
    assert board.bf == 1
    ctx = run(dev, "cube2", True)  # GFP: slot 4
    assert ctx.status == "ok", ctx.message
    assert car.calls == [("slot4", 0, 0)]  # both lights off while F moves
    assert (board.bf, board.epi) == (0, 1)
    assert run(dev, "cube2", True).status == "ok" and len(car.calls) == 1  # already in the path: F not moved
    ctx = run(dev, "cube1", True)  # DAPI: slot 3, dark during the move
    assert ctx.status == "ok" and car.calls[-1] == ("slot3", 0, 0) and board.epi == 1
    dev.disconnect()


def test_carriage_failure_or_wrong_cube_leaves_the_instrument_dark():
    board = FakeBoard(cube="RFP")
    dev, _ = make(board)
    dev.carriage = Carriage(board, ok=False)
    dev.connect()
    ctx = run(dev, "cube2", True)
    assert ctx.status == "failed" and ctx.code == "CARRIAGE_FAILED" and "not idle" in ctx.message
    assert (board.bf, board.epi) == (0, 0)
    assert dev.channel == "cube2" and not dev.enabled  # the UI's Light toggle follows: this channel, dark
    dev.carriage = Carriage(board, cube_after="DAPI")  # the sensor disagrees after the move
    ctx = run(dev, "cube2", True)
    assert ctx.status == "failed" and ctx.code == "CUBE_NOT_IN_PATH" and board.epi == 0
    n = len(writes(board))
    dev.carriage = car = Carriage(board)
    ctx = run(dev, "cube3", True)  # TxRed: not on this instrument
    assert ctx.status == "failed" and "no such cube" in ctx.message and car.calls == []
    assert all(w[1:] == (0, 0) or w == (0x43, 0, 0) for w in writes(board)[n:]) and board.epi == 0
    dev.disconnect()


def test_cube_slider_reaches_zero_intensity_and_back():
    from openevos.instrument.settings import light_slider_split

    assert light_slider_split(0, True)[0] == 0.0 and light_slider_split(0, False)[0] == 0.0
    board = FakeBoard(cube="GFP")
    dev, _ = make(board)
    dev.connect()
    assert run(dev, "cube2", True, pct=0.0).status == "ok"  # zero intensity: on, at level 0
    assert board.epi == 1 and board.epi_level == 0 and dev.enabled
    assert run(dev, "cube2", True, pct=100.0).status == "ok"  # slide back up: brighter, no re-toggle
    assert board.epi == 1 and board.epi_level == il.EPI_LEVEL_AT_100
    assert run(dev, "cube2", False, pct=100.0).status == "ok" and board.epi == 0
    dev.disconnect()


def test_real_light_session_shows_this_units_cubes_on_their_slots():
    from openevos.acquisition.session import UNIT_CUBES, build_sim_session
    from openevos.backends.ledboard.illumination import LABEL_TO_CUBE, UNIT_CUBE_SLOTS
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import AppSettings

    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), illumination="real",
                          led_hid=FakeBoard())
    try:
        cubes = {c.slot: (c.label, c.installed) for c in s.profile.channels if c.slot.startswith("cube")}
        assert cubes == {"cube1": ("RFP", True), "cube2": ("", False), "cube3": ("DAPI", True),
                         "cube4": ("GFP", True)}
        assert {c.slot for c in s.profile.channels} >= {"trans", "phase"}
        for n, spec in UNIT_CUBES.items():  # each button's slot is the slot the LED device drives F to
            if spec is not None:
                (cube,) = LABEL_TO_CUBE[spec[0].upper()]
                assert UNIT_CUBE_SLOTS[cube] == n
        assert s.rig.light.labels["cube1"] == "RFP"
    finally:
        s.shutdown()
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())  # simulator: unchanged
    try:
        assert s.profile.channel("cube1").label == "DAPI"
    finally:
        s.shutdown()


def test_session_links_the_carriage_only_with_real_light_and_motor_control():
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import AppSettings

    board = FakeBoard(cube="GFP")
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), illumination="real",
                          led_hid=board, telemetry={"technosoft": {"port": "sim", "motion": True}})
    try:
        svc = s.telemetry[0]
        assert svc.carriage_control and s.rig.light.carriage == svc.move_carriage
    finally:
        s.shutdown()
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), illumination="real",
                          led_hid=FakeBoard(), telemetry={"technosoft": {"port": "sim", "motion": False}})
    try:
        assert not s.telemetry[0].carriage_control and s.rig.light.carriage is None
    finally:
        s.shutdown()
