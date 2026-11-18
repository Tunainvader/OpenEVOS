"""Motor Gate 1 (ADR-0016, spec rev 2) against a fake serial drive chain. Nothing here touches real hardware."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from openevos.maintenance import motor_gate1 as mg
from openevos.safety.firmware import REVIEWED_L3_READS

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate1_spec.md"
PORT = mg.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", "1-1.2")
FF = b"\xff"
NORMAL_FLAGS = {"fBinary": 1, "fParity": 0, "fOutxCtsFlow": 0, "fOutxDsrFlow": 0, "fDtrControl": 1,
                "fDsrSensitivity": 0, "fTXContinueOnXoff": 0, "fOutX": 0, "fInX": 0, "fErrorChar": 0, "fNull": 0,
                "fRtsControl": 1, "fAbortOnError": 0}


def read_reply(axis_id: int, address: int, value: bytes) -> bytes:
    body = bytes([0x00, 0x10, 0xB4, 0x05 if len(value) == 4 else 0x04, 0x00, (axis_id << 4) & 0xFF,
                  address >> 8, address & 0xFF]) + value
    framed = bytes([len(body)]) + body
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def id_reply(gid: int = 1) -> bytes:
    framed = bytes([6, 0x00, 0x01, 0xB0, 0x00, (gid >> 4) & 0xFF, (gid << 4) & 0xFF])
    return b"O" + framed + bytes([sum(framed) & 0xFF])


class FakeChain:
    """Answers like an idle, homed drive chain at `baud`; records every byte written and every call."""

    def __init__(self, overrides=None, baud=9600, flags=None, sync_fail_before=0, lose_sync_at=None):
        self.name = "COM3"
        self.calls: list[tuple] = []
        self.writes: list[bytes] = []
        self.closed = False
        self.baud = None
        self.answer_baud = baud
        self.flags = dict(NORMAL_FLAGS, **(flags or {}))
        self._pending = b""
        self._sync_fail_before = sync_fail_before
        self._lose_sync_at = lose_sync_at  # index of the frame write after which sync stops working
        self.values = {}
        for axis, aid, var, addr, _bits, frame in mg.FRAMES:
            v = {"ESTATEV": 0, "IINITONCEDONE": 12345, "IVERSION": mg.MOTOR_FIRMWARE_INI[axis]}.get(var)
            raw = (1000 * aid).to_bytes(4, "big", signed=True) if var == "CPOS" else v.to_bytes(2, "big")
            self.values[frame] = read_reply(aid, addr, raw)
        self.values[mg.ID_QUERY] = id_reply(1)
        self.values.update(overrides or {})

    def inspect(self):
        self.calls.append(("inspect",))
        return {"dcb": {"raw_hex": "1c 00", "BaudRate": 9600, "ByteSize": 8, "Parity": 0, "StopBits": 0,
                        "flags": dict(self.flags), "XonLim": 0, "XoffLim": 0},
                "timeouts": {"ReadIntervalTimeout": 0}}

    def configure(self, baud):
        self.calls.append(("configure", baud))
        self.baud = baud

    def purge_rx(self):
        self.calls.append(("purge",))
        self._pending = b""

    def frames_written(self):
        return [w for w in self.writes if w != FF]

    def write(self, data):
        data = bytes(data)
        self.calls.append(("write", data))
        self.writes.append(data)
        if self.baud != self.answer_baud:
            self._pending = b""
        elif data == FF:
            lost = self._lose_sync_at is not None and len(self.frames_written()) > self._lose_sync_at
            if self._sync_fail_before > 0 or lost:
                self._sync_fail_before -= 1
                self._pending = b""
            else:
                self._pending = b"\x0d"
        else:
            self._pending = self.values.get(data, b"")
        return len(data)

    def read(self, max_bytes):
        self.calls.append(("read", max_bytes))
        out, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return out

    def close(self):
        self.calls.append(("close",))
        self.closed = True


def go(chain=None, ports=(PORT,), **kw):
    chain = chain or FakeChain()
    opened, sleeps = [], []

    def open_port(dev):
        opened.append(dev)
        return chain

    args = {"send": True, "confirm": "READ MOTOR DRIVES ON COM3", "on_for_2_min": True}
    args.update(kw)
    res = mg.run(find_ports=lambda: list(ports), open_port=open_port, monotonic=lambda: 0.0,
                 sleep=sleeps.append, **args)
    return res, chain, opened


def expected_writes(sync_ffs_before_id=1):
    out = [FF] * sync_ffs_before_id + [mg.ID_QUERY]
    for f in mg.FRAMES:
        out += [FF, f[5]]
    return out


# ---------------------------------------------------------------------------------- frames
def test_frames_match_the_spec_table_exactly():
    rows = re.findall(r"^\| (\w) \((\d)\) \| (\d\d)-bit \| `(\w+)` `0x([0-9A-F]{4})` \| `([0-9A-F ]+)` \|$",
                      SPEC.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(rows) == 22 == len(mg.FRAMES)
    for (axis, aid, bits, var, addr, hexframe), f in zip(rows, mg.FRAMES, strict=True):
        assert f[:5] == (axis, int(aid), var, int(addr, 16), int(bits))
        assert f[5] == bytes.fromhex(hexframe)


def test_frames_are_byte_identical_to_the_vendor_probes():
    by_name = {(f[0], f[2]): f[5] for f in mg.FRAMES}
    assert by_name["X", "CPOS"].hex(" ") == "08 00 10 b0 05 00 11 02 9e 7e"  # FUN_18002C2C0
    assert by_name["Y", "IVERSION"][:9].hex() == "080020b004001103ef"  # YAxisGetScriptVersion
    assert by_name["Z", "IVERSION"][:9].hex() == "080040b004001103e2"  # ZAxisGetScriptVersion
    assert mg.ID_QUERY.hex(" ") == "08 00 00 b0 00 00 01 00 00 b9"


def test_vendor_constants():
    assert mg.SYNC_ATTEMPTS == 15 and mg.BAUD_PASSES == (115200, 9600) and mg.SPEC_REVISION == 2


@pytest.mark.parametrize("opcode", [0x0801, 0x0820, 0xB000, 0x0000, 0xFFFF])
def test_only_reads_can_be_built(opcode):
    with pytest.raises(mg.MotorGate1Refused):
        mg.build_read(1, opcode, 0x029E)


def test_anything_outside_the_fixed_frames_is_refused():
    mg.assert_allowed(FF)
    for f in mg.FRAMES:
        mg.assert_allowed(f[5])
    for bad in (b"\x00", mg.build_read(1, 0xB004, 0x0300), bytes.fromhex("08 00 10 08 01 00 11 00 01 33"),
                bytes.fromhex("06 00 10 08 20 00 04 42"), b"AXISON\r"):
        with pytest.raises(mg.MotorGate1Refused):
            mg.assert_allowed(bad)


def test_registry_entry():
    e = REVIEWED_L3_READS["motor.gate1_read"]
    assert e["vid_pid"] == (0x0403, 0x6001) and e["commands"] == mg.ALLOWED_OPCODES and e["adr"] == "ADR-0016"


# ---------------------------------------------------------------------------------- replies
def test_reply_parsing_rejects_damage():
    good = read_reply(1, 0x03EA, b"\x00\x00")
    assert mg.parse_read_reply(good, 1, 0x03EA, 16) == (b"\x00\x00", "")
    assert "checksum" in mg.parse_read_reply(good[:-1] + bytes([good[-1] ^ 1]), 1, 0x03EA, 16)[1]
    assert "acknowledgement" in mg.parse_read_reply(b"X" + good[1:], 1, 0x03EA, 16)[1]
    assert "length" in mg.parse_read_reply(good + b"\x00", 1, 0x03EA, 16)[1]
    assert "not axis" in mg.parse_read_reply(good, 2, 0x03EA, 16)[1]
    assert "variable" in mg.parse_read_reply(good, 1, 0x03FE, 16)[1]
    assert "expected 15" in mg.parse_read_reply(good, 1, 0x03EA, 32)[1]
    assert mg.parse_id_reply(id_reply(1)) == (1, "")
    assert mg.parse_id_reply(id_reply(6)) == (6, "")


def test_32_bit_values_decode_in_both_word_orders():
    assert mg.decode(bytes.fromhex("0001 0002"), 32) == {"i32_big": 0x00010002, "i32_word_swapped": 0x00020001}
    assert mg.decode(bytes.fromhex("FFFF"), 16) == {"u16": 65535, "i16": -1}


# ---------------------------------------------------------------------------------- driver state
def test_driver_state_check():
    ok = {"dcb": {"flags": dict(NORMAL_FLAGS)}}
    assert mg.driver_state_problems(ok) == []
    rev1 = {"dcb": {"flags": dict(NORMAL_FLAGS, fDtrControl=0, fRtsControl=0)}}
    assert len(mg.driver_state_problems(rev1)) == 2
    for flag in mg.FLOW_FLAGS:
        assert mg.driver_state_problems({"dcb": {"flags": dict(NORMAL_FLAGS, **{flag: 1})}})
    assert mg.driver_state_problems({"dcb": {"flags": dict(NORMAL_FLAGS, fRtsControl=2)}})  # handshake


# ---------------------------------------------------------------------------------- dry run and refusals
def test_dry_run_opens_and_sends_nothing():
    res, chain, opened = go(send=False, confirm="", on_for_2_min=False)
    assert res.outcome == "dry_run" and opened == [] and chain.calls == [] and res.bytes_written == 0
    assert len(res.exchanges) == 23


@pytest.mark.parametrize("kw, ports, why", [
    ({}, (), "found 0"),
    ({}, (PORT, PORT), "found 2"),
    ({"on_for_2_min": False}, (PORT,), "2 minutes"),
    ({"confirm": "READ MOTOR DRIVES ON COM4"}, (PORT,), "phrase"),
    ({"confirm": "read motor drives on com3"}, (PORT,), "phrase"),
    ({"confirm": "INSPECT MOTOR PORT COM3"}, (PORT,), "phrase"),
    ({"send": False, "inspect_only": True, "confirm": "READ MOTOR DRIVES ON COM3"}, (PORT,), "phrase"),
    ({"inspect_only": True}, (PORT,), "separate steps"),
])
def test_refusals_open_nothing(kw, ports, why):
    res, chain, opened = go(ports=ports, **kw)
    assert res.outcome == "refused" and why in res.message
    assert opened == [] and chain.calls == []


# ---------------------------------------------------------------------------------- inspect-only
@pytest.mark.parametrize("flags, normal", [({}, True), ({"fDtrControl": 0, "fRtsControl": 0}, False)])
def test_inspect_port_reads_back_and_writes_nothing(flags, normal):
    res, chain, opened = go(FakeChain(flags=flags), send=False, inspect_only=True, confirm="INSPECT MOTOR PORT COM3",
                            on_for_2_min=False)
    assert res.outcome == "inspected" and opened == ["COM3"]
    assert chain.calls == [("inspect",), ("close",)]  # no configure, no purge, no write
    assert res.bytes_written == 0 and res.port_state_read_back["dcb"]["flags"]["fDtrControl"] == flags.get("fDtrControl", 1)  # noqa: E501
    assert ("normal" in res.message and "NOT" not in res.message) == normal


def test_wrong_driver_state_refuses_before_any_byte():
    res, chain, _ = go(FakeChain(flags={"fDtrControl": 0, "fRtsControl": 0}))
    assert res.outcome == "driver_state" and "Nothing sent" in res.message and "Re-plug" in res.message
    assert chain.calls == [("inspect",), ("close",)] and chain.writes == []


# ---------------------------------------------------------------------------------- full run
def test_complete_run_at_9600_copies_the_vendor_sequence():
    res, chain, opened = go(FakeChain(baud=9600))
    assert res.outcome == "complete", res.message
    assert opened == ["COM3"] and chain.closed and res.baud == 9600
    # inspect first, then pass 1 at 115,200 (15 unanswered FF), then pass 2 at 9,600
    assert chain.calls[0] == ("inspect",) and chain.calls[1] == ("configure", 115200)
    assert [c for c in chain.calls if c[0] == "configure"] == [("configure", 115200), ("configure", 9600)]
    assert chain.writes == [FF] * 15 + expected_writes()
    assert chain.frames_written() == [mg.ID_QUERY] + [f[5] for f in mg.FRAMES]
    assert res.gateway_axis_id == 1 and res.bytes_written == sum(map(len, chain.writes))
    assert res.axes["X"]["CPOS"]["i32_big"] == 1000
    assert res.axes["X"]["CPOS_mm_candidate"]["i32_big"] == round(1000 / 2880 * 1.27, 4)
    assert not any(a["older_than_ini"] for a in res.axes.values())
    first_pass = res.exchanges[0]
    assert first_pass.what == "line sync @ 115200" and first_pass.attempts == 15 and first_pass.problem


def test_complete_run_at_115200_skips_the_second_pass():
    res, chain, _ = go(FakeChain(baud=115200))
    assert res.outcome == "complete" and res.baud == 115200
    assert [c for c in chain.calls if c[0] == "configure"] == [("configure", 115200)]
    assert chain.writes == expected_writes()


def test_each_frame_is_one_write_then_one_32_byte_read():
    _res, chain, _ = go()
    for i, call in enumerate(chain.calls):
        if call[0] == "write" and call[1] != FF:
            assert chain.calls[i + 1] == ("read", 32)
        if call == ("write", FF):
            assert chain.calls[i + 1] == ("read", 1)


def test_no_sync_at_either_rate_sends_no_frame():
    res, chain, _ = go(FakeChain(baud=57600))
    assert res.outcome == "stopped" and "no line sync" in res.message
    assert chain.writes == [FF] * 30 and chain.frames_written() == [] and chain.closed


def test_sync_succeeds_on_a_later_attempt():
    res, _chain, _ = go(FakeChain(sync_fail_before=2))  # 15 unanswered at 115,200, then 2 failures at 9,600
    assert res.outcome == "complete" and res.exchanges[1].attempts == 3


def test_sync_lost_mid_run_stops():
    res, chain, _ = go(FakeChain(lose_sync_at=3))
    assert res.outcome == "stopped" and "sync lost" in res.message
    assert len(chain.frames_written()) == 4


def test_wrong_gateway_id_stops_before_any_read():
    res, chain, _ = go(FakeChain({mg.ID_QUERY: id_reply(3)}))
    assert res.outcome == "stopped" and "axis ID 3" in res.message
    assert chain.frames_written() == [mg.ID_QUERY]


def test_missing_reply_fails_fast_without_resend():
    y_state = next(f[5] for f in mg.FRAMES if f[:3] == ("Y", 2, "ESTATEV"))
    res, chain, _ = go(FakeChain({y_state: b""}))
    assert res.outcome == "stopped" and "Y ESTATEV" in res.message and "Not retried" in res.message
    assert chain.writes.count(y_state) == 1 and chain.writes[-1] == y_state


def test_bad_checksum_fails_fast():
    xpos = mg.FRAMES[3][5]
    good = FakeChain().values[xpos]
    res, chain, _ = go(FakeChain({xpos: good[:-1] + bytes([good[-1] ^ 0xFF])}))
    assert res.outcome == "stopped" and "checksum" in res.message and chain.writes[-1] == xpos


def test_not_idle_stops_further_axes():
    z_state = next(f[5] for f in mg.FRAMES if f[:3] == ("Z", 4, "ESTATEV"))
    res, chain, _ = go(FakeChain({z_state: read_reply(4, 0x03B2, b"\x00\x05")}))
    assert res.outcome == "not_idle" and "Z state is 5" in res.message
    assert chain.writes[-1] == z_state and "F" not in res.axes


def test_homing_not_done_stops():
    x_init = mg.FRAMES[1][5]
    res, chain, _ = go(FakeChain({x_init: read_reply(1, 0x03FE, b"\x00\x00")}))
    assert res.outcome == "not_idle" and len(chain.frames_written()) == 3


def test_old_script_version_is_flagged():
    o_ver = next(f[5] for f in mg.FRAMES if f[:3] == ("O", 6, "IVERSION"))
    res, _, _ = go(FakeChain({o_ver: read_reply(6, 0x03E8, b"\x00\x05")}))
    assert res.outcome == "complete" and res.axes["O"]["older_than_ini"] is True


# ---------------------------------------------------------------------------------- CLI
def test_cli_writes_evidence(tmp_path, capsys):
    chain = FakeChain()
    rc = mg.main(["--send", "--instrument-on-2min", "--confirm", "READ MOTOR DRIVES ON COM3", "--evidence-dir",
                  str(tmp_path)], find_ports=lambda: [PORT], open_port=lambda d: chain,
                 monotonic=lambda: 0.0, sleep=lambda s: None)
    assert rc == 0
    files = list(tmp_path.glob("motor-gate1-*-complete.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["port"]["hwid"] == PORT.hwid and data["spec_revision"] == 2 and data["baud"] == 9600
    assert data["port_state_read_back"]["dcb"]["flags"]["fDtrControl"] == 1
    assert "[complete]" in capsys.readouterr().out


def test_cli_inspect_port(tmp_path, capsys):
    chain = FakeChain()
    rc = mg.main(["--inspect-port", "--confirm", "INSPECT MOTOR PORT COM3", "--evidence-dir", str(tmp_path)],
                 find_ports=lambda: [PORT], open_port=lambda d: chain)
    assert rc == 0 and chain.writes == [] and len(list(tmp_path.glob("motor-gate1-*-inspected.json"))) == 1
    assert "read back:" in capsys.readouterr().out


def test_cli_modes_are_exclusive():
    with pytest.raises(SystemExit):
        mg.main(["--send", "--inspect-port"], find_ports=lambda: [PORT])


def test_cli_dry_run_is_default(tmp_path):
    chain = FakeChain()
    rc = mg.main(["--evidence-dir", str(tmp_path)], find_ports=lambda: [PORT], open_port=lambda d: chain)
    assert rc == 0 and chain.calls == []


# ---------------------------------------------------------------------------------- Win32 structures
@pytest.mark.skipif(sys.platform != "win32", reason="Win32 serial")
def test_vendor_line_settings_keep_the_read_back_flags():
    from openevos.maintenance import win_serial as ws

    back = ws.DCB()
    back.DCBlength = 0x1C
    back.BaudRate = 1200
    back.flags = 0x1011 | 0x0080  # fBinary, DTR enable, RTS enable, fTXContinueOnXoff
    back.XonLim, back.XoffLim, back.XonChar = 2048, 512, b"\x11"
    new = ws.vendor_line_settings(back, 9600)
    assert (new.BaudRate, new.ByteSize, new.Parity, new.StopBits) == (9600, 8, 0, 2)
    assert (new.flags, new.XonLim, new.XoffLim, new.XonChar) == (back.flags, 2048, 512, b"\x11")
    assert back.BaudRate == 1200  # the read-back copy is untouched
    flags = ws.decode_flags(new.flags)
    assert flags["fDtrControl"] == 1 and flags["fRtsControl"] == 1 and flags["fBinary"] == 1
    assert flags["fTXContinueOnXoff"] == 1 and flags["fOutX"] == 0
    with pytest.raises(ws.SerialError):
        ws.vendor_line_settings(back, 57600)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 serial")
def test_vendor_timeouts_change_only_the_three_read_fields():
    from openevos.maintenance import win_serial as ws

    back = ws.COMMTIMEOUTS(5, 6, 7, 8, 9)
    to = ws.vendor_timeouts(back)
    assert (to.ReadIntervalTimeout, to.ReadTotalTimeoutMultiplier, to.ReadTotalTimeoutConstant) == (1000, 10, 10)
    assert (to.WriteTotalTimeoutMultiplier, to.WriteTotalTimeoutConstant) == (8, 9)
