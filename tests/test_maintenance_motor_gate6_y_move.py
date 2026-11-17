"""Motor Gate 6 (ADR-0021) against a fake drive chain that models a Y move. No real hardware."""

from __future__ import annotations

import pytest
from test_maintenance_motor_gate4_x_move import FakeX

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate6_y_move as g6
from openevos.safety.firmware import REVIEWED_MAINTENANCE

PORT = mg1.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", None)
Y_STOP = [f for _n, f in g6.Y_STOP]


class FakeY(FakeX):
    """FakeX's model, re-pointed at axis 2: Y frames drive Y variables; X writes are not expected."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.axis = "Y"
        self.by_frame.update({r.frame: r for r in g6.YR.values()})
        for r in g6.YR.values():
            self.v.setdefault((r.axis, r.var), {"LCONVERGEBAND": kw.get("band", 8), "SRH": 0x0200}.get(r.var, 0))
        self.v[("Y", "SRH")] = 0x0200
        self.v[("Y", "LCONVERGEBAND")] = kw.get("band", 8)

    def _x(self, var, val=None):  # the parent's helper, but for Y
        if val is not None:
            self.v[("Y", var)] = val
        return self.v[("Y", var)]

    def write(self, data):
        data = bytes(data)
        mapping = {g6.Y1_TARGET_OUT: g4.M1_TARGET_OUT, g6.Y2_TARGET_HOME: g4.M2_TARGET_HOME,
                   g6.Y3_ESTATE_MOVE: g4.M3_ESTATE_MOVE, Y_STOP[2]: g4.STOP[2][1]}
        if data in mapping:  # reuse the parent's state model with the X-equivalent frame
            if data == g6.Y1_TARGET_OUT:
                self.writes.append(data)
                self._x("CPOSCMD", g6.TARGET)
                self._pending = b"O"
                return len(data)
            self.writes.append(data)
            n = super().write(mapping[data])
            self.writes.pop()  # keep only the real Y frame in the record
            return n
        return super().write(data)

    def write_frames(self):
        return [w for w in self.writes if w in g6.ALLOWED_WRITES]


def go(fake=None, **kw):
    fake = fake or FakeY()
    args = {"send": True, "confirm": "MOVE Y 12MM AND BACK ON COM3", "on_for_2_min": True,
            "stage_empty_watching": True}
    args.update(kw)
    res = g6.run(find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0, sleep=lambda s: None,
                 **args)
    return res, fake


def test_frames():
    for f in (g6.Y1_TARGET_OUT, g6.Y2_TARGET_HOME, g6.Y3_ESTATE_MOVE, *Y_STOP):
        assert sum(f[:-1]) & 0xFF == f[-1] and f[2] == 0x20  # axis 2 only
    assert g6.Y1_TARGET_OUT[5:9] == bytes.fromhex("6A 4D 00 00")  # 27,213 in vendor order
    assert abs(g6.TARGET / 2880 * 1.27 - 12.0) < 0.001
    assert REVIEWED_MAINTENANCE["motor.y_move_12mm_and_back"]["adr"] == "ADR-0021"


def test_no_x_or_other_writes():
    for bad in (g4.M1_TARGET_OUT, g4.M3_ESTATE_MOVE, bytes.fromhex("08 00 20 25 B6 00 00 00 00 FB")):
        with pytest.raises(g6.MotorGate6Refused):
            g6.assert_allowed(bad)
    link = g4._Link(FakeY(), g4.MotorGate4Result("x", "", "", ""), lambda: 0.0, lambda s: None,
                    allowed_writes=g6.ALLOWED_WRITES, allowed_reads=g6.ALLOWED_READS)
    with pytest.raises(g4.MotorGate4Refused):
        link.write("x move", g4.M3_ESTATE_MOVE)
    with pytest.raises(g4.MotorGate4Refused):
        link.read(g4.XR["APOS"])


def test_refusal_and_dry_run():
    res, fake = go(confirm="MOVE X 1MM AND BACK ON COM3")
    assert res.outcome == "refused" and fake.writes == []
    res, fake = go(send=False, on_for_2_min=False, stage_empty_watching=False, confirm="")
    assert res.outcome == "dry_run" and fake.writes == []


def test_pass():
    res, fake = go()
    assert res.outcome == "pass", (res.message, res.final)
    assert fake.write_frames() == [g6.Y1_TARGET_OUT, g6.Y3_ESTATE_MOVE, g6.Y2_TARGET_HOME, g6.Y3_ESTATE_MOVE,
                                   *Y_STOP]
    assert res.legs["out"]["APOS"] == g6.TARGET and res.legs["home"]["APOS"] == 0


def test_y_not_at_home_refused():
    fake = FakeY()
    fake.v[("Y", "APOS")] = 3
    res, f = go(fake)
    assert res.outcome == "baseline_refused" and "Y not exactly at home" in res.message and f.write_frames() == []


def test_timeout_sends_one_y_stop():
    res, fake = go(FakeY(stuck=True))
    assert res.outcome == "fault_stopped"
    assert fake.write_frames() == [g6.Y1_TARGET_OUT, g6.Y3_ESTATE_MOVE, *Y_STOP]
