"""Motor Gate 2 (ADR-0017) against a fake serial drive chain. Nothing here touches real hardware."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate2 as mg
from openevos.safety.firmware import REVIEWED_L3_READS

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "evidence" / "fl1_v1.6_motor_gate2_spec.md"
PORT = mg1.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", None)
FF = b"\xff"
NORMAL = {"fBinary": 1, "fParity": 0, "fOutxCtsFlow": 0, "fOutxDsrFlow": 0, "fDtrControl": 1, "fDsrSensitivity": 0,
          "fTXContinueOnXoff": 0, "fOutX": 0, "fInX": 0, "fErrorChar": 0, "fNull": 0, "fRtsControl": 1,
          "fAbortOnError": 0}
X_COUNTS_PER_MM = 2880 / 1.27


def vendor32(v: int) -> bytes:
    b = v.to_bytes(4, "big", signed=True)
    return b[2:4] + b[0:2]  # low word first, each word big-endian


def reply(axis_id: int, address: int, value: bytes) -> bytes:
    body = bytes([0x00, 0x11, 0xB4, 0x05 if len(value) == 4 else 0x04, 0x00, axis_id << 4,
                  address >> 8, address & 0xFF]) + value
    framed = bytes([len(body)]) + body
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def id_reply(gid=1):
    framed = bytes([6, 0x00, 0x01, 0xB0, 0x00, (gid >> 4) & 0xFF, (gid << 4) & 0xFF])
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def good_values():
    half = {"X": int(119.1 * X_COUNTS_PER_MM / 2), "Y": int(78.1 * X_COUNTS_PER_MM / 2),
            "Z": int(8.4 * 5000 / 0.61 / 2), "F": 5000}
    vals = {}
    for r in mg.READS:
        v = {"ESTATEV": 0, "IINITONCEDONE": 12345, "APOS": 3, "CPOS": 0, "CACCSTOP": 0x00010000,
             "CSPDCMD": 0x00028000, "CACCCMD": 0x00004000}.get(r.var)
        if r.var == "LPOSMIN":
            v = -half[r.axis]
        if r.var == "LPOSMAX":
            v = half[r.axis]
        vals[(r.axis, r.var)] = v
    return vals


class FakeChain:
    def __init__(self, values=None, overrides=None, flags=None, baud=115200):
        self.name, self.baud, self.answer_baud = "COM3", None, baud
        self.writes, self.calls, self.closed, self._pending = [], [], False, b""
        self.flags = dict(NORMAL, **(flags or {}))
        vals = values or good_values()
        self.replies = {mg1.ID_QUERY: id_reply()}
        for r in mg.READS:
            v = vals[(r.axis, r.var)]
            raw = v.to_bytes(2, "big") if r.vtype == "INT" else vendor32(v)
            self.replies[r.frame] = reply(r.axis_id, r.address, raw)
        self.replies.update(overrides or {})

    def inspect(self):
        self.calls.append("inspect")
        return {"dcb": {"flags": dict(self.flags)}, "timeouts": {}}

    def configure(self, baud):
        self.calls.append(("configure", baud))
        self.baud = baud

    def purge_rx(self):
        self._pending = b""

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if self.baud != self.answer_baud:
            self._pending = b""
        else:
            self._pending = b"\x0d" if data == FF else self.replies.get(data, b"")
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.closed = True

    def frames(self):
        return [w for w in self.writes if w != FF]


def go(chain=None, **kw):
    chain = chain or FakeChain()
    opened = []
    args = {"send": True, "confirm": "READ MOTOR LIMITS ON COM3", "on_for_2_min": True}
    args.update(kw)
    res = mg.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d) or chain,
                 monotonic=lambda: 0.0, sleep=lambda s: None, **args)
    return res, chain, opened


def frame_of(axis, var):
    return next(r.frame for r in mg.READS if r.axis == axis and r.var == var)


# ---------------------------------------------------------------------------------- frames
def test_frames_match_the_spec_table_exactly():
    rows = re.findall(r"^\| (\w) \((\d)\) \| `(\w+)` \| (INT|LONG|FIXED) \| `0x([0-9A-F]{4})` \| `([0-9A-F ]+)` \|$",
                      SPEC.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(rows) == 41 == len(mg.READS)
    for (axis, aid, var, typ, addr, hexframe), r in zip(rows, mg.READS, strict=True):
        assert (r.axis, r.axis_id, r.var, r.vtype, r.address) == (axis, int(aid), var, typ, int(addr, 16))
        assert r.frame == bytes.fromhex(hexframe)


def test_opcode_matches_variable_width():
    for r in mg.READS:
        assert r.frame[3:5] == (b"\xb0\x04" if r.vtype == "INT" else b"\xb0\x05")


def test_only_reviewed_frames_can_be_sent():
    mg.assert_allowed(FF)
    mg.assert_allowed(mg1.ID_QUERY)
    for r in mg.READS:
        mg.assert_allowed(r.frame)
    for bad in (bytes.fromhex("06 00 10 21 B3 00 06 F0"),  # ESTATE = 6 (stop)
                bytes.fromhex("04 00 10 00 02 16"),  # AXISOFF
                bytes.fromhex("08 00 10 25 B4 00 00 00 00 F1"),  # LPOSMIN write
                mg1.build_read(1, 0xB005, 0x03D4)):  # a read that is not in the table
        with pytest.raises(mg.MotorGate2Refused):
            mg.assert_allowed(bad)


def test_registry_entry():
    e = REVIEWED_L3_READS["motor.gate2_read"]
    assert e["vid_pid"] == (0x0403, 0x6001) and e["commands"] == mg1.ALLOWED_OPCODES and e["adr"] == "ADR-0017"


def test_decode_vendor_order_and_fixed():
    assert mg.decode_value(vendor32(-123456), "LONG")["i32"] == -123456
    assert mg.decode_value(vendor32(0x00018000), "FIXED")["fixed"] == 1.5
    assert mg.decode_value(bytes.fromhex("3039"), "INT")["u16"] == 12345


# ---------------------------------------------------------------------------------- procedure
def test_dry_run_sends_nothing():
    res, chain, opened = go(send=False, on_for_2_min=False, confirm="")
    assert res.outcome == "dry_run" and opened == [] and chain.writes == [] and len(res.exchanges) == 42


@pytest.mark.parametrize("kw", [{"on_for_2_min": False}, {"confirm": "READ MOTOR DRIVES ON COM3"},
                                {"confirm": "READ MOTOR LIMITS ON COM4"}])
def test_refusals_open_nothing(kw):
    res, chain, opened = go(**kw)
    assert res.outcome == "refused" and opened == [] and chain.writes == []


def test_wrong_driver_state_sends_nothing():
    res, chain, _ = go(FakeChain(flags={"fDtrControl": 0}))
    assert res.outcome == "driver_state" and chain.writes == [] and chain.closed


def test_complete_run_order_and_checks():
    res, chain, _ = go()
    assert res.outcome == "complete", res.message
    ready = [r.frame for r in mg.READS if r.var in mg.READY]
    params = [r.frame for r in mg.READS if r.var not in mg.READY]
    assert chain.frames() == [mg1.ID_QUERY] + ready + params
    assert chain.writes.count(FF) == 1 + 41  # baud-pass sync + one per read
    assert len(set(chain.frames())) == len(chain.frames())  # nothing resent
    c = res.checks
    for ax in ("X", "Y", "Z"):
        assert c[ax]["limits_ordered"] and not c[ax]["limits_zero"] and c[ax]["span_matches_mcba"], (ax, c[ax])
        assert c[ax]["stop_decel_positive"]
    assert c["F"]["limits_ordered"] and "stop_decel_positive" not in c["F"]
    assert c["O"] == {"holding_error_counts": 3} and res.axes["X"]["CSPDCMD"]["fixed"] == 2.5


def test_zero_limits_and_zero_stop_decel_are_reported():
    vals = good_values()
    vals[("X", "LPOSMIN")] = vals[("X", "LPOSMAX")] = 0
    vals[("Z", "CACCSTOP")] = 0
    res, _, _ = go(FakeChain(values=vals))
    assert res.outcome == "complete"
    assert res.checks["X"]["limits_zero"] and not res.checks["X"]["limits_ordered"]
    assert res.checks["Z"]["stop_decel_positive"] is False
    assert "X: limits 0/0" in res.message and "CACCSTOP NOT > 0" in res.message


def test_not_idle_stops_before_any_parameter_read():
    o_state = frame_of("O", "ESTATEV")
    res, chain, _ = go(FakeChain(overrides={o_state: reply(6, 0x03B2, b"\x00\x03")}))
    assert res.outcome == "not_idle" and chain.frames()[-1] == o_state
    assert not any(f in chain.frames() for f in (r.frame for r in mg.READS if r.var not in mg.READY))


def test_missing_reply_fails_fast():
    xmin = frame_of("X", "LPOSMIN")
    res, chain, _ = go(FakeChain(overrides={xmin: b""}))
    assert res.outcome == "stopped" and "X LPOSMIN" in res.message and chain.frames().count(xmin) == 1
    assert chain.frames()[-1] == xmin


def test_wrong_echoed_address_is_rejected():
    xmin = frame_of("X", "LPOSMIN")
    res, _, _ = go(FakeChain(overrides={xmin: reply(1, 0x03B6, vendor32(1))}))
    assert res.outcome == "stopped" and "variable" in res.message


def test_mcba_comparison():
    axes = {"X": {"LPOSMIN": {"i32": -5}, "LPOSMAX": {"i32": 5}}}
    assert mg.evaluate(axes, {"X": (-5, 5)})["X"]["vs_mcba"] == "equal"
    assert mg.evaluate(axes, {"X": (-6, 5)})["X"]["vs_mcba"] == "different"


def test_cli_writes_evidence(tmp_path):
    chain = FakeChain()
    rc = mg.main(["--send", "--instrument-on-2min", "--confirm", "READ MOTOR LIMITS ON COM3", "--evidence-dir",
                  str(tmp_path)], find_ports=lambda: [PORT], open_port=lambda d: chain,
                 monotonic=lambda: 0.0, sleep=lambda s: None)
    files = list(tmp_path.glob("motor-gate2-*-complete.json"))
    assert rc == 0 and len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["checks"]["X"]["span_matches_mcba"] is True and data["capability_source"] == "CANDIDATE"
