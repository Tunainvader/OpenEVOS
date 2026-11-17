"""LED Gate 3 (ADR-0031): read-only status queries 0x40, 0x47, 0x5A, 0x4A — fake device only.

Proves what reaches the device: per command one flush and exactly one 65-byte write, then only
reads; nothing in a dry run, on a failed precondition or without the exact phrase; the run stops at
the first BUSY / timeout / short write and never resends; no other command byte can be built.
"""

from __future__ import annotations

import json
import struct

import pytest

from openevos.maintenance import led_status_read as st
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.safety.firmware import REVIEWED_L3_READS, operation_refusal
from tests.test_maintenance_checksum import APP, BOOT, FakeApp, framed, start_record


def version_reply(major=3, minor=0, build=25949):
    return framed(0xC0, struct.pack(">III", major, minor, build))


def positions_reply():
    p = bytearray(62)
    p[0], p[1], p[2] = 2, 3, 4  # bytes 2, 3, 4
    p[23:27] = bytes([11, 12, 13, 14])  # bytes 25..28
    p[29], p[30] = 2, 1  # bytes 31, 32
    return framed(0xC7, bytes(p))


def epi_channels_reply():
    return framed(0xDA, bytes([1, 4, 0, 1, 0, 0]))


def epi_status_reply():
    p = bytearray(62)
    p[1], p[2], p[3], p[4] = 1, 0x80, 0, 0xFF  # bytes 3..6
    p[5:11] = bytes([0x00, 0x02, 0x01, 0x2C, 0x03, 0xE8])  # bytes 7..12
    p[19], p[22] = 0x55, 0  # bytes 21, 24
    return framed(0xCA, bytes(p))


ALL_REPLIES = [version_reply(), positions_reply(), epi_channels_reply(), epi_status_reply()]


def send(fake, tmp_path):
    return st.read_status(fake, evidence_dir=tmp_path, send=True, confirm=st.CONFIRM_PHRASE)


# ------------------------------------------------------------------ requests and registry
def test_requests_are_bare_commands_with_twos_complement_checksum():
    for cmd in st.SEQUENCE:
        r = st.REQUESTS[cmd]
        assert len(r) == 65 and r[0] == 0 and r[1] == cmd and r[2:64] == bytes(62)
        assert sum(r[1:65]) & 0xFF == 0


@pytest.mark.parametrize("cmd", [0x08, 0x0A, 0x31, 0x32, 0x33, 0x41, 0x46, 0x48, 0x49, 0x4B, 0x4C, 0x51,
                                 0x52, 0x54, 0x59, 0x5B, 0x5D, 0x5E, 0x60, 0x00, 0xFF])
def test_no_other_command_can_be_built(cmd):
    with pytest.raises(st.StatusReadRefused):
        st.build_request(cmd)


def test_tool_matches_the_reviewed_read_registry():
    entry = REVIEWED_L3_READS["led_application.gate3_status_read"]
    assert entry["vid_pid"] == (st.VID, st.PID_APPLICATION)
    assert entry["commands"] == st.ALLOWED_COMMANDS == frozenset({0x40, 0x47, 0x5A, 0x4A})
    assert entry["adr"] == "ADR-0031"
    assert operation_refusal("led_application.gate3_status_read") is None


# ------------------------------------------------------------------ the happy path
def test_four_reads_in_order_and_decoded(tmp_path):
    start_record(tmp_path)
    fake = FakeApp(replies=list(ALL_REPLIES))
    res = send(fake, tmp_path)
    assert res.outcome == "complete" and "3.0.25949" in res.message and "CANDIDATE" in res.message
    expected = []
    for cmd in st.SEQUENCE:
        expected += [("flush",), ("write", st.REQUESTS[cmd], 0.5), ("read", 65)]
    assert fake.io() == [("open", APP)] + expected
    v, pos, ch, epi = (ex.decoded.fields for ex in res.exchanges)
    assert (v["major"], v["minor"], v["build"]) == (3, 0, 25949)
    assert (pos["filter_cube_position"], pos["objective_position"], pos["filter_cube_positions"]) == (2, 3, 4)
    assert pos["cube_type_by_position"] == [11, 12, 13, 14] and pos["phase_ring_position"] == 1
    # mode 1: the firmware does not write bytes 3..7 (stale buffer), so they are not decoded
    assert ch["epi_mode"] == 1 and ch["epi_fixed_output_channels"] is None and ch["epi_channel_state"] is None
    assert epi["epi_digipot"] == 0x80 and epi["epi_digipot_range"] == [0, 0xFF]
    assert (epi["epi_current_mode"], epi["pwm_width_epi"], epi["pwm_period_epi"]) == (2, 300, 1000)
    assert epi["epi_digipot_hardware_value"] == 0x55


def test_unrelated_reports_are_ignored_and_never_trigger_a_resend(tmp_path):
    start_record(tmp_path)
    fake = FakeApp(replies=[framed(0xD9), version_reply()] + ALL_REPLIES[1:])
    res = send(fake, tmp_path)
    assert res.outcome == "complete" and len(res.exchanges[0].ignored_reports_hex) == 1
    assert len(fake.writes()) == 4


# ------------------------------------------------------------------ stop on first failure
@pytest.mark.parametrize("bad,why", [(framed(0xF0), "BUSY"), (None, "no matching reply")])
def test_failure_stops_the_run_without_resend(tmp_path, bad, why):
    start_record(tmp_path)
    replies = [version_reply()] + ([bad] if bad else [])
    fake = FakeApp(replies=replies)
    res = send(fake, tmp_path)
    assert res.outcome == "stopped" and why in res.message and "not resent" in res.message
    assert [w[1][1] for w in fake.writes()] == [0x40, 0x47]


def test_at_most_five_reads_per_command(tmp_path):
    start_record(tmp_path)
    fake = FakeApp(replies=[framed(0xD9)] * 9)
    res = send(fake, tmp_path)
    assert res.outcome == "stopped" and [c[0] for c in fake.io()].count("read") == 5 and len(fake.writes()) == 1


def test_write_failure_and_short_write_stop_before_reading(tmp_path):
    start_record(tmp_path)
    for fake in (FakeApp(write_error=TimeoutError("stalled")), FakeApp(written=7)):
        res = send(fake, tmp_path)
        assert res.outcome == "stopped" and "read" not in [c[0] for c in fake.io()] and len(fake.writes()) == 1


# ------------------------------------------------------------------ preconditions: nothing sent
def test_dry_run_is_the_default(tmp_path):
    start_record(tmp_path)
    fake = FakeApp()
    res = st.read_status(fake, evidence_dir=tmp_path)
    assert res.outcome == "dry_run" and fake.io() == [] and "0x40, 0x47, 0x5A, 0x4A" in res.message


@pytest.mark.parametrize("confirm", ["", st.CONFIRM_PHRASE.lower(), st.CONFIRM_PHRASE + " "])
def test_exact_phrase_required(tmp_path, confirm):
    start_record(tmp_path)
    fake = FakeApp()
    res = st.read_status(fake, evidence_dir=tmp_path, send=True, confirm=confirm)
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


# ------------------------------------------------------------------ parsing
def test_enum_names_decode_the_instruments_2026_11_13_reply():
    """Gate 3 on the instrument: 0x47 bytes 2..4 = 05 02 04, 25..28 = 05 01 03 04; 0x4A bytes 7..8 = FF FD."""
    p = bytearray(62)
    p[0:3] = bytes([5, 2, 4])
    p[23:27] = bytes([5, 1, 3, 4])
    f = st.parse_reply(0x47, framed(0xC7, bytes(p))).fields
    assert f["cube_name_by_position"] == ["RFP", "NONE", "DAPI", "GFP"]
    assert f["filter_cube_in_path_name"] == "RFP" and f["objective_name"] == "4X" and f["phase_ring_name"] == "DISENGAGED"
    e = bytearray(62)
    e[5:7] = b"\xff\xfd"
    assert st.parse_reply(0x4A, framed(0xCA, bytes(e))).fields["epi_current_mode_name"] == "DIGIPOT"


def test_channel_bytes_decoded_only_in_fixed_output_mode():
    """Firmware handler 0x5A (PC 0x2F28) writes bytes 3..7 only when epi mode == 2."""
    f = st.parse_reply(0x5A, framed(0xDA, bytes([2, 4, 0, 1, 0, 0]))).fields
    assert f["epi_fixed_output_channels"] == 4 and f["epi_channel_state"] == [0, 1, 0, 0]
    assert f["written_bytes"] == [2, 7]


def test_live_reply_residue_is_not_decoded():
    """The board's 2026-11-13 0x47 reply: bytes 2..34 written, from byte 36 bootloader residue (43 F3)."""
    live = bytes.fromhex("0502040180000003ff000000030300018003ff0134015a050103040701010000010043f3"
                         "0000000000000003000000000000655d00000000000000000000")
    r = framed(0xC7, live)
    f = st.parse_reply(0x47, r).fields
    assert f["epi_mode"] == 1 and f["epi_channels"] is None and f["written_bytes"] == [2, 34]
    assert (f["objective_sensor_raw"], f["cube_sensor_raw"], f["phase_sensor_raw"]) == (7, 1, 0)


def test_reply_parsing_rules():
    assert st.parse_reply(0x40, version_reply()).valid
    assert not st.parse_reply(0x40, version_reply()[:64]).valid
    assert "0xC7" in st.parse_reply(0x40, positions_reply()).problem
    bad = bytearray(version_reply())
    bad[64] ^= 1
    assert "frame checksum" in st.parse_reply(0x40, bytes(bad)).problem


# ------------------------------------------------------------------ CLI
def test_cli_send_records_evidence(tmp_path, capsys):
    start_record(tmp_path)
    rc = st.main(["--send", "--confirm", st.CONFIRM_PHRASE, "--evidence-dir", str(tmp_path)],
                 backend=FakeApp(replies=list(ALL_REPLIES)))
    assert rc == 0
    rec = json.loads(next(tmp_path.glob(f"{st.EVIDENCE_PREFIX}-*-complete.json")).read_text("utf-8"))
    assert rec["sent"] and len(rec["exchanges"]) == 4 and "[complete]" in capsys.readouterr().out


def test_cli_dry_run_and_refusal(tmp_path):
    start_record(tmp_path)
    fake = FakeApp()
    assert st.main(["--evidence-dir", str(tmp_path)], backend=fake) == 0 and fake.io() == []
    assert st.main(["--send", "--evidence-dir", str(tmp_path)], backend=fake) == 1 and fake.io() == []
