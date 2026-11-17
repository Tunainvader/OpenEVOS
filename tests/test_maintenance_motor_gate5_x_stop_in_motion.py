"""Motor Gate 5 (ADR-0020) against a fake X drive that models a stop arriving mid-move. No real hardware."""

from __future__ import annotations

import pytest
from test_maintenance_motor_gate4_x_move import FakeX

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate3_stop as g3
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate5_x_stop_in_motion as g5
from openevos.safety.firmware import REVIEWED_MAINTENANCE

PORT = mg1.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", None)
STOP = [f for _n, f in g3.WRITES]


class FakeFar(FakeX):
    """Adds T1 (22,680). A stop during that move leaves X at `stop_at`; `finish_first` lets it complete."""

    def __init__(self, *, stop_at=9300, finish_first=False, stop_never_settles=False, **kw):
        super().__init__(**kw)
        self.stop_at, self.finish_first, self.never = stop_at, finish_first, stop_never_settles
        self.far_moving = False

    def write(self, data):
        data = bytes(data)
        if data == g5.T1_TARGET_FAR:
            self.writes.append(data)
            self._x("CPOSCMD", g5.FAR_TARGET)
            self._pending = b"O"
            return len(data)
        if data == g4.M3_ESTATE_MOVE and self._x("CPOSCMD") == g5.FAR_TARGET:
            n = super().write(data)
            self.far_moving, self.remaining = True, 0  # far move progresses only via the stop below
            if self.finish_first:
                self._x("APOS", g5.FAR_TARGET)
                self._x("ESTATEV", 13)
                self._x("SRH", 0x0200)
                self.far_moving = False
            return n
        if data == g3.W3_ESTATE_STOP and self.far_moving:
            self.writes.append(data)
            self._pending = b"O"
            self.far_moving = False
            self._x("APOS", self.stop_at)
            self._x("CPOS", self.stop_at)
            self._x("ESTATEV", 0)
            self._x("SRH", 0 if self.never else 0x0200)
            return len(data)
        return super().write(data)

    def write_frames(self):
        return [w for w in self.writes if w in g5.ALLOWED_WRITES]


def go(fake=None, **kw):
    fake = fake or FakeFar()
    args = {"send": True, "confirm": "STOP X DURING 10MM MOVE ON COM3", "on_for_2_min": True,
            "stage_empty_watching": True}
    args.update(kw)
    res = g5.run(find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0, sleep=lambda s: None,
                 **args)
    return res, fake


def test_frame_and_allowlist():
    f = g5.T1_TARGET_FAR
    assert sum(f[:-1]) & 0xFF == f[-1] and f[5:9] == bytes.fromhex("58 98 00 00")
    for bad in (bytes.fromhex("08 00 10 25 D4 10 00 00 00 19"), bytes.fromhex("04 00 10 00 02 16")):
        with pytest.raises(g5.MotorGate5Refused):
            g5.assert_allowed(bad)
    assert REVIEWED_MAINTENANCE["motor.x_stop_during_10mm_move"]["adr"] == "ADR-0020"


def test_link_refuses_writes_outside_gate5():
    link = g5._Link5(FakeFar(), g4.MotorGate4Result("x", "", "", ""), lambda: 0.0, lambda s: None)
    with pytest.raises(g4.MotorGate4Refused):
        link.write("bad", bytes.fromhex("08 00 10 25 D4 10 00 00 00 19"))


def test_refusals_and_dry_run():
    res, fake = go(confirm="MOVE X 1MM AND BACK ON COM3")
    assert res.outcome == "refused" and fake.writes == []
    res, fake = go(send=False, on_for_2_min=False, stage_empty_watching=False, confirm="")
    assert res.outcome == "dry_run" and fake.writes == []


def test_pass_stop_mid_move_then_return():
    res, fake = go()
    assert res.outcome == "pass", res.message
    leg = res.legs["stop_in_motion"]
    assert leg["result"] == "interrupted" and leg["APOS"] == 9300 and leg["ESTATEV"] == 0
    assert fake.write_frames() == [g5.T1_TARGET_FAR, g4.M3_ESTATE_MOVE, *STOP,
                                   g4.M2_TARGET_HOME, g4.M3_ESTATE_MOVE, *STOP]
    assert res.legs["home"]["APOS"] == 0 and all(res.final["checks"].values())


def test_move_finished_first_is_not_a_pass():
    res, _ = go(FakeFar(finish_first=True))
    assert res.outcome == "not_interrupted" and res.legs["stop_in_motion"]["result"] == "not_interrupted"
    assert res.legs["home"]["APOS"] == 0


def test_stop_not_settling_sends_nothing_more():
    res, fake = go(FakeFar(stop_never_settles=True))
    assert res.outcome == "fault_stopped" and "not at rest" in res.message
    assert fake.write_frames() == [g5.T1_TARGET_FAR, g4.M3_ESTATE_MOVE, *STOP]


def test_unexpected_rest_position_is_a_fault_without_return():
    res, fake = go(FakeFar(stop_at=200))
    assert res.outcome == "fault_stopped" and g4.M2_TARGET_HOME not in fake.write_frames()


def test_limits_must_cover_ten_mm():
    fake = FakeFar()
    fake.v[("X", "LPOSMAX")] = 20000
    res, f = go(fake)
    assert res.outcome == "baseline_refused" and f.write_frames() == []
