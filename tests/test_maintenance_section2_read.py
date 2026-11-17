"""Gate 2 Step D (ADR-0015): PearlScope's section-2 calibration blob, against a fake device only.

The fake board serves `0x31` reads from a section-2 memory image holding a "lico" header and a
zlib blob. Proves: only the reviewed header reads and in-range 58-byte blob chunks can be built;
each request is sent once; the header gates the blob (magic, length); fail-fast; --header-only;
nothing in a dry run, on any failed precondition or without the exact phrase; offline decode.
"""

from __future__ import annotations

import json
import struct
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from openevos.maintenance import led_section2_read as sd
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.safety.firmware import REVIEWED_L3_READS

APP = r"\\?\hid#vid_0dad&pid_0051#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
NOW = datetime(2026, 11, 12, 16, 30, tzinfo=timezone.utc)
PAYLOAD = b"<PersistentData><Motor x='1'/><Turret p='2'/></PersistentData>" * 20


def image(magic=b"lico", payload=PAYLOAD, length=None, compress=True):
    blob = zlib.compress(payload) if compress else payload
    n = len(blob) if length is None else length
    mem = bytearray(0x15727 + len(blob) + 64)
    mem[0x15713:0x15717] = magic
    mem[0x15717:0x1571F] = struct.pack("<Q", 0x01DB5A0000000000)
    mem[0x1571F:0x15723] = struct.pack("<i", len(payload))
    mem[0x15723:0x15727] = struct.pack("<i", n)
    mem[0x15727:0x15727 + len(blob)] = blob
    return bytes(mem), blob


def frame(code, addr=0, count=0, payload=b""):
    r = bytearray(65)
    r[1] = code
    r[2:5] = addr.to_bytes(3, "big")
    r[5] = count
    r[6:6 + len(payload)] = payload
    r[64] = (-sum(r[1:64])) & 0xFF
    return bytes(r)


class FakeApp:
    def __init__(self, mem=None, busy_after=None, desc=None, apps=(APP,)):
        self.mem = mem if mem is not None else image()[0]
        self.busy_after = busy_after
        self.desc = desc or DeviceDescription(APP, 0x0DAD, 0x0051, 1, 65, 65)
        self.apps = list(apps)
        self.calls: list[tuple] = []
        self.pending: list[bytes] = []

    def find(self, vid, pid):
        return self.apps if pid == 0x0051 else []

    def describe(self, path):
        return self.desc

    @contextmanager
    def open(self, path):
        fake = self

        class Ch:
            def flush_input(self):
                fake.pending = []

            def write(self, report, timeout_s):
                fake.calls.append(("write", bytes(report)))
                assert report[1] == 0x31 and (sum(report[1:65]) & 0xFF) == 0
                addr, count = int.from_bytes(report[2:5], "big"), report[5]
                if fake.busy_after is not None and len(fake.writes()) > fake.busy_after:
                    fake.pending = [frame(0xF0)]
                else:
                    fake.pending = [frame(0xB1, addr, count, fake.mem[addr:addr + count])]
                return 65

            def read(self, length, timeout_s):
                if not fake.pending:
                    raise TimeoutError("no completion")
                return fake.pending.pop(0)

        yield Ch()

    def writes(self):
        return [c[1] for c in self.calls if c[0] == "write"]


def step_b(d, age=timedelta(minutes=5)):
    rec = {"outcome": "reply", "started_utc": (NOW - age).isoformat(timespec="milliseconds"),
           "device": {"path": APP}, "decoded": {"valid": True, "consistent": True, "consistent2": True}}
    (d / f"led-application-stepb-{(NOW - age).strftime('%Y%m%dT%H%M%S')}-reply.json").write_text(json.dumps(rec))


def send(fake, tmp_path, **kw):
    return sd.read_section2(fake, evidence_dir=tmp_path, send=True, confirm=sd.CONFIRM_PHRASE, now=lambda: NOW, **kw)


# ------------------------------------------------------------------ requests
def test_registry_and_request_framing():
    entry = REVIEWED_L3_READS["led_application.section2_blob_read"]
    assert entry["vid_pid"] == (0x0DAD, 0x0051) and entry["commands"] == sd.ALLOWED_COMMANDS == frozenset({0x31})
    r = sd.build_request(0x31, 0x15713, 4)
    assert r[:6] == bytes([0x00, 0x31, 0x01, 0x57, 0x13, 0x04]) and (sum(r[1:65]) & 0xFF) == 0


@pytest.mark.parametrize("cmd,addr,count", [
    (0x51, 0x15713, 4),  # other command
    (0x31, 0x15713, 8),  # header field at the wrong size
    (0x31, 0x15727, 59),  # chunk larger than a reply holds
    (0x31, 0x15728, 58),  # chunk not on a 58-byte boundary
    (0x31, 0x15700, 4),  # before the header
    (0x31, 0x15727 + 58 * 604, 58),  # beyond N max
])
def test_only_reviewed_reads_can_be_built(cmd, addr, count):
    with pytest.raises(sd.StepDRefused):
        sd.build_request(cmd, addr, count)


# ------------------------------------------------------------------ sequence
def test_complete_read_and_offline_decode(tmp_path):
    step_b(tmp_path)
    mem, blob = image()
    fake = FakeApp(mem)
    res, got, decoded = send(fake, tmp_path)
    assert res.outcome == "complete" and got == blob and decoded == PAYLOAD
    assert res.header["magic_ok"] and res.header["stored_length"] == len(blob)
    assert res.decode == {"method": "zlib", "decoded_bytes": len(PAYLOAD), "matches_decoded_size": True}
    sent = fake.writes()
    assert len(sent) == 4 + -(-len(blob) // 58) and len(set(sent)) == len(sent)  # each request once


def test_header_only_stops_before_the_blob(tmp_path):
    step_b(tmp_path)
    fake = FakeApp()
    res, got, _ = send(fake, tmp_path, header_only=True)
    assert res.outcome == "header_only" and got is None and len(fake.writes()) == 4


@pytest.mark.parametrize("mem,why", [
    (image(magic=b"\x00\x00\x00\x00")[0], "not 'lico'"),
    (image(length=0)[0], "outside"),
    (image(length=0x88DA)[0], "outside"),
])
def test_header_gates_the_blob(tmp_path, mem, why):
    step_b(tmp_path)
    fake = FakeApp(mem)
    res, got, _ = send(fake, tmp_path)
    assert res.outcome == "stopped" and why in res.message and len(fake.writes()) == 4 and got is None


def test_fail_fast_mid_blob_keeps_partial_and_never_retries(tmp_path):
    import random

    step_b(tmp_path)
    mem, blob = image(payload=random.Random(1).randbytes(1000))  # incompressible: ~18 chunks
    assert len(blob) > 5 * 58
    fake = FakeApp(mem, busy_after=6)
    res, got, _ = send(fake, tmp_path)
    assert res.outcome == "stopped" and "BUSY" in res.message and "Not retried" in res.message
    assert len(fake.writes()) == 7 and len(got) == 2 * 58


def test_unrecognised_format_is_reported_not_guessed(tmp_path):
    step_b(tmp_path)
    mem, _ = image(compress=False)
    res, _, decoded = send(FakeApp(mem), tmp_path)
    assert res.outcome == "complete" and decoded is None and res.decode["method"] is None


def test_address_echo_is_required():
    reply = frame(0xB1, 0x15717, 4, b"lico")
    assert sd.parse_reply(reply, 0x15713, 4)[1] == "reply does not echo the requested address"


# ------------------------------------------------------------------ preconditions
def test_dry_run_sends_nothing(tmp_path):
    step_b(tmp_path)
    fake = FakeApp()
    res, *_ = sd.read_section2(fake, evidence_dir=tmp_path, now=lambda: NOW)
    assert res.outcome == "dry_run" and fake.writes() == [] and len(res.exchanges) == 4


@pytest.mark.parametrize("setup,confirm", [
    (lambda d: None, sd.CONFIRM_PHRASE),
    (lambda d: step_b(d, age=timedelta(minutes=40)), sd.CONFIRM_PHRASE),
    (step_b, "read section 2"),
])
def test_refusals_send_nothing(tmp_path, setup, confirm):
    setup(tmp_path)
    fake = FakeApp()
    res, *_ = sd.read_section2(fake, evidence_dir=tmp_path, send=True, confirm=confirm, now=lambda: NOW)
    assert res.outcome == "refused" and fake.writes() == []


def test_cli_writes_blob_and_decoded_files(tmp_path):
    step_b(tmp_path)
    mem, blob = image()
    fake = FakeApp(mem)
    import openevos.maintenance.led_section2_read as mod

    orig = mod.read_section2
    mod_read = lambda b, **kw: orig(b, now=lambda: NOW, **kw)
    try:
        mod.read_section2 = mod_read
        assert mod.main(["--send", "--confirm", sd.CONFIRM_PHRASE, "--evidence-dir", str(tmp_path)], backend=fake) == 0
    finally:
        mod.read_section2 = orig
    assert next(tmp_path.glob("led-application-stepd-*-blob.bin")).read_bytes() == blob
    assert next(tmp_path.glob("led-application-stepd-*-decoded.bin")).read_bytes() == PAYLOAD
