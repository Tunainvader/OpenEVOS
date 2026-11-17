"""Gate 1 (ADR-0011): the LED board bootloader version query, against a fake HID backend.

Nothing here touches real hardware. The fake records every call so the tests can prove what
reaches the device: one flush, exactly one 65-byte write of `00 0A 00…`, exactly one read, then
nothing else; and nothing at all on any refusal or in a dry run.
"""

from __future__ import annotations

import json
import struct
import sys
from contextlib import contextmanager

import pytest

from openevos.maintenance import led_bootloader as lb
from openevos.maintenance.led_bootloader import (
    CONFIRM_PHRASE,
    DeviceDescription,
    Gate1Refused,
    build_request,
    parse_version_reply,
    query_version,
)
from openevos.safety.firmware import REVIEWED_L3_READS

PATH = r"\\?\hid#vid_0dad&pid_0057#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"


def reply(code=0x8A, a=2, b=1, ver=(1, 4, 77), tail=b""):
    r = bytearray(65)
    r[1] = code
    r[2:10] = struct.pack(">II", a, b)
    r[22:34] = struct.pack(">III", *ver)
    r[34:34 + len(tail)] = tail
    return bytes(r)


class FakeHid:
    def __init__(self, paths=(PATH,), desc=None, answer=None, write_error=None, read_error=None, written=65):
        self.paths, self.answer = list(paths), answer if answer is not None else reply()
        self.desc = desc or DeviceDescription(PATH, 0x0DAD, 0x0057, 0x0100, 65, 65)
        self.write_error, self.read_error, self.written = write_error, read_error, written
        self.calls: list[tuple] = []

    def find(self, vid, pid):
        self.calls.append(("find", vid, pid))
        return self.paths

    def describe(self, path):
        self.calls.append(("describe", path))
        return self.desc

    @contextmanager
    def open(self, path):
        self.calls.append(("open", path))
        fake = self

        class Ch:
            def flush_input(self):
                fake.calls.append(("flush",))

            def write(self, report, timeout_s):
                fake.calls.append(("write", bytes(report), timeout_s))
                if fake.write_error:
                    raise fake.write_error
                return fake.written

            def read(self, length, timeout_s):
                fake.calls.append(("read", length, timeout_s))
                if fake.read_error:
                    raise fake.read_error
                return fake.answer

        try:
            yield Ch()
        finally:
            self.calls.append(("close",))

    def io(self):
        return [c for c in self.calls if c[0] in ("open", "flush", "write", "read")]


def send(fake):
    return query_version(fake, send=True, confirm=CONFIRM_PHRASE)


# ------------------------------------------------------------------ the request
def test_request_is_the_exact_spec_report():
    assert lb.VERSION_QUERY == bytes([0x00, 0x0A]) + bytes(63) and len(lb.VERSION_QUERY) == 65


@pytest.mark.parametrize("cmd", [0x08, 0x59, 0x51, 0x40, 0x00, 0xFF])
def test_no_other_command_can_be_built(cmd):
    with pytest.raises(Gate1Refused):
        build_request(cmd)


def test_tool_matches_the_reviewed_read_registry():
    entry = REVIEWED_L3_READS["led_bootloader.version_query"]
    assert entry["vid_pid"] == (lb.VID, lb.PID_BOOTLOADER)
    assert entry["commands"] == lb.ALLOWED_COMMANDS == frozenset({0x0A})


# ------------------------------------------------------------------ exactly one exchange
def test_send_does_one_flush_one_write_one_read_and_nothing_else():
    fake = FakeHid()
    res = send(fake)
    assert res.outcome == "reply" and res.sent
    assert fake.io() == [("open", PATH), ("flush",), ("write", lb.VERSION_QUERY, 0.5), ("read", 65, 0.5)]
    assert fake.calls[-1] == ("close",)
    assert res.decoded.valid and res.decoded.app_version == (1, 4, 77)
    assert res.decoded.application_image_evidenced
    assert "1.4.77" in res.message and "ADR-0012" in res.message


def test_dry_run_is_the_default_and_sends_nothing():
    fake = FakeHid()
    res = query_version(fake)
    assert res.outcome == "dry_run" and not res.sent and fake.io() == []
    assert "00 0a 00" in res.message


@pytest.mark.parametrize("confirm", ["", "send 0a to 0dad:0057", "SEND 0A", CONFIRM_PHRASE + " "])
def test_wrong_confirmation_sends_nothing(confirm):
    fake = FakeHid()
    res = query_version(fake, send=True, confirm=confirm)
    assert res.outcome == "refused" and fake.io() == []


@pytest.mark.parametrize("fake,why", [
    (FakeHid(paths=()), "no LED board bootloader"),
    (FakeHid(paths=(PATH, PATH + "2")), "exactly one"),
    (FakeHid(desc=DeviceDescription(PATH, 0x0DAD, 0x0051, 1, 65, 65)), "not 0DAD:0057"),
    (FakeHid(desc=DeviceDescription(PATH, 0x0DAD, 0x0057, 1, 64, 65)), "report sizes"),
    (FakeHid(desc=DeviceDescription(PATH, 0x0DAD, 0x0057, 1, 65, 9)), "report sizes"),
])
def test_preconditions_refuse_before_any_io(fake, why):
    res = send(fake)
    assert res.outcome == "refused" and why in res.message and fake.io() == []


def test_only_the_bootloader_pid_is_ever_looked_up():
    fake = FakeHid()
    send(fake)
    assert [c for c in fake.calls if c[0] == "find"] == [("find", 0x0DAD, 0x0057)]


def test_read_timeout_is_reported_and_never_retried():
    fake = FakeHid(read_error=TimeoutError("no completion within 500 ms"))
    res = send(fake)
    assert res.outcome == "no_reply" and "not retried" in res.message
    assert [c[0] for c in fake.io()].count("write") == 1 and [c[0] for c in fake.io()].count("read") == 1


def test_write_timeout_stops_before_reading():
    fake = FakeHid(write_error=TimeoutError("write stalled"))
    res = send(fake)
    assert res.outcome == "no_reply" and [c[0] for c in fake.io()] == ["open", "flush", "write"]


def test_short_write_stops_before_reading():
    fake = FakeHid(written=10)
    res = send(fake)
    assert res.outcome == "no_reply" and "10 of 65" in res.message
    assert "read" not in [c[0] for c in fake.io()]


def test_busy_is_reported_once_never_looped():
    fake = FakeHid(answer=reply(code=0xF0))
    res = send(fake)
    assert res.decoded.busy and not res.decoded.valid and "not retried" in res.decoded.problem
    assert [c[0] for c in fake.io()].count("write") == 1


# ------------------------------------------------------------------ reply checks (§6 Step A)
def test_reply_checks_follow_the_vendor_rules():
    assert parse_version_reply(reply()).valid
    assert not parse_version_reply(reply()[:64]).valid  # length
    assert "0x8B" in parse_version_reply(reply(code=0x8B)).problem
    assert "header_a" in parse_version_reply(reply(a=1)).problem
    assert "header_b" in parse_version_reply(reply(b=0)).problem


@pytest.mark.parametrize("ver_bytes", [bytes(12), b"\xff" * 12])
def test_blank_application_version_means_do_not_consider_startapp(ver_bytes):
    r = bytearray(reply())
    r[22:34] = ver_bytes
    fake = FakeHid(answer=bytes(r))
    res = send(fake)
    assert res.decoded.valid and res.decoded.app_version_blank and not res.decoded.application_image_evidenced
    assert "must not be considered" in res.message


def test_invalid_reply_means_do_not_consider_startapp():
    res = send(FakeHid(answer=reply(a=0)))
    assert not res.decoded.valid and "must not be considered" in res.message


def test_undecoded_bytes_are_kept_raw():
    d = parse_version_reply(reply(tail=b"\x12\x34"))
    assert d.unidentified_hex["34-64"].startswith("1234") and len(d.unidentified_hex["10-21"]) == 24


# ------------------------------------------------------------------ evidence + CLI
def test_cli_dry_run_writes_evidence_and_sends_nothing(tmp_path, capsys):
    fake = FakeHid()
    rc = lb.main(["--evidence-dir", str(tmp_path)], backend=fake)
    assert rc == 0 and fake.io() == []
    out = capsys.readouterr().out
    assert "[dry_run]" in out and "00 0a 00" in out
    rec = json.loads(next(tmp_path.glob("led-bootloader-gate1-*-dry_run.json")).read_text("utf-8"))
    assert rec["sent"] is False and rec["request_hex"] == lb.VERSION_QUERY.hex()


def test_cli_send_records_raw_bytes(tmp_path):
    fake = FakeHid()
    rc = lb.main(["--send", "--confirm", CONFIRM_PHRASE, "--evidence-dir", str(tmp_path)], backend=fake)
    assert rc == 0
    rec = json.loads(next(tmp_path.glob("led-bootloader-gate1-*-reply.json")).read_text("utf-8"))
    assert rec["sent"] is True and rec["reply_hex"] == reply().hex()
    assert rec["decoded"]["app_version"] == [1, 4, 77] and rec["spec"].endswith("ADR-0011")


def test_cli_send_without_phrase_is_refused_and_writes_nothing(tmp_path):
    fake = FakeHid()
    rc = lb.main(["--send", "--evidence-dir", str(tmp_path)], backend=fake)
    assert rc == 1 and fake.io() == [] and not list(tmp_path.iterdir())


# ------------------------------------------------------------------ Windows transport (no device I/O)
@pytest.mark.skipif(sys.platform != "win32", reason="Windows HID transport")
def test_windows_transport_loads_and_enumeration_is_read_only():
    from openevos.maintenance import win_hid

    # Enumeration only (SetupAPI); opens nothing. An unused VID/PID must find nothing.
    assert win_hid.find_interfaces(0xFFFF, 0xFFFE) == []
