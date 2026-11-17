"""LED Gate 5 (ADR-0049): low-level brightness test, fake board only.

Proves: 0x5E can only carry field mask 0x04 (brightfield level) with a level 0..31; the exact sequence; the
original level is always restored and read back; every failure switches the light off and restores once; epi is
refused for an empty slot or DAPI; dry run / phrases / preconditions send nothing.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from openevos.maintenance import led_brightness_test as bt
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.safety.firmware import REVIEWED_MAINTENANCE
from tests.test_maintenance_checksum import APP, framed, start_record

CUBE = {"RFP": 5, "NONE": 1, "DAPI": 3, "GFP": 4}


class Board:
    def __init__(self, bf_level=7, epi_level=1, cube="RFP", ignore_level=False, ignore_off=False, die_after=None):
        self.bf_level, self.epi_level, self.cube = bf_level, epi_level, cube
        self.bf = self.epi = 0
        self.ignore_level, self.ignore_off, self.die_after = ignore_level, ignore_off, die_after
        self.sent: list[bytes] = []
        self.q: list[bytes] = []

    def find(self, vid, pid):
        return [APP] if pid == 0x0051 else []

    def describe(self, path):
        return DeviceDescription(APP, 0x0DAD, 0x0051, 1, 65, 65)

    def reply(self, req: bytes):
        cmd = req[1]
        if self.die_after is not None and len(self.sent) > self.die_after:
            return None
        if cmd == 0x5E:
            assert req[2] == 0x04 and all(b == 0 for i, b in enumerate(req[3:64], 3) if i != 19)
            if not self.ignore_level:
                self.bf_level = req[19]
            return framed(0xDE)
        if cmd == 0x4C:
            if not self.ignore_level:
                self.epi_level = req[2]
            return framed(0xCC, bytes([0, self.epi_level]))
        if cmd == 0x5D:
            p = bytearray(62)
            p[33], p[34], p[35] = self.bf_level, 0, 31
            return framed(0xDD, bytes(p))
        if cmd == 0x4A:
            return framed(0xCA, bytes([self.epi, 0, self.epi_level, 0, 31]))
        if cmd == 0x47:
            return framed(0xC7, bytes([CUBE[self.cube]]))
        if cmd == 0x43:
            if req[3] or not self.ignore_off:
                self.bf = req[3]
            return framed(0xC3, bytes([0, self.bf]))
        if cmd == 0x49:
            if req[2] or not self.ignore_off:
                self.epi = req[2]
            return framed(0xC9, bytes([self.epi]))
        raise AssertionError(f"0x{cmd:02X} must never be sent")

    @contextmanager
    def open(self, path):
        b = self

        class Ch:
            def flush_input(self):
                pass

            def write(self, report, timeout_s):
                b.sent.append(bytes(report))
                r = b.reply(bytes(report))
                if r is not None:
                    b.q.append(r)
                return 65

            def read(self, n, timeout_s):
                if not b.q:
                    raise TimeoutError("no completion")
                return b.q.pop(0)

        yield Ch()

    def cmds(self):
        return [(r[1], r[2], r[19] if r[1] == 0x5E else r[3]) for r in self.sent]


def run(board, tmp_path, **kw):
    start_record(tmp_path)
    kw.setdefault("light", "brightfield")
    return bt.brightness_test(board, evidence_dir=tmp_path, send=True, confirm=bt.CONFIRM_PHRASE,
                              checklist=bt.CHECKLIST_PHRASE, sleep=lambda s: None, **kw)


# ------------------------------------------------------------------ frames and registry
def test_0x5e_can_only_set_the_brightfield_level():
    r = bt.build_request(0x5E, 2)
    assert r[1:3] == bytes([0x5E, 0x04]) and r[19] == 2 and sum(r[1:65]) & 0xFF == 0
    assert [i for i, b in enumerate(r[:64]) if b] == [1, 2, 19]
    for bad in [(0x5E, 32), (0x5E, -1), (0x5E, None), (0x4C, 40), (0x5D, 1), (0x43, 2), (0x41, 1), (0x08, None),
                (0x52, 0), (0x32, 0), (0x5B, 1)]:
        with pytest.raises(bt.BrightnessRefused):
            bt.build_request(*bad)


def test_registered():
    e = REVIEWED_MAINTENANCE["led_application.brightness_test"]
    assert e["commands"] == bt.ALLOWED_COMMANDS and e["module"] == "maintenance/led_brightness_test.py"


# ------------------------------------------------------------------ sequences
def test_brightfield_sequence_restores_original(tmp_path):
    b = Board(bf_level=7)
    res = run(b, tmp_path, level=2)
    assert res.outcome == "complete" and res.original_level == 7 and b.bf_level == 7 and b.bf == 0
    assert b.cmds() == [(0x5D, 0, 0), (0x5E, 4, 2), (0x5D, 0, 0), (0x43, 0, 1), (0x43, 0, 0), (0x5E, 4, 7),
                        (0x5D, 0, 0)]


def test_epi_sequence_checks_cube_and_restores(tmp_path):
    b = Board(epi_level=1, cube="GFP")
    res = run(b, tmp_path, light="epi", level=3)
    assert res.outcome == "complete" and b.epi_level == 1 and b.epi == 0
    assert [c[0] for c in b.cmds()] == [0x4A, 0x47, 0x4C, 0x4A, 0x49, 0x49, 0x4C, 0x4A]


@pytest.mark.parametrize("cube", ["DAPI", "NONE"])
def test_epi_refused_for_dapi_or_empty_slot(tmp_path, cube):
    b = Board(cube=cube)
    res = run(b, tmp_path, light="epi")
    assert res.outcome == "stopped" and cube in res.message
    assert [c[0] for c in b.cmds()] == [0x4A, 0x47]  # read only: no level change, no ON


def test_level_not_taken_restores_and_stops_before_on(tmp_path):
    b = Board(ignore_level=True)
    res = run(b, tmp_path, level=2)
    assert res.outcome == "stopped" and 0x43 not in [c[0] for c in b.cmds()]


def test_unconfirmed_off_says_power_cycle(tmp_path):
    b = Board(ignore_off=True)
    res = run(b, tmp_path, level=2)
    assert res.outcome == "off_failed" and "Power-cycle" in res.message


def test_link_dies_after_on_tries_off_and_restore(tmp_path):
    b = Board(die_after=4)  # replies stop after the ON
    res = run(b, tmp_path, level=2)
    assert res.outcome == "off_failed"
    # the scheduled OFF, then one OFF after failure, then one restore attempt of the original level
    assert [c[0] for c in b.cmds()][-3:] == [0x43, 0x43, 0x5E]


def test_unexpected_range_stops_before_any_write(tmp_path):
    b = Board(bf_level=200)
    res = run(b, tmp_path)
    assert res.outcome == "stopped" and b.cmds() == [(0x5D, 0, 0)]


# ------------------------------------------------------------------ nothing sent
def test_dry_run_and_phrases(tmp_path):
    start_record(tmp_path)
    b = Board()
    assert bt.brightness_test(b, evidence_dir=tmp_path, light="brightfield").outcome == "dry_run" and b.sent == []
    res = bt.brightness_test(b, evidence_dir=tmp_path, light="brightfield", send=True, confirm=bt.CONFIRM_PHRASE)
    assert res.outcome == "refused" and b.sent == []


@pytest.mark.parametrize("kw", [{"level": 32}, {"level": -1}, {"hold_s": 11}, {"light": "dapi"}])
def test_argument_limits(tmp_path, kw):
    b = Board()
    assert run(b, tmp_path, **kw).outcome == "refused" and b.sent == []


def test_needs_start_record(tmp_path):
    b = Board()
    res = bt.brightness_test(b, evidence_dir=tmp_path, light="brightfield", send=True, confirm=bt.CONFIRM_PHRASE,
                             checklist=bt.CHECKLIST_PHRASE)
    assert res.outcome == "refused" and b.sent == []
