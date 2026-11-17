"""Motor Gate 4 (ADR-0019) against a fake drive chain that models an X move. No real hardware."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate2 as g2
from openevos.maintenance import motor_gate3_stop as g3
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.safety.firmware import REVIEWED_MAINTENANCE

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate4_x_move_spec.md"
PORT = mg1.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", None)
FF = b"\xff"
NORMAL = {"fBinary": 1, "fParity": 0, "fOutxCtsFlow": 0, "fOutxDsrFlow": 0, "fDtrControl": 1, "fDsrSensitivity": 0,
          "fTXContinueOnXoff": 0, "fOutX": 0, "fInX": 0, "fErrorChar": 0, "fNull": 0, "fRtsControl": 1,
          "fAbortOnError": 0}


def vendor32(v):
    b = v.to_bytes(4, "big", signed=True)
    return b[2:4] + b[0:2]


def reply(axis_id, address, value):
    body = bytes([0x00, 0x11, 0xB4, 0x05 if len(value) == 4 else 0x04, 0x00, axis_id << 4,
                  address >> 8, address & 0xFF]) + value
    framed = bytes([len(body)]) + body
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def id_reply():
    framed = bytes([6, 0x00, 0x01, 0xB0, 0x00, 0x00, 0x10])
    return b"O" + framed + bytes([sum(framed) & 0xFF])


ALL_READS = (*g2.READS, g4.SRH, g4.LCONVERGEBAND, g4.CPOSCMD)


class FakeX:
    """Six drives at rest; X models CPOSCMD, ESTATE=8 moves (taking `move_polls` SRH reads), state 13, stop."""

    def __init__(self, *, move_polls=2, stuck=False, overshoot=0, ack_fail=None, band=8, apos=0, speed=0x000F0000,
                 stop_ack_fail=False, stale=False, abandon=False):
        self.baud, self.writes, self.closed, self._pending = None, [], False, b""
        self.abandon = abandon
        self.stale = stale  # the previous move's "complete" SRH/ESTATEV survive into the first poll of a new move
        self.axis = "X"  # the axis whose SRH reads advance a move
        self.v = {}
        for r in ALL_READS:
            self.v[(r.axis, r.var)] = {"ESTATEV": 0, "IINITONCEDONE": 12345, "LPOSMAX": 50000,
                                       "CACCSTOP": 0x00010000, "CSPDCMD": 0x000F0000, "CACCCMD": 0x00010000,
                                       "LCONVERGEBAND": band, "SRH": 0x0200}.get(r.var, 0)
        self.v[("X", "APOS")] = self.v[("X", "CPOS")] = apos
        self.v[("X", "CSPDCMD")] = speed
        self.move_polls, self.stuck, self.overshoot = move_polls, stuck, overshoot
        self.ack_fail, self.stop_ack_fail = ack_fail, stop_ack_fail
        self.remaining = 0
        self.by_frame = {r.frame: r for r in ALL_READS}

    def inspect(self):
        return {"dcb": {"flags": dict(NORMAL)}, "timeouts": {}}

    def configure(self, baud):
        self.baud = baud

    def purge_rx(self):
        self._pending = b""

    def _x(self, var, val=None):
        if val is not None:
            self.v[("X", var)] = val
        return self.v[("X", var)]

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = id_reply()
        elif data in self.by_frame:
            r = self.by_frame[data]
            if (r.axis, r.var) == (self.axis, "SRH") and self.remaining > 0 and not self.stuck:
                poll = self.move_polls - self.remaining  # 0 = first poll of this move
                self.remaining -= 1
                if self.stale and poll == 1:  # the latched state clears only from the second poll on
                    self._x("ESTATEV", 8)
                    self._x("SRH", 0)
                if self.remaining == 0:  # move finished: the trajectory (CPOS) reaches the target only now
                    self._x("CPOS", self._x("CPOSCMD"))
                    self._x("APOS", self._x("CPOS") + self.overshoot)
                    self._x("ESTATEV", 13)
                    self._x("SRH", 0x0200)
            val = self.v[(r.axis, r.var)]
            raw = (val & 0xFFFF).to_bytes(2, "big") if r.vtype == "INT" else vendor32(val)
            self._pending = reply(r.axis_id, r.address, raw)
        else:
            if data == self.ack_fail or (self.stop_ack_fail and data in g3.ALLOWED_WRITES):
                self._pending = b""
                return len(data)
            self._pending = b"O"
            if data[2] == 0x10 and data[3:5] in (bytes([0x25, 0xD4]), bytes([0x25, 0xD6])):  # X target / speed
                v = data[5:9]
                self._x("CPOSCMD" if data[4] == 0xD4 else "CSPDCMD", int.from_bytes(v[2:4] + v[0:2], "big", signed=True))
            elif data == g4.M3_ESTATE_MOVE and self.abandon:  # the drive ends the move by itself, short
                self._x("APOS", 1000)
                self._x("CPOS", 1000)
                self._x("ESTATEV", 0)
                self._x("SRH", 0x0200)
            elif data == g4.M3_ESTATE_MOVE:
                if not self.stale:
                    self._x("ESTATEV", 8)
                    self._x("SRH", 0)
                self.remaining = self.move_polls
            elif data == g3.W3_ESTATE_STOP:
                self.remaining = 0
                self._x("CPOS", self._x("APOS"))
                self._x("ESTATEV", 0)
                self._x("SRH", 0x0200)
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.closed = True

    def write_frames(self):
        return [w for w in self.writes if w in g4.ALLOWED_WRITES]


def go(fake=None, **kw):
    fake = fake or FakeX()
    opened = []
    args = {"send": True, "confirm": "MOVE X 1MM AND BACK ON COM3", "on_for_2_min": True, "stage_empty_watching": True}
    args.update(kw)
    res = g4.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d) or fake,
                 monotonic=lambda: 0.0, sleep=lambda s: None, **args)
    return res, fake, opened


STOP_FRAMES = [f for _n, f in g3.WRITES]
HAPPY = [g4.M1_TARGET_OUT, g4.M3_ESTATE_MOVE, g4.M2_TARGET_HOME, g4.M3_ESTATE_MOVE, *STOP_FRAMES]


# ---------------------------------------------------------------------------------- constants
def test_write_frames_are_in_the_spec_and_checksummed():
    text = SPEC.read_text(encoding="utf-8")
    for f in (g4.M1_TARGET_OUT, g4.M2_TARGET_HOME, g4.M3_ESTATE_MOVE):
        assert f"**`{f.hex(' ').upper()}`**" in text and sum(f[:-1]) & 0xFF == f[-1]
    assert g4.M1_TARGET_OUT[5:9] == vendor32(2268) and g4.M2_TARGET_HOME[5:9] == vendor32(0)
    assert g4.SRH.frame.hex(" ") == "08 00 10 b0 04 00 11 09 0f f5"  # vendor XYStageXAxisIsMoving


def test_nothing_else_can_be_written():
    for bad in (bytes.fromhex("08 00 10 25 D4 10 00 00 00 19"),  # another target
                bytes.fromhex("06 00 20 21 B3 00 08 02"),  # move on Y
                bytes.fromhex("08 00 10 25 D6 00 00 00 20 2B"),  # speed write
                bytes.fromhex("08 00 10 25 B6 00 00 00 00 EB"),  # limit write
                bytes.fromhex("04 00 10 00 02 16")):  # AXISOFF
        with pytest.raises(g4.MotorGate4Refused):
            g4.assert_allowed(bad)


def test_registered():
    e = REVIEWED_MAINTENANCE["motor.x_move_1mm_and_back"]
    assert e["module"] == "maintenance/motor_gate4_x_move.py" and e["adr"] == "ADR-0019"


# ---------------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("kw", [{"on_for_2_min": False}, {"stage_empty_watching": False},
                                {"confirm": "STOP IDLE X AXIS ON COM3"}])
def test_refusals_open_nothing(kw):
    res, fake, opened = go(**kw)
    assert res.outcome == "refused" and opened == [] and fake.writes == []


def test_dry_run():
    res, fake, opened = go(send=False, on_for_2_min=False, stage_empty_watching=False, confirm="")
    assert res.outcome == "dry_run" and opened == [] and fake.writes == []


@pytest.mark.parametrize("fake", [FakeX(apos=5), FakeX(band=0), FakeX(band=500), FakeX(speed=0x00200000)])
def test_baseline_refusals_write_nothing(fake):
    res, f, _ = go(fake)
    assert res.outcome == "baseline_refused" and f.write_frames() == []


# ---------------------------------------------------------------------------------- the move
def test_pass_full_sequence():
    res, fake, _ = go()
    assert res.outcome == "pass", (res.message, res.final)
    assert fake.write_frames() == HAPPY
    assert res.legs["out"]["APOS"] == 2268 and res.legs["home"]["APOS"] == 0
    assert res.legs["out"]["trace"][-1] == {"SRH": 0x0200, "ESTATEV": 13, "CPOS": 2268}
    assert all(res.final["checks"].values())


def test_stale_complete_state_at_first_poll_is_not_taken_as_completion():
    # 2026-11-13 Y home leg: the first poll still showed SRH bit 9 + ESTATEV 13 from the previous leg.
    res, _fake, _ = go(FakeX(stale=True, move_polls=4))
    assert res.outcome == "pass", (res.message, res.final)
    first = res.legs["home"]["trace"][0]
    assert first["SRH"] & g4.SRH_NOT_MOVING_BIT and first["ESTATEV"] == 13 and first["CPOS"] != 0
    assert len(res.legs["home"]["trace"]) == 4 and res.legs["home"]["APOS"] == 0


def test_a_move_the_drive_abandons_is_stopped_after_a_few_polls_not_the_timeout():
    res, fake, _ = go(FakeX(abandon=True))
    assert res.outcome == "fault_stopped" and "ended the move at CPOS 1000" in res.fault
    assert len(res.legs["out"]["trace"]) == g4.ABANDON_POLLS + 1  # the first poll has nothing to compare with
    assert fake.write_frames() == [g4.M1_TARGET_OUT, g4.M3_ESTATE_MOVE, *STOP_FRAMES]


def test_stray_sync_byte_before_a_read_reply_is_tolerated():
    res = g4.MotorGate4Result("x", "", "", "")
    fake = FakeX()
    link = g4._Link(fake, res, lambda: 0.0, lambda s: None)
    real_write = fake.write

    def write(data):
        n = real_write(data)
        if bytes(data) == g4.XR["APOS"].frame:
            fake._pending = b"\x0d" + fake._pending
        return n

    fake.write = write
    assert link.read(g4.XR["APOS"])["i32"] == 0
    assert res.exchanges[-1].problem == "leading late sync byte(s) dropped"


def test_drifted_axis_is_refused_before_the_target_write():
    res = g4.MotorGate4Result("x", "", "", "")
    fake = FakeX()
    fake.v[("X", "APOS")] = -8  # command 0: 8 counts of encoder drift
    link = g4._Link(fake, res, lambda: 0.0, lambda s: None)
    with pytest.raises(g4.DriftRefused, match="drift -8"):
        g4._leg(link, res, "out", "M1 CPOSCMD=2268", g4.M1_TARGET_OUT, g4.TARGET, 8)
    assert fake.write_frames() == []


def test_timeout_is_wall_clock_bounded():
    t = iter(range(10_000))  # each monotonic() call advances 1 s
    fake = FakeX(stuck=True)
    res = g4.run(send=True, confirm="MOVE X 1MM AND BACK ON COM3", on_for_2_min=True, stage_empty_watching=True,
                 find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: float(next(t)),
                 sleep=lambda s: None)
    assert res.outcome == "fault_stopped" and "did not complete" in res.fault
    assert len(res.legs["out"]["trace"]) < int(g4.LEG_TIMEOUT_S / g4.POLL_S)


def test_small_overshoot_within_band_passes():
    res, _, _ = go(FakeX(overshoot=3, band=8))
    assert res.outcome == "pass", res.message  # both legs land 3 counts past target, inside the band of 8
    assert res.legs["out"]["error"] == 3 and res.legs["home"]["error"] == 3


def test_timeout_sends_one_stop_and_nothing_else():
    res, fake, _ = go(FakeX(stuck=True))
    assert res.outcome == "fault_stopped" and "did not complete" in res.fault
    assert fake.write_frames() == [g4.M1_TARGET_OUT, g4.M3_ESTATE_MOVE, *STOP_FRAMES]
    assert len(res.legs["out"]["trace"]) == int(g4.LEG_TIMEOUT_S / g4.POLL_S)


def test_position_out_of_band_stops_without_return():
    res, fake, _ = go(FakeX(overshoot=40, band=8))
    assert res.outcome == "fault_stopped" and "ended at APOS" in res.fault
    assert g4.M2_TARGET_HOME not in fake.write_frames()


def test_ack_failure_on_move_request_stops():
    res, fake, _ = go(FakeX(ack_fail=g4.M3_ESTATE_MOVE))
    assert res.outcome == "fault_stopped" and "M3" in res.fault
    assert fake.write_frames() == [g4.M1_TARGET_OUT, g4.M3_ESTATE_MOVE, *STOP_FRAMES]


def test_failed_stop_tells_operator_to_switch_off():
    res, fake, _ = go(FakeX(stuck=True, stop_ack_fail=True))
    assert res.outcome == "fault_stopped" and "switch the instrument off" in res.message
    assert fake.write_frames()[-1] == STOP_FRAMES[0]  # nothing after the failed stop write


def test_cli_evidence(tmp_path):
    fake = FakeX()
    rc = g4.main(["--send", "--instrument-on-2min", "--stage-empty-watching", "--confirm",
                  "MOVE X 1MM AND BACK ON COM3", "--evidence-dir", str(tmp_path)],
                 find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0, sleep=lambda s: None)
    f = list(tmp_path.glob("motor-gate4-*-pass.json"))
    assert rc == 0 and len(f) == 1
    data = json.loads(f[0].read_text(encoding="utf-8"))
    assert data["legs"]["out"]["target"] == 2268 and data["final"]["checks"]["x_idle"] is True
