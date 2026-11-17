"""LED Gate 4 (ADR-0034): brightfield / epi on-off test, fake device only.

Proves: only 0x43/0x49 with value 0/1 and reads 0x45/0x4A can be built; the exact order; each request once;
on any failure every light that may be on gets one OFF; dry run / phrases / preconditions send nothing.
"""

from __future__ import annotations

import json

import pytest

from openevos.maintenance import led_light_test as lt
from openevos.safety.firmware import REVIEWED_MAINTENANCE
from tests.test_maintenance_checksum import APP, BOOT, FakeApp, framed, start_record


class Board:
    """Reply generator: echoes the enable state the requests set (a minimal board model)."""

    def __init__(self, fail_on=None, off_readback=0):
        self.bf = self.epi = 0
        self.fail_on, self.off_readback = fail_on, off_readback

    def reply(self, req: bytes):
        cmd = req[1]
        if self.fail_on is not None and self.fail_on == (cmd, req[2] or req[3]):
            return None
        if cmd == lt.CMD_BF_ENABLE:
            self.bf = req[3]
            return framed(0xC3, bytes([0, self.bf if self.bf else self.off_readback]))
        if cmd == lt.CMD_EPI_ENABLE:
            self.epi = req[2]
            return framed(0xC9, bytes([self.epi if self.epi else self.off_readback, 0, 1]))
        if cmd == lt.CMD_BF_STATUS:
            return framed(0xC5, bytes([0, self.bf, 0x02, 0x8E, 0x60, 0x00, 0, 1]))
        return framed(0xCA, bytes([self.epi, 0, 1, 0, 0x1F, 0xFF, 0xFD]))


class FakeBoard(FakeApp):
    def __init__(self, board=None, **kw):
        super().__init__(replies=[], **kw)
        self.board = board or Board()
        self.pending = []

    def open(self, path):
        cm = super().open(path)
        fake = self

        class Wrap:
            def __enter__(self_inner):
                ch = cm.__enter__()
                orig_write, orig_read = ch.write, ch.read

                def write(report, timeout_s):
                    n = orig_write(report, timeout_s)
                    r = fake.board.reply(bytes(report))
                    if r is not None:
                        fake.replies.append(r)
                    return n

                def read(length, timeout_s):
                    return orig_read(length, timeout_s)

                ch.write, ch.read = write, read
                return ch

            def __exit__(self_inner, *a):
                return cm.__exit__(*a)

        return Wrap()


def run(fake, tmp_path, **kw):
    return lt.light_test(fake, evidence_dir=tmp_path, send=True, confirm=lt.CONFIRM_PHRASE,
                         checklist=lt.CHECKLIST_PHRASE, sleep=lambda s: None, **kw)


def sent(fake):
    return [(w[1][1], w[1][2], w[1][3]) for w in fake.writes()]


# ------------------------------------------------------------------ requests and registry
def test_only_on_off_values_can_be_built():
    assert lt.build_request(0x43, 1)[1:4] == bytes([0x43, 0, 1])
    assert lt.build_request(0x49, 1)[1:3] == bytes([0x49, 1])
    for r in (lt.build_request(0x43, 0), lt.build_request(0x45), lt.build_request(0x49, 0)):
        assert len(r) == 65 and sum(r[1:65]) & 0xFF == 0
    for bad in [(0x43, 2), (0x49, 2), (0x49, None), (0x45, 1), (0x4C, 1), (0x41, 1), (0x42, 1), (0x44, 1),
                (0x46, 1), (0x52, 0), (0x32, 0), (0x5E, 0), (0x08, None)]:
        with pytest.raises(lt.LightTestRefused):
            lt.build_request(*bad)


def test_registered_as_reviewed_maintenance():
    e = REVIEWED_MAINTENANCE["led_application.light_test"]
    assert e["commands"] == lt.ALLOWED_COMMANDS and e["module"] == "maintenance/led_light_test.py"
    assert e["vid_pid"] == (0x0DAD, 0x0051)


# ------------------------------------------------------------------ the sequence
def test_full_sequence_in_order_each_once(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard()
    holds = []
    res = lt.light_test(fake, evidence_dir=tmp_path, send=True, confirm=lt.CONFIRM_PHRASE,
                        checklist=lt.CHECKLIST_PHRASE, hold_s=2.5, sleep=holds.append)
    assert res.outcome == "complete" and res.lights_possibly_on == [] and holds == [2.5, 2.5]
    assert sent(fake) == [(0x45, 0, 0), (0x4A, 0, 0), (0x43, 0, 1), (0x43, 0, 0), (0x49, 1, 0), (0x49, 0, 0),
                          (0x45, 0, 0), (0x4A, 0, 0)]
    on = next(s for s in res.steps if s.name == "brightfield ON")
    assert on.fields == {"bf_enable_readback": 1}
    assert res.steps[-1].fields["epi_current_mode_name"] == "DIGIPOT"


def test_failure_after_on_sends_one_off_and_stops(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard(Board(fail_on=(0x49, 1)))  # epi ON gets no reply
    res = run(fake, tmp_path)
    assert res.outcome == "stopped" and "epi ON" in res.message
    assert sent(fake)[-2:] == [(0x49, 1, 0), (0x49, 0, 0)]  # the OFF after failure, once
    assert len(sent(fake)) == 6


def test_off_without_confirmation_says_power_cycle(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard(Board(off_readback=1))  # board keeps reporting "on"
    res = run(fake, tmp_path)
    assert res.outcome == "off_failed" and "Power-cycle" in res.message
    assert res.lights_possibly_on == ["brightfield"]
    assert sent(fake) == [(0x45, 0, 0), (0x4A, 0, 0), (0x43, 0, 1), (0x43, 0, 0), (0x43, 0, 0)]


def test_busy_on_first_read_stops_with_nothing_switched(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard()
    fake.board.reply = lambda req: framed(0xF0)
    res = run(fake, tmp_path)
    assert res.outcome == "stopped" and len(fake.writes()) == 1 and res.lights_possibly_on == []


def test_brightfield_only_long_hold_never_touches_epi(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard()
    holds, announced = [], []
    res = lt.light_test(fake, evidence_dir=tmp_path, send=True, confirm=lt.CONFIRM_PHRASE,
                        checklist=lt.CHECKLIST_PHRASE, hold_s=90, only="brightfield", sleep=holds.append,
                        on_light=lambda n, h: announced.append((n, h)))
    assert res.outcome == "complete" and holds == [90] and announced == [("brightfield", 90)]
    assert sent(fake) == [(0x45, 0, 0), (0x4A, 0, 0), (0x43, 0, 1), (0x43, 0, 0), (0x45, 0, 0), (0x4A, 0, 0)]
    assert 0x49 not in [w[1][1] for w in fake.writes()]


@pytest.mark.parametrize("only,hold,ok", [("brightfield", 120, True), ("brightfield", 121, False),
                                          ("epi", 10, True), ("epi", 11, False), (None, 11, False),
                                          ("dapi", 3, False)])
def test_hold_limit_depends_on_mode(tmp_path, only, hold, ok):
    start_record(tmp_path)
    fake = FakeBoard()
    res = run(fake, tmp_path, hold_s=hold, only=only)
    assert (res.outcome == "complete") == ok
    if not ok:
        assert fake.io() == []


def test_epi_only_sequence(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard()
    res = run(fake, tmp_path, only="epi")
    assert res.outcome == "complete" and 0x43 not in [w[1][1] for w in fake.writes()]


# ------------------------------------------------------------------ nothing sent
def test_dry_run_default(tmp_path):
    start_record(tmp_path)
    fake = FakeBoard()
    res = lt.light_test(fake, evidence_dir=tmp_path)
    assert res.outcome == "dry_run" and fake.io() == []


@pytest.mark.parametrize("checklist,confirm", [("", lt.CONFIRM_PHRASE), (lt.CHECKLIST_PHRASE, ""),
                                               (lt.CHECKLIST_PHRASE.lower(), lt.CONFIRM_PHRASE)])
def test_phrases_required(tmp_path, checklist, confirm):
    start_record(tmp_path)
    fake = FakeBoard()
    res = lt.light_test(fake, evidence_dir=tmp_path, send=True, confirm=confirm, checklist=checklist)
    assert res.outcome == "refused" and fake.io() == []


@pytest.mark.parametrize("hold", [0, -1, 10.5, 60])
def test_hold_limits(tmp_path, hold):
    start_record(tmp_path)
    fake = FakeBoard()
    assert run(fake, tmp_path, hold_s=hold).outcome == "refused" and fake.io() == []


@pytest.mark.parametrize("kw,why", [({"apps": ()}, "found 0"), ({"boot": (BOOT,)}, "unexpected"),
                                    ({"alt": (APP,)}, "unexpected")])
def test_device_preconditions(tmp_path, kw, why):
    start_record(tmp_path)
    fake = FakeBoard(**kw)
    res = run(fake, tmp_path)
    assert res.outcome == "refused" and why in res.message and fake.io() == []


def test_needs_start_record(tmp_path):
    fake = FakeBoard()
    assert run(fake, tmp_path).outcome == "refused" and fake.io() == []


def test_cli_writes_evidence(tmp_path):
    start_record(tmp_path)
    rc = lt.main(["--send", "--hold", "0.01", "--checklist", lt.CHECKLIST_PHRASE, "--confirm", lt.CONFIRM_PHRASE,
                  "--evidence-dir", str(tmp_path)], backend=FakeBoard())
    assert rc == 0
    rec = json.loads(next(tmp_path.glob(f"{lt.EVIDENCE_PREFIX}-*-complete.json")).read_text("utf-8"))
    assert rec["sent"] and len(rec["steps"]) == 8
