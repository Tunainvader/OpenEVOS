"""Gate 2 Step B (ADR-0013): the LED application's calibration-memory checksum query, fake device only.

Proves what reaches the device: one flush, exactly one 65-byte write of `00 59 00… 59`, then
only reads (at most 5, within 1 s); nothing in a dry run, on any failed precondition or without
the exact phrase; never a second write, also not on BUSY or after unrelated reports.
"""

from __future__ import annotations

import json
import struct
from contextlib import contextmanager

import pytest

from openevos.maintenance import led_checksum as cs
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.maintenance.led_startapp import EVIDENCE_PREFIX
from openevos.safety.firmware import REVIEWED_L3_READS

APP = r"\\?\hid#vid_0dad&pid_0051#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
BOOT = r"\\?\hid#vid_0dad&pid_0057#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"


def framed(code: int, payload: bytes = b"") -> bytes:
    r = bytearray(65)
    r[1] = code
    r[2:2 + len(payload)] = payload
    r[64] = (-sum(r[1:64])) & 0xFF  # two's complement: bytes 1..64 sum to zero
    return bytes(r)


# The board's real BUSY reply to the first (wrongly framed) request, 2026-11-12.
REAL_BUSY = bytes([0x00, 0xF0]) + bytes(62) + bytes([0x10])


def checksum_reply(stored=0x1234ABCD, computed=0x1234ABCD, stored2=0x00C0FFEE, computed2=0x00C0FFEE):
    return framed(0xD9, struct.pack(">IIII", stored, computed, stored2, computed2))


class FakeApp:
    def __init__(self, replies=None, apps=(APP,), boot=(), alt=(), desc=None, write_error=None, written=65):
        self.replies = list(replies if replies is not None else [checksum_reply()])
        self.apps, self.boot, self.alt = list(apps), list(boot), list(alt)
        self.desc = desc or DeviceDescription(APP, 0x0DAD, 0x0051, 1, 65, 65)
        self.write_error, self.written = write_error, written
        self.calls: list[tuple] = []

    def find(self, vid, pid):
        self.calls.append(("find", vid, pid))
        return {0x0051: self.apps, 0x0057: self.boot, 0x0052: self.alt}.get(pid, [])

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
                fake.calls.append(("read", length))
                if not fake.replies:
                    raise TimeoutError("no completion within 500 ms")
                return fake.replies.pop(0)

        try:
            yield Ch()
        finally:
            self.calls.append(("close",))

    def io(self):
        return [c for c in self.calls if c[0] in ("open", "flush", "write", "read")]

    def writes(self):
        return [c for c in self.calls if c[0] == "write"]


def start_record(dirpath, path=APP):
    rec = {"outcome": "app_appeared", "after": {"0DAD:0057": [], "0DAD:0051": [path], "0DAD:0052": []}}
    (dirpath / f"{EVIDENCE_PREFIX}-20261112T145644-app_appeared.json").write_text(json.dumps(rec), "utf-8")


def send(fake, tmp_path):
    return cs.query_checksum(fake, evidence_dir=tmp_path, send=True, confirm=cs.CONFIRM_PHRASE)


# ------------------------------------------------------------------ the request
def test_request_is_00_59_with_the_twos_complement_frame_checksum():
    assert cs.CHECKSUM_QUERY == bytes([0x00, 0x59]) + bytes(62) + bytes([0xA7])
    assert len(cs.CHECKSUM_QUERY) == 65 and sum(cs.CHECKSUM_QUERY[1:65]) & 0xFF == 0


def test_checksum_rule_matches_the_boards_own_reply():
    """The board's BUSY frame `00 F0 … 10` is valid under the two's-complement rule, not the plain sum."""
    assert cs.frame_checksum(REAL_BUSY) == REAL_BUSY[64] == 0x10
    assert sum(REAL_BUSY[1:64]) & 0xFF != REAL_BUSY[64]
    assert cs.parse_reply(REAL_BUSY).busy


@pytest.mark.parametrize("cmd", [0x51, 0x40, 0x08, 0x0A, 0x00, 0xFF])
def test_no_other_command_can_be_built(cmd):
    with pytest.raises(cs.StepBRefused):
        cs.build_request(cmd)


def test_tool_matches_the_reviewed_read_registry():
    entry = REVIEWED_L3_READS["led_application.calibration_checksum"]
    assert entry["vid_pid"] == (cs.VID, cs.PID_APPLICATION)
    assert entry["commands"] == cs.ALLOWED_COMMANDS == frozenset({0x59})


# ------------------------------------------------------------------ one write, reads only
def test_consistent_checksums(tmp_path):
    start_record(tmp_path)
    fake = FakeApp()
    res = send(fake, tmp_path)
    assert res.outcome == "reply" and res.decoded.consistent and res.decoded.stored == 0x1234ABCD
    assert fake.io() == [("open", APP), ("flush",), ("write", cs.CHECKSUM_QUERY, 0.5), ("read", 65)]
    assert "internally consistent" in res.message and "does not prove" in res.message


def test_section_two_is_decoded_from_bytes_10_to_17(tmp_path):
    """Slot 137 GetPersistentDataSection2Checksum sends the same 0x59 and reads reply bytes 10–17."""
    start_record(tmp_path)
    res = send(FakeApp(replies=[checksum_reply(stored2=5, computed2=6)]), tmp_path)
    d = res.decoded
    assert d.consistent and (d.stored2, d.computed2, d.consistent2) == (5, 6, False)
    assert "Section 2" in res.message and "DIFFER" in res.message and len(d.unidentified_hex) == 2 * 46


def test_inconsistent_checksums_say_do_not_trust(tmp_path):
    start_record(tmp_path)
    res = send(FakeApp(replies=[checksum_reply(1, 2)]), tmp_path)
    assert res.outcome == "reply" and not res.decoded.consistent and "Do not trust" in res.message


def test_unrelated_reports_are_ignored_kept_and_never_trigger_a_resend(tmp_path):
    start_record(tmp_path)
    other = framed(0xC0, b"\x00\x00\x00\x03")
    bad_ck = bytearray(checksum_reply())
    bad_ck[64] ^= 0xFF
    fake = FakeApp(replies=[other, bytes(bad_ck), checksum_reply()])
    res = send(fake, tmp_path)
    assert res.decoded.consistent and len(res.ignored_reports_hex) == 2 and len(fake.writes()) == 1


def test_at_most_five_reads_and_no_resend_if_nothing_matches(tmp_path):
    start_record(tmp_path)
    fake = FakeApp(replies=[framed(0xC0)] * 9)
    res = send(fake, tmp_path)
    assert res.outcome == "no_reply" and "not resent" in res.message
    assert [c[0] for c in fake.io()].count("read") == 5 and len(fake.writes()) == 1


def test_read_timeout_is_reported_not_resent(tmp_path):
    start_record(tmp_path)
    fake = FakeApp(replies=[])
    res = send(fake, tmp_path)
    assert res.outcome == "no_reply" and len(fake.writes()) == 1


def test_busy_is_reported_once(tmp_path):
    start_record(tmp_path)
    fake = FakeApp(replies=[framed(0xF0)])
    res = send(fake, tmp_path)
    assert res.decoded.busy and "BUSY" in res.message and len(fake.writes()) == 1


def test_write_failure_and_short_write_stop_before_reading(tmp_path):
    start_record(tmp_path)
    for fake in (FakeApp(write_error=TimeoutError("stalled")), FakeApp(written=7)):
        res = send(fake, tmp_path)
        assert res.outcome == "no_reply" and "read" not in [c[0] for c in fake.io()]


# ------------------------------------------------------------------ preconditions: nothing sent
def test_dry_run_is_the_default(tmp_path):
    start_record(tmp_path)
    fake = FakeApp()
    res = cs.query_checksum(fake, evidence_dir=tmp_path)
    assert res.outcome == "dry_run" and fake.io() == [] and res.app_start_record


@pytest.mark.parametrize("confirm", ["", "send 59 to 0dad:0051", cs.CONFIRM_PHRASE + "."])
def test_exact_phrase_required(tmp_path, confirm):
    start_record(tmp_path)
    fake = FakeApp()
    res = cs.query_checksum(fake, evidence_dir=tmp_path, send=True, confirm=confirm)
    assert res.outcome == "refused" and fake.io() == []


@pytest.mark.parametrize("fake,why", [
    (FakeApp(apps=()), "found 0"),
    (FakeApp(apps=(APP, APP + "2")), "found 2"),
    (FakeApp(boot=(BOOT,)), "unexpected interface"),
    (FakeApp(alt=(APP,)), "unexpected interface"),
    (FakeApp(desc=DeviceDescription(APP, 0x0DAD, 0x0057, 1, 65, 65)), "not 0DAD:0051"),
    (FakeApp(desc=DeviceDescription(APP, 0x0DAD, 0x0051, 1, 64, 65)), "report sizes"),
])
def test_device_preconditions(tmp_path, fake, why):
    start_record(tmp_path)
    res = send(fake, tmp_path)
    assert res.outcome == "refused" and why in res.message and fake.io() == []


def test_needs_a_start_record_for_this_interface(tmp_path):
    fake = FakeApp()
    assert "no StartApp record" in send(fake, tmp_path).message and fake.io() == []
    start_record(tmp_path, path=APP + "-other")
    assert send(fake, tmp_path).outcome == "refused" and fake.io() == []


# ------------------------------------------------------------------ reply parsing
def test_reply_parsing_rules():
    assert cs.parse_reply(checksum_reply()).valid
    assert not cs.parse_reply(checksum_reply()[:64]).valid
    assert "0xC0" in cs.parse_reply(framed(0xC0)).problem
    bad = bytearray(checksum_reply())
    bad[64] ^= 1
    assert "frame checksum" in cs.parse_reply(bytes(bad)).problem


# ------------------------------------------------------------------ CLI
def test_cli_send_records_evidence(tmp_path, capsys):
    start_record(tmp_path)
    rc = cs.main(["--send", "--confirm", cs.CONFIRM_PHRASE, "--evidence-dir", str(tmp_path)], backend=FakeApp())
    assert rc == 0
    rec = json.loads(next(tmp_path.glob("led-application-stepb-*-reply.json")).read_text("utf-8"))
    assert rec["sent"] and rec["request_hex"] == cs.CHECKSUM_QUERY.hex() and rec["decoded"]["consistent"]
    assert "[reply]" in capsys.readouterr().out


def test_cli_refusal_writes_nothing(tmp_path):
    fake = FakeApp()
    assert cs.main(["--send", "--confirm", cs.CONFIRM_PHRASE, "--evidence-dir", str(tmp_path)], backend=fake) == 1
    assert fake.io() == [] and not list(tmp_path.glob("led-application-stepb-*"))
