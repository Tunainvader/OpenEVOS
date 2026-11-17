"""Motor Gate 3 (ADR-0018) against a fake drive chain that models the X stop state. No real hardware."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate2 as g2
from openevos.maintenance import motor_gate3_stop as g3
from openevos.safety.firmware import REVIEWED_MAINTENANCE

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate3_stop_spec.md"
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


class FakeDrives:
    """X/Y/P/Z/F/O at rest. W3 puts X in state 6 for `stop_polls` polls, then idle (or stuck)."""

    def __init__(self, *, apos=0, cpos=0, caccstop=0x00010000, ack=b"O", ack_on=None, stop_polls=1,
                 stuck=False, move_on_stop=0, not_idle_axis=None):
        self.baud, self.writes, self.closed, self._pending = None, [], False, b""
        self.vars = {}
        for r in g2.READS:
            v = {"ESTATEV": 0, "IINITONCEDONE": 12345, "APOS": 0, "CPOS": 0, "LPOSMIN": 0, "LPOSMAX": 50000,
                 "CACCSTOP": 0x00010000, "CSPDCMD": 0, "CACCCMD": 0}[r.var]
            self.vars[(r.axis, r.var)] = v
        self.vars[("X", "APOS")], self.vars[("X", "CPOS")] = apos, cpos
        self.vars[("X", "CACCSTOP")] = caccstop
        if not_idle_axis:
            self.vars[(not_idle_axis, "ESTATEV")] = 7
        self.ack, self.ack_on = ack, ack_on
        self.stop_polls, self.stuck, self.move_on_stop = stop_polls, stuck, move_on_stop
        self.by_frame = {r.frame: r for r in g2.READS}

    def inspect(self):
        return {"dcb": {"flags": dict(NORMAL)}, "timeouts": {}}

    def configure(self, baud):
        self.baud = baud

    def purge_rx(self):
        self._pending = b""

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = id_reply()
        elif data in self.by_frame:
            r = self.by_frame[data]
            if (r.axis, r.var) == ("X", "ESTATEV") and self.vars[("X", "ESTATEV")] == 6 and not self.stuck:
                self.stop_polls -= 1
                if self.stop_polls < 0:
                    self.vars[("X", "ESTATEV")] = 0
            v = self.vars[(r.axis, r.var)]
            raw = v.to_bytes(2, "big") if r.vtype == "INT" else vendor32(v)
            self._pending = reply(r.axis_id, r.address, raw)
        else:  # a write
            self._pending = self.ack if (self.ack_on is None or data == self.ack_on) else b"O"
            if data == g3.W3_ESTATE_STOP:
                self.vars[("X", "ESTATEV")] = 6
                self.vars[("X", "APOS")] += self.move_on_stop
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.closed = True

    def frames(self):
        return [w for w in self.writes if w != FF]

    def write_frames(self):
        return [w for w in self.frames() if w in g3.ALLOWED_WRITES]


def go(drives=None, **kw):
    drives = drives or FakeDrives()
    opened = []
    args = {"send": True, "confirm": "STOP IDLE X AXIS ON COM3", "on_for_2_min": True, "stage_clear_watching": True}
    args.update(kw)
    res = g3.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d) or drives,
                 monotonic=lambda: 0.0, sleep=lambda s: None, **args)
    return res, drives, opened


# ---------------------------------------------------------------------------------- constants
def test_write_frames_match_spec_and_vendor_checksums():
    text = SPEC.read_text(encoding="utf-8")
    for f in (g3.W1_IPENDINGMOVES, g3.W2_CACCSTOP_1_0, g3.W3_ESTATE_STOP):
        assert f"**`{f.hex(' ').upper()}`**" in text
        assert sum(f[:-1]) & 0xFF == f[-1]
    assert g3.W1_IPENDINGMOVES[2:5] == b"\x10\x21\xcd" and g3.W3_ESTATE_STOP[3:7] == b"\x21\xb3\x00\x06"
    assert g3.W2_CACCSTOP_1_0[5:9] == bytes.fromhex("00 00 00 01")  # 1.0 in vendor order


def test_nothing_else_can_be_written():
    bad = [bytes.fromhex("06 00 10 21 B3 00 08 F2"),  # ESTATE = 8 (move)
           bytes.fromhex("06 00 10 21 B3 00 09 F3"),  # ESTATE = 9 (halt)
           bytes.fromhex("06 00 20 21 B3 00 06 00"),  # stop on Y
           bytes.fromhex("04 00 10 00 02 16"),  # AXISOFF
           bytes.fromhex("08 00 10 25 B6 00 00 00 00 EB")]  # LPOSMAX write
    for b in bad:
        with pytest.raises(g3.MotorGate3Refused):
            g3.assert_allowed(b)
    for r in g2.READS:  # reads outside the Gate 3 set are refused too
        if r.axis != "X" and r.var not in g2.READY:
            with pytest.raises(g3.MotorGate3Refused):
                g3.assert_allowed(r.frame)


def test_registered_as_reviewed_maintenance():
    e = REVIEWED_MAINTENANCE["motor.x_controlled_stop_idle"]
    assert e["module"] == "maintenance/motor_gate3_stop.py" and e["adr"] == "ADR-0018"


def test_spec_addresses_are_the_x_variables():
    text = SPEC.read_text(encoding="utf-8")
    assert re.search(r"`ESTATE` `0x03B3`", text) and re.search(r"`IPENDINGMOVES` `0x03CD`", text)
    assert {r.var: r.address for r in g2.READS if r.axis == "X"}["CACCSTOP"] == 0x03E8


# ---------------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("kw", [{"on_for_2_min": False}, {"stage_clear_watching": False},
                                {"confirm": "STOP IDLE X AXIS ON COM4"}, {"confirm": "READ MOTOR LIMITS ON COM3"}])
def test_refusals_open_nothing(kw):
    res, drives, opened = go(**kw)
    assert res.outcome == "refused" and opened == [] and drives.writes == []


def test_dry_run_sends_nothing():
    res, drives, opened = go(send=False, on_for_2_min=False, stage_clear_watching=False, confirm="")
    assert res.outcome == "dry_run" and opened == [] and drives.writes == []


@pytest.mark.parametrize("drives, why", [
    (FakeDrives(not_idle_axis="Z"), "not_idle"),
    (FakeDrives(apos=1, cpos=0), "baseline_refused"),
    (FakeDrives(apos=60000, cpos=60000), "baseline_refused"),
    (FakeDrives(caccstop=0x00020000), "baseline_refused"),
])
def test_preconditions_write_nothing(drives, why):
    res, d, _ = go(drives)
    assert res.outcome == why and d.write_frames() == [] and res.writes_sent == []


# ---------------------------------------------------------------------------------- the stop
def test_pass_sends_the_three_writes_once_in_order():
    res, d, _ = go()
    assert res.outcome == "pass", (res.message, res.checks)
    assert d.write_frames() == [g3.W1_IPENDINGMOVES, g3.W2_CACCSTOP_1_0, g3.W3_ESTATE_STOP]
    assert res.polls == [6, 0] and all(res.checks.values())
    i = d.writes.index(g3.W1_IPENDINGMOVES)
    assert d.writes[i - 1] == FF  # each write preceded by its line sync


def test_fast_stop_seen_as_idle_on_first_poll():
    res, _, _ = go(FakeDrives(stop_polls=0))
    assert res.outcome == "pass" and res.polls == [0]


@pytest.mark.parametrize("ack, ack_on, sent", [(b"", g3.W1_IPENDINGMOVES, 1), (b"X", g3.W2_CACCSTOP_1_0, 2),
                                               (b"", g3.W3_ESTATE_STOP, 3)])
def test_bad_ack_stops_further_writes(ack, ack_on, sent):
    res, d, _ = go(FakeDrives(ack=ack, ack_on=ack_on))
    assert res.outcome == "write_failed" and len(d.write_frames()) == sent
    assert d.write_frames()[-1] == ack_on and "not retried" in res.message


def test_stuck_in_stop_state_fails_and_sends_nothing_more():
    res, d, _ = go(FakeDrives(stuck=True))
    assert res.outcome == "fail" and res.polls == [6] * g3.POLLS and not res.checks["x_returned_to_idle"]
    assert d.write_frames() == [g3.W1_IPENDINGMOVES, g3.W2_CACCSTOP_1_0, g3.W3_ESTATE_STOP]
    assert "power cycle" in res.message


def test_position_change_fails():
    res, _, _ = go(FakeDrives(move_on_stop=5))
    assert res.outcome == "fail" and not res.checks["apos_unchanged"]


def test_cli_evidence(tmp_path):
    d = FakeDrives()
    rc = g3.main(["--send", "--instrument-on-2min", "--stage-clear-watching", "--confirm", "STOP IDLE X AXIS ON COM3",
                  "--evidence-dir", str(tmp_path)], find_ports=lambda: [PORT], open_port=lambda x: d,
                 monotonic=lambda: 0.0, sleep=lambda s: None)
    f = list(tmp_path.glob("motor-gate3-*-pass.json"))
    assert rc == 0 and len(f) == 1
    data = json.loads(f[0].read_text(encoding="utf-8"))
    assert data["writes_sent"] == [n for n, _ in g3.WRITES] and data["polls"] == [6, 0]
