"""Motor Gate 11 (ADR-0035): F carriage to a cube slot, against a fake F drive. No real hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_maintenance_motor_gate4_x_move import FF, NORMAL, PORT, id_reply, reply

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate11_f_cube as g11
from openevos.safety.firmware import REVIEWED_MAINTENANCE

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate11_f_cube_spec.md"


class FakeF:
    """All drives idle/homed; F models GOTO_CUBE (moves `step` counts per APOS read) and STOP."""

    def __init__(self, *, cube=(0, 34000, 68000, 102000), wrong_way=False, step=20000):
        self.writes, self._pending = [], b""
        self.by_frame = {r.frame: r for r in (*g11.FR.values(), *g11.g4.READINESS)}
        self.v = {("F", n): 0 for n in g11.FR}
        self.v.update({("F", "IINITONCEDONE"): 12345, ("F", "LPOSMAX"): 128000, ("F", "LCONVERGEBAND"): 5,
                       ("F", "ISTATUSFLAGS"): 0xD000})
        for i, p in enumerate(cube, 1):
            self.v[("F", f"LCUBE{i}POS")] = p
        self.target, self.slot, self.wrong_way, self.step = None, 0, wrong_way, step

    def inspect(self):
        return {"dcb": {"flags": dict(NORMAL)}, "timeouts": {}}

    def configure(self, baud):
        pass

    def purge_rx(self):
        self._pending = b""

    def close(self):
        pass

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = id_reply()
        elif data in self.by_frame:
            r = self.by_frame[data]
            if r.axis == "F" and r.var == "APOS" and self.target is not None:
                a = self.v[("F", "APOS")]
                d = (self.target - a) if not self.wrong_way else -self.step
                a += max(-self.step, min(self.step, d))
                self.v[("F", "APOS")] = self.v[("F", "CPOS")] = a
                if a == self.target:
                    self.v[("F", "ISTATUSFLAGS")] = 0xD000
                    self.target = None
            val = self.v.get((r.axis, r.var), 12345 if r.var == "IINITONCEDONE" else 0)
            raw = (val & 0xFFFF).to_bytes(2, "big") if r.vtype == "INT" else g2_raw(val)
            self._pending = reply(r.axis_id, r.address, raw)
        else:
            self._pending = b"O"
            if data in [g11.slot_frame(n) for n in (1, 2, 3, 4)]:
                self.slot = [g11.slot_frame(n) for n in (1, 2, 3, 4)].index(data) + 1
            elif data == g11.GOTO_FRAME:
                self.target = self.v[("F", f"LCUBE{self.slot}POS")]
                self.v[("F", "ISTATUSFLAGS")] = 0x1000  # converged flag cleared at move start
            elif data == g11.STOP_FRAME:
                self.target = None
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out


def g2_raw(v):
    b = int(v).to_bytes(4, "big", signed=True)
    return b[2:4] + b[0:2]


def go(fake, slot=3, **kw):
    args = {"send": True, "confirm": f"MOVE F TO CUBE {slot} ON COM3", "on_for_2_min": True, "nobody_looking": True}
    args.update(kw)
    return g11.run(slot=slot, find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0,
                   sleep=lambda s: None, **args)


def test_frames_and_registry():
    assert g11.GOTO_FRAME.hex(" ") == "06 00 50 21 b5 00 10 3c"
    assert g11.STOP_FRAME[5:7] == bytes([0, 15]) and g11.slot_frame(3)[5:9] == bytes([0, 3, 0, 0])
    assert all(sum(f[:-1]) & 0xFF == f[-1] for f in g11.ALLOWED_WRITES)
    with pytest.raises(g11.g4.MotorGate4Refused):
        g11.slot_frame(5)
    assert REVIEWED_MAINTENANCE["motor.f_goto_cube_slot"]["adr"] == "ADR-0035"
    text = SPEC.read_text(encoding="utf-8")
    for f in (g11.GOTO_FRAME, g11.STOP_FRAME, *(g11.slot_frame(n) for n in (1, 2, 3, 4))):
        assert f.hex(" ").upper() in text


def test_dry_run_and_refusals_open_nothing():
    opened = []
    res = g11.run(slot=2, find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == []
    for kw in ({"nobody_looking": False}, {"confirm": "MOVE F TO CUBE 2 ON COM3"}):
        res = g11.run(slot=3, send=True, on_for_2_min=True, **{"nobody_looking": True,
                      "confirm": "MOVE F TO CUBE 3 ON COM3", **kw},
                      find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
        assert res.outcome == "refused" and opened == []


def test_moves_to_the_slot_with_only_the_reviewed_writes():
    fake = FakeF()
    res = go(fake, slot=3)
    assert res.outcome == "pass", res.message
    assert res.final["APOS"] == 68000
    assert [w for w in fake.writes if w in g11.ALLOWED_WRITES] == [g11.slot_frame(3), g11.GOTO_FRAME]


def test_unexpected_stored_positions_refuse_with_nothing_written():
    fake = FakeF(cube=(0, 34000, 70000, 102000))
    res = go(fake, slot=3)
    assert res.outcome == "baseline_refused" and not [w for w in fake.writes if w in g11.ALLOWED_WRITES]


def test_wrong_way_motion_sends_the_f_stop():
    fake = FakeF(wrong_way=True, step=500)
    fake.v[("F", "APOS")] = fake.v[("F", "CPOS")] = 34000
    res = go(fake, slot=3)
    assert res.outcome == "fault_stopped" and "moved away" in res.fault
    assert [w for w in fake.writes if w in g11.ALLOWED_WRITES][-1] == g11.STOP_FRAME
