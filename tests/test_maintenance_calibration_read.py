"""Gate 2 Step C (ADR-0014): field-by-field HW_DATA reads, against a fake device only.

The fake board answers `0x51` reads from a small memory image and records every call. Proves:
only mapped (address, size) reads can be built; one write per field, never repeated; reads only
otherwise; fail-fast on timeout / BUSY / no match; nothing in a dry run, on any failed
precondition or without the exact phrase; correct two's-complement framing and decoding.
"""

from __future__ import annotations

import json
import struct
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from openevos.maintenance import led_calibration_read as sc
from openevos.maintenance.hwdata_fields import HW_DATA_FIELDS
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.safety.firmware import REVIEWED_L3_READS

APP = r"\\?\hid#vid_0dad&pid_0051#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
NOW = datetime(2026, 11, 12, 15, 30, tzinfo=timezone.utc)
FIELDS = {name: (addr, size) for _, name, addr, size in HW_DATA_FIELDS}


def frame(code, addr=0, count=0, payload=b""):
    r = bytearray(65)
    r[1] = code
    r[2:5] = addr.to_bytes(3, "big")
    r[5] = count
    r[6:6 + len(payload)] = payload
    r[64] = (-sum(r[1:64])) & 0xFF
    return bytes(r)


def memory_image():
    mem = {}
    for _, name, addr, size in HW_DATA_FIELDS:
        mem[addr] = struct.pack("<d", 1.25) if size == 8 else struct.pack("<i", addr)
    mem[FIELDS["HW_DATA__X_POS_MIN"][0]] = struct.pack("<i", -1000)
    mem[FIELDS["HW_DATA__X_POS_MAX"][0]] = struct.pack("<i", 90000)
    return mem


class FakeApp:
    def __init__(self, mem=None, busy_at=None, silent_at=None, noise_before=0, desc=None, apps=(APP,), boot=()):
        self.mem = mem or memory_image()
        self.busy_at, self.silent_at, self.noise_before = busy_at, silent_at, noise_before
        self.desc = desc or DeviceDescription(APP, 0x0DAD, 0x0051, 1, 65, 65)
        self.apps, self.boot = list(apps), list(boot)
        self.calls: list[tuple] = []
        self.pending: list[bytes] = []

    def find(self, vid, pid):
        self.calls.append(("find", pid))
        return {0x0051: self.apps, 0x0057: self.boot}.get(pid, [])

    def describe(self, path):
        return self.desc

    @contextmanager
    def open(self, path):
        self.calls.append(("open", path))
        fake = self

        class Ch:
            def flush_input(self):
                fake.calls.append(("flush",))
                fake.pending = []

            def write(self, report, timeout_s):
                fake.calls.append(("write", bytes(report)))
                assert (sum(report[1:65]) & 0xFF) == 0, "request frame checksum must make bytes 1..64 sum to 0"
                addr, count = int.from_bytes(report[2:5], "big"), report[5]
                if addr == fake.silent_at:
                    fake.pending = []
                elif addr == fake.busy_at:
                    fake.pending = [frame(0xF0)]
                else:
                    fake.pending = [frame(0xC0)] * fake.noise_before + [frame(0xD1, addr, count, fake.mem[addr])]
                return 65

            def read(self, length, timeout_s):
                fake.calls.append(("read",))
                if not fake.pending:
                    raise TimeoutError("no completion within 500 ms")
                return fake.pending.pop(0)

        try:
            yield Ch()
        finally:
            self.calls.append(("close",))

    def writes(self):
        return [c[1] for c in self.calls if c[0] == "write"]


def step_b_record(d, path=APP, age=timedelta(minutes=5), c1=True, c2=True):
    rec = {"outcome": "reply", "started_utc": (NOW - age).isoformat(timespec="milliseconds"),
           "device": {"path": path}, "decoded": {"valid": True, "consistent": c1, "consistent2": c2}}
    (d / f"led-application-stepb-{(NOW - age).strftime('%Y%m%dT%H%M%S')}-reply.json").write_text(json.dumps(rec))


def run(fake, tmp_path, **kw):
    return sc.read_fields(fake, evidence_dir=tmp_path, now=lambda: NOW, **kw)


def send(fake, tmp_path):
    return run(fake, tmp_path, send=True, confirm=sc.CONFIRM_PHRASE)


# ------------------------------------------------------------------ requests
def test_field_map_is_the_documented_76():
    assert len(HW_DATA_FIELDS) == 76 and len(sc.ALLOWED_READS) == 76
    assert {s for *_, s in HW_DATA_FIELDS} == {4, 8}
    assert min(a for _, _, a, _ in HW_DATA_FIELDS) == 0x141D and max(a for _, _, a, _ in HW_DATA_FIELDS) == 0x15BE


def test_request_framing():
    r = sc.build_request(0x51, 0x1451, 4)
    assert r[:6] == bytes([0x00, 0x51, 0x00, 0x14, 0x51, 0x04]) and r[6:64] == bytes(58)
    assert (sum(r[1:65]) & 0xFF) == 0


@pytest.mark.parametrize("cmd,addr,count", [
    (0x52, 0x1451, 4),  # any other command
    (0x51, 0x1451, 8),  # wrong size for this field
    (0x51, 0x1469, 1),  # the unmapped 1-byte read
    (0x51, 0x1452, 4),  # unaligned / not a field
    (0x51, 0x141D, 58),  # bulk read
])
def test_only_mapped_field_reads_can_be_built(cmd, addr, count):
    with pytest.raises(sc.StepCRefused):
        sc.build_request(cmd, addr, count)


def test_tool_matches_registry():
    entry = REVIEWED_L3_READS["led_application.calibration_read"]
    assert entry["vid_pid"] == (0x0DAD, 0x0051) and entry["commands"] == sc.ALLOWED_COMMANDS == frozenset({0x51})


# ------------------------------------------------------------------ the sequence
def test_complete_read_one_write_per_field_and_decoding(tmp_path):
    step_b_record(tmp_path)
    fake = FakeApp()
    res = send(fake, tmp_path)
    assert res.outcome == "complete" and res.sent_requests == 76
    sent = fake.writes()
    assert len(sent) == 76 and len({w for w in sent}) == 76  # each field requested exactly once
    values = {f.name: f.value for f in res.fields}
    assert values["HW_DATA__COLORCAMERA__RED_GAIN"] == 1.25 and values["HW_DATA__X_POS_MIN"] == -1000
    assert res.checks["X min < max"] == "pass" and res.checks["all doubles finite"] == "pass"
    assert res.capability_source == "CANDIDATE" and res.units.startswith("UNKNOWN")


def test_unrelated_reports_are_ignored_without_resending(tmp_path):
    step_b_record(tmp_path)
    fake = FakeApp(noise_before=2)
    res = send(fake, tmp_path)
    assert res.outcome == "complete" and len(fake.writes()) == 76
    assert all(len(f.ignored_hex) == 2 for f in res.fields)


@pytest.mark.parametrize("kw,why", [
    ({"busy_at": FIELDS["HW_DATA__X_POS_MIN"][0]}, "BUSY"),
    ({"silent_at": FIELDS["HW_DATA__X_POS_MIN"][0]}, "no reply"),
])
def test_fail_fast_stops_the_whole_sequence(tmp_path, kw, why):
    step_b_record(tmp_path)
    fake = FakeApp(**kw)
    res = send(fake, tmp_path)
    assert res.outcome == "stopped" and why in res.message and "Not retried" in res.message
    stop_index = [n for _, n, _, _ in HW_DATA_FIELDS].index("HW_DATA__X_POS_MIN")
    assert len(fake.writes()) == stop_index + 1 == res.sent_requests


def test_wrong_count_echo_is_not_accepted(tmp_path):
    payload, why, busy = sc.parse_reply(frame(0xD1, 0x1451, 8, bytes(8)), 4)
    assert payload is None and "count" in why and not busy
    bad = bytearray(frame(0xD1, 0x1451, 4, bytes(4)))
    bad[64] ^= 1
    assert sc.parse_reply(bytes(bad), 4)[1] == "frame checksum invalid"


def test_sanity_checks_flag_problems():
    vals = {"HW_DATA__X_POS_MIN": 10, "HW_DATA__X_POS_MAX": 5, "HW_DATA__X_AXIS_MM_PER_REVOLUTION": float("nan")}
    c = sc.sanity_checks(vals)
    assert c["X min < max"] == "FAIL" and c["X mm/rev > 0 and finite"] == "FAIL" and c["Y min < max"] == "n/a"


# ------------------------------------------------------------------ preconditions
def test_dry_run_sends_nothing_and_lists_the_plan(tmp_path):
    step_b_record(tmp_path)
    fake = FakeApp()
    res = run(fake, tmp_path)
    assert res.outcome == "dry_run" and len(res.fields) == 76 and fake.writes() == []
    assert not any(c[0] == "open" for c in fake.calls)


@pytest.mark.parametrize("make,why", [
    (lambda d: None, "no Step B reply record"),
    (lambda d: step_b_record(d, age=timedelta(minutes=31)), "older than 30 min"),
    (lambda d: step_b_record(d, path=APP + "x"), "no Step B reply record"),
    (lambda d: step_b_record(d, c2=False), "both sections consistent"),
    (lambda d: step_b_record(d, c1=False), "both sections consistent"),
])
def test_needs_recent_consistent_step_b_for_this_interface(tmp_path, make, why):
    make(tmp_path)
    fake = FakeApp()
    res = send(fake, tmp_path)
    assert res.outcome == "refused" and why in res.message and fake.writes() == []


@pytest.mark.parametrize("fake", [FakeApp(apps=()), FakeApp(boot=("x",)),
                                  FakeApp(desc=DeviceDescription(APP, 0x0DAD, 0x0051, 1, 64, 65))])
def test_device_preconditions(tmp_path, fake):
    step_b_record(tmp_path)
    assert send(fake, tmp_path).outcome == "refused" and fake.writes() == []


@pytest.mark.parametrize("confirm", ["", sc.CONFIRM_PHRASE.lower(), "READ HW_DATA"])
def test_exact_phrase(tmp_path, confirm):
    step_b_record(tmp_path)
    fake = FakeApp()
    assert run(fake, tmp_path, send=True, confirm=confirm).outcome == "refused" and fake.writes() == []
