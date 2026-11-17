"""Motor Gate 15 (ADR-0044): one host AXISOFF to the idle Y axis, against a fake drive chain. No real hardware.

The fake advances a clock by 16 ms per read (the line's measured read time) so the 100 ms rule is tested with
realistic timing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate2 as g2
from openevos.maintenance import motor_gate15_y_axisoff as g15
from openevos.safety.firmware import REVIEWED_MAINTENANCE

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate15_y_axisoff_spec.md"
PORT = mg1.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", None)
FF = b"\xff"
W1 = g15.W1_Y_AXISOFF
NORMAL = {"fBinary": 1, "fParity": 0, "fOutxCtsFlow": 0, "fOutxDsrFlow": 0, "fDtrControl": 1, "fDsrSensitivity": 0,
          "fTXContinueOnXoff": 0, "fOutX": 0, "fInX": 0, "fErrorChar": 0, "fNull": 0, "fRtsControl": 1,
          "fAbortOnError": 0}
READ_S = 0.016
PHRASE = "SWITCH OFF IDLE Y AXIS ON COM3"


def _reply(r, value):
    b = value.to_bytes(4, "big", signed=True)
    raw = (value & 0xFFFF).to_bytes(2, "big") if r.bits == 16 else b[2:4] + b[0:2]  # vendor word order
    body = bytes([0x00, 0x11, 0xB4, 0x04 if r.bits == 16 else 0x05, 0x00, r.axis_id << 4,
                  r.address >> 8, r.address & 0xFF]) + raw
    framed = bytes([len(body)]) + body
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def _id_reply():
    framed = bytes([6, 0x00, 0x01, 0xB0, 0x00, 0x00, 0x10])
    return b"O" + framed + bytes([sum(framed) & 0xFF])


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeDrives:
    """Six idle, homed drives; X and Y as Gate 9 read them healthy (SRL 0xC400, ISTATUSFLAGS 0xD000).
    W1 switches Y off after `off_after` further Y SRL reads (never with `never_off`)."""

    def __init__(self, clock=None, *, ack=b"O", off_after=0, never_off=False, set_=None, wander=0, creep_off=0,
                 x_drops=False, flags_after=None, corrupt_once=None):
        self.clock = clock or Clock()
        self.writes, self.closed, self._pending = [], False, b""
        self.by_frame = {r.frame: r for r in g2.READS} | {r.frame: r for rs in g15.R.values() for r in rs.values()}
        self.v = {(r.axis, r.var): {"ESTATEV": 0, "IINITONCEDONE": 12345, "LPOSMAX": 50000}.get(r.var, 0)
                  for r in self.by_frame.values()}
        for ax in ("X", "Y"):
            self.v |= {(ax, "SRL"): 0xC400, (ax, "SRH"): 0x033F, (ax, "ISTATUSFLAGS"): 0xD000, (ax, "IQ"): 150,
                       (ax, "IIQMAX"): 2251, (ax, "IIQMIN"): -2506}
        self.v[("Y", "APOS")] = self.v[("Y", "CPOS")] = 4
        self.v |= set_ or {}
        self.ack, self.off_after, self.never_off = ack, off_after, never_off
        self.wander, self.creep_off, self.x_drops, self.flags_after = wander, creep_off, x_drops, flags_after
        self.corrupt_once = corrupt_once
        self.off_requested = False

    def inspect(self):
        return {"dcb": {"flags": dict(NORMAL)}, "timeouts": {}}

    def configure(self, baud):
        pass

    def purge_rx(self):
        self._pending = b""

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = _id_reply()
        elif data in self.by_frame:
            self.clock.t += READ_S
            r = self.by_frame[data]
            key = (r.axis, r.var)
            if key == ("Y", "SRL") and self.off_requested and not self.never_off:
                if self.off_after <= 0:
                    self.v[key] = 0x4400
                self.off_after -= 1
            if key == ("Y", "APOS"):
                self.v[key] += self.creep_off if self.off_requested else self.wander
            self._pending = _reply(r, self.v[key])
            if self.corrupt_once == key and self.off_requested:  # a fast read in the hold
                self._pending = self._pending[:-1] + bytes([self._pending[-1] ^ 0xFF])
                self.corrupt_once = None
        else:  # a write
            self._pending = self.ack
            if data == W1:
                self.off_requested = True
                self.v[("Y", "IQ")] = 0
                if self.x_drops:
                    self.v[("X", "SRL")] = 0x4400
                if self.flags_after is not None:
                    self.v[("Y", "ISTATUSFLAGS")] = self.flags_after
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.closed = True

    def write_frames(self):
        return [w for w in self.writes if w != FF and w not in self.by_frame and w != mg1.ID_QUERY]


OK = {"send": True, "confirm": PHRASE, "on_for_2_min": True, "power_cycled_no_move": True,
      "stage_clear_watching_y": True, "main_switch_second_person": True}


def go(drives=None, **kw):
    d = drives or FakeDrives()
    opened = []
    res = g15.run(find_ports=lambda: [PORT], open_port=lambda dev: opened.append(dev) or d,
                  monotonic=d.clock.monotonic, sleep=d.clock.sleep, **(OK | kw))
    return res, d, opened


# ---------------------------------------------------------------------------------- constants
def test_w1_is_the_vendors_y_axisoff_frame_and_is_in_the_spec():
    assert W1 == bytes([0x04, 0x00, 0x20, 0x00, 0x02, sum(W1[:-1]) & 0xFF])  # length 4, axis 2, TML 0x0002
    assert W1[-1] == tp.checksum(W1[:-1]) == 0x26
    assert "`04 00 20 00 02 26`" in SPEC.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["04 00 20 01 02 27",  # AXISON Y
                                 "04 00 10 00 02 16",  # AXISOFF X
                                 "04 00 00 00 02 06",  # AXISOFF broadcast
                                 "06 00 20 21 B3 00 08 02",  # Y move request
                                 "06 00 20 21 B3 00 06 00"])  # Y stop
def test_nothing_else_can_be_written(bad):
    with pytest.raises(g15.MotorGate15Refused):
        g15.assert_allowed(bytes.fromhex(bad))
    link = g15._Link(FakeDrives(), g15.MotorGate15Result("x", "", "", ""), lambda: 0.0, lambda s: None)
    with pytest.raises(Exception, match="not one of this gate"):
        link.write("bad", bytes.fromhex(bad))


def test_reads_are_the_apps_reviewed_frames():
    """Every Gate 15 read is the app's own X/Y read frame (Gate 9's addresses; LCOMMERRPOS 32-bit at 0x03BC)."""
    for ax in ("X", "Y"):
        for v, r in g15.R[ax].items():
            assert r.frame == tp.VARS[ax][v].frame, (ax, v)


def test_registered_as_reviewed_maintenance():
    e = REVIEWED_MAINTENANCE["motor.y_axisoff_idle"]
    assert e["module"] == "maintenance/motor_gate15_y_axisoff.py" and e["adr"] == "ADR-0044"
    assert e["commands"] == frozenset({0x0002})


# ---------------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("kw", [{"on_for_2_min": False}, {"power_cycled_no_move": False},
                                {"stage_clear_watching_y": False}, {"main_switch_second_person": False},
                                {"confirm": "SWITCH OFF IDLE Y AXIS ON COM4"}, {"confirm": "STOP IDLE X AXIS ON COM3"}])
def test_refusals_open_nothing(kw):
    res, d, opened = go(**kw)
    assert res.outcome == "refused" and opened == [] and d.writes == []


def test_dry_run_sends_nothing():
    res, d, opened = go(send=False, confirm="")
    assert res.outcome == "dry_run" and opened == [] and d.writes == []
    assert res.exchanges[0].request_hex == "04 00 20 00 02 26"


@pytest.mark.parametrize("drives, why", [
    (FakeDrives(set_={("Z", "ESTATEV"): 7}), "not idle"),
    (FakeDrives(set_={("Y", "SRL"): 0x4400}), "Y power stage already off"),
    (FakeDrives(set_={("Y", "SRL"): 0x8000}), "Y motion not complete"),
    (FakeDrives(set_={("Y", "BDOAXISOFF"): 1}), "settle check armed"),
    (FakeDrives(set_={("Y", "ISTATUSFLAGS"): 0x5000}), "settle window not over"),
    (FakeDrives(set_={("Y", "ISTATUSFLAGS"): 0xD001}), "drive flags set"),
    (FakeDrives(set_={("Y", "APOS"): 8}), "counts from CPOS"),
    (FakeDrives(set_={("X", "SRL"): 0x4400}), "X power stage off"),
    (FakeDrives(wander=1), "Y is not still"),
])
def test_preconditions_write_nothing(drives, why):
    res, d, _ = go(drives)
    assert res.outcome in ("not_idle", "baseline_refused") and why in res.message, res.message
    assert d.write_frames() == [] and res.writes_sent == []


# ---------------------------------------------------------------------------------- the switch-off
def test_pass_sends_w1_once_and_confirms_off_within_100_ms():
    res, d, _ = go()
    assert res.outcome == "pass", (res.message, res.checks)
    assert d.write_frames() == [W1] and res.writes_sent == [g15.W1_NAME]
    assert d.writes[d.writes.index(W1) - 1] == FF  # W1 preceded by its line sync
    assert res.off_after_ms <= 100 and res.findings["y_srl_off"] == "0x4400"
    assert res.hold[-1][0] >= 10_000 and len(res.hold) > 200  # ~30 Hz for 10 s
    assert len(res.hold_each_s) >= 10 and all(e["Y IQ"] == 0 for _t, e in res.hold_each_s)
    assert "held position unpowered" in res.message and "power-cycle" in res.message
    assert len(res.still) > 20  # Phase B: Y still for 1 s at the fast rate
    assert res.final["ESTATEV_all"] == {ax: 0 for ax in ("X", "Y", "P", "Z", "F", "O")}


def test_a_late_drop_fails_without_a_second_axisoff():
    res, d, _ = go(FakeDrives(off_after=8))  # 9 reads x 16 ms
    assert res.outcome == "fail" and not res.checks["y_off_within_100ms"]
    assert res.off_after_ms > 100 and d.write_frames() == [W1]


def test_y_that_never_switches_off_fails_after_watching_1_1_s():
    res, d, _ = go(FakeDrives(never_off=True))
    assert res.outcome == "fail" and res.off_after_ms is None
    assert res.off_reads[-1][0] > 1100 and d.write_frames() == [W1]
    assert not res.checks["y_stayed_off"]


@pytest.mark.parametrize("ack", [b"", b"X"])
def test_a_bad_acknowledgement_is_not_retried_and_the_state_is_still_read(ack):
    res, d, _ = go(FakeDrives(ack=ack))
    assert res.outcome == "write_failed" and d.write_frames() == [W1] and "not retried" in res.message
    assert res.off_reads and res.final  # Phase D reads recorded whether it took effect anyway
    assert "power-cycle" in res.message


def test_y_moving_while_unpowered_is_reported_as_a_finding():
    res, _, _ = go(FakeDrives(creep_off=1))
    assert res.findings["y_max_counts_from_baseline_while_off"] > g15.HOLD_FINDING_COUNTS
    assert "FINDING: Y moved" in res.message
    assert res.outcome == "pass"  # the switch-off itself worked; the movement is a separate finding


def test_x_dropping_stops_the_hold_and_fails():
    res, d, _ = go(FakeDrives(x_drops=True))
    assert res.outcome == "fail" and "X power stage dropped" in res.hold_stopped
    assert res.final == {} and d.write_frames() == [W1]


def test_y_flags_changed_by_the_axisoff_fail_the_state_check():
    res, _, _ = go(FakeDrives(flags_after=0xD010))
    assert res.outcome == "fail" and not res.checks["y_state_unchanged"]


def test_one_corrupt_fast_reply_is_resynced_and_retried_once():
    res, _, _ = go(FakeDrives(corrupt_once=("Y", "APOS")))
    assert res.outcome == "pass" and len(res.fast_resyncs) == 1 and "checksum" in res.fast_resyncs[0]


def test_a_corrupt_baseline_reply_stops_with_nothing_written():
    d = FakeDrives(corrupt_once=("Y", "APOS"))
    d.off_requested = True  # corrupt from the first read on (the slow Phase B reads do not retry, as every gate)
    d.never_off = True
    res, _, _ = go(d)
    assert res.outcome == "stopped" and d.write_frames() == [] and "nothing written" in res.message


# ---------------------------------------------------------------------------------- Phase E and CLI
def test_check_recovered_is_read_only():
    d = FakeDrives()
    res, _, opened = go(d, send=False, confirm="", check_recovered=True)
    assert res.outcome == "recovered" and opened == ["COM3"] and d.write_frames() == []


def test_check_recovered_reports_y_still_off():
    res, _, _ = go(FakeDrives(set_={("Y", "SRL"): 0x4400}), send=False, check_recovered=True)
    assert res.outcome == "not_recovered" and "Y power stage off" in res.message


def test_cli_writes_evidence(tmp_path):
    d = FakeDrives()
    rc = g15.main(["--send", "--instrument-on-2min", "--power-cycled-no-move", "--stage-clear-watching-y",
                   "--main-switch-second-person", "--confirm", PHRASE, "--evidence-dir", str(tmp_path)],
                  find_ports=lambda: [PORT], open_port=lambda x: d, monotonic=d.clock.monotonic, sleep=d.clock.sleep)
    files = list(tmp_path.glob("motor-gate15-*-pass.json"))
    assert rc == 0 and len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["writes_sent"] == [g15.W1_NAME] and data["off_after_ms"] <= 100
