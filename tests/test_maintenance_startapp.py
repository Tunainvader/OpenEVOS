"""StartApp tool (ADR-0012), against a fake HID backend. No real hardware.

The fake simulates the board: after the one `00 08` write, `0DAD:0057` disappears and
`0DAD:0051` appears after a set number of polls (or never). The tests prove what reaches the
device: exactly one write of exactly `00 08 00…`; no flush, no read; nothing at all in a dry run,
on any failed precondition, or without both exact phrases; never a second write; nothing sent to
the application.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from openevos.maintenance import led_startapp as sa
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.safety.firmware import REVIEWED_MAINTENANCE

BOOT = r"\\?\hid#vid_0dad&pid_0057#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
APP = r"\\?\hid#vid_0dad&pid_0051#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
PHRASES = {"checklist": sa.CHECKLIST_PHRASE, "confirm": sa.CONFIRM_PHRASE}


class FakeBoard:
    def __init__(self, app_after_polls: int | None = 2, boot_paths=(BOOT,), app_paths=(), desc=None,
                 write_error=None, written=65):
        self.boot, self.app = list(boot_paths), list(app_paths)
        self.app_after_polls, self.started = app_after_polls, False
        self.desc = desc or DeviceDescription(BOOT, 0x0DAD, 0x0057, 1, 65, 65)
        self.write_error, self.written = write_error, written
        self.polls_since_start = 0
        self.calls: list[tuple] = []

    def find(self, vid, pid):
        self.calls.append(("find", vid, pid))
        if self.started and pid == 0x0057:
            self.polls_since_start += 1
        if pid == 0x0057:
            return [] if self.started else list(self.boot)
        if pid == 0x0051:
            if self.started and self.app_after_polls is not None and self.polls_since_start > self.app_after_polls:
                return [APP]
            return list(self.app)
        return []

    def describe(self, path):
        self.calls.append(("describe", path))
        return self.desc

    @contextmanager
    def open(self, path):
        self.calls.append(("open", path))
        board = self

        class Ch:
            def flush_input(self):
                board.calls.append(("flush",))

            def write(self, report, timeout_s):
                board.calls.append(("write", bytes(report), timeout_s))
                if board.write_error:
                    raise board.write_error
                board.started = True
                return board.written

            def read(self, length, timeout_s):
                board.calls.append(("read", length, timeout_s))
                raise AssertionError("StartApp must never read")

        try:
            yield Ch()
        finally:
            self.calls.append(("close",))

    def io(self):
        return [c for c in self.calls if c[0] in ("open", "flush", "write", "read")]


def gate1_record(dirpath, *, path=BOOT, age=timedelta(minutes=5), outcome="reply", valid=True, evidenced=True):
    rec = {"outcome": outcome, "started_utc": (NOW - age).isoformat(timespec="milliseconds"),
           "device": {"path": path}, "decoded": {"valid": valid, "application_image_evidenced": evidenced}}
    name = f"led-bootloader-gate1-{(NOW - age).strftime('%Y%m%dT%H%M%S')}-{outcome}.json"
    (dirpath / name).write_text(json.dumps(rec), "utf-8")


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(board, tmp_path, **kw):
    clock = Clock()
    return sa.start_app(board, evidence_dir=tmp_path, monotonic=clock.monotonic, sleep=clock.sleep,
                        now=lambda: NOW, **kw)


# ------------------------------------------------------------------ the report
def test_report_is_exactly_00_08_and_nothing_else_can_be_built():
    assert sa.START_REPORT == bytes([0x00, 0x08]) + bytes(63)
    for cmd in (0x0A, 0x59, 0x51, 0x40, 0x00, 0xFF):
        with pytest.raises(sa.StartRefused):
            sa.build_report(cmd)


def test_tool_matches_the_reviewed_maintenance_registry():
    entry = REVIEWED_MAINTENANCE["led_bootloader.start_app"]
    assert entry["vid_pid"] == (sa.VID, sa.PID_BOOTLOADER)
    assert entry["commands"] == sa.ALLOWED_COMMANDS == frozenset({0x08})
    assert entry["module"] == "maintenance/led_startapp.py"


# ------------------------------------------------------------------ one write, then enumeration only
def test_send_writes_once_never_reads_and_watches_for_the_application(tmp_path):
    gate1_record(tmp_path)
    board = FakeBoard(app_after_polls=2)
    res = run(board, tmp_path, send=True, **PHRASES)
    assert res.outcome == "app_appeared" and res.sent and res.bytes_written == 65
    assert board.io() == [("open", BOOT), ("write", sa.START_REPORT, 0.5)]
    assert res.bootloader_gone_after_s == 0.0 and res.app_appeared_after_s == 2.0
    assert res.after["0DAD:0051"] == [APP] and res.before["0DAD:0057"] == [BOOT]
    assert "Nothing was sent to it" in res.message
    # after the write, only enumeration: no further open/describe at all
    after_write = board.calls[board.calls.index(("write", sa.START_REPORT, 0.5)) + 1:]
    assert {c[0] for c in after_write} <= {"close", "find"}


def test_no_application_within_30s_is_reported_and_never_retried(tmp_path):
    gate1_record(tmp_path)
    board = FakeBoard(app_after_polls=None)
    res = run(board, tmp_path, send=True, **PHRASES)
    assert res.outcome == "no_app" and res.waited_s == 30.0 and "not retried" in res.message
    assert [c[0] for c in board.io()].count("write") == 1


def test_write_failure_is_reported_and_never_retried(tmp_path):
    gate1_record(tmp_path)
    board = FakeBoard(write_error=TimeoutError("stalled"))
    res = run(board, tmp_path, send=True, **PHRASES)
    assert res.outcome == "write_failed" and [c[0] for c in board.io()] == ["open", "write"]


def test_short_write_is_reported(tmp_path):
    gate1_record(tmp_path)
    res = run(FakeBoard(written=3), tmp_path, send=True, **PHRASES)
    assert res.outcome == "write_failed" and "3 of 65" in res.message


# ------------------------------------------------------------------ nothing sent unless every precondition holds
def test_dry_run_is_the_default_and_sends_nothing(tmp_path):
    gate1_record(tmp_path)
    board = FakeBoard()
    res = run(board, tmp_path)
    assert res.outcome == "dry_run" and board.io() == [] and res.gate1_record


@pytest.mark.parametrize("phrases", [
    {}, {"checklist": sa.CHECKLIST_PHRASE}, {"confirm": sa.CONFIRM_PHRASE},
    {"checklist": sa.CHECKLIST_PHRASE.lower(), "confirm": sa.CONFIRM_PHRASE},
    {"checklist": sa.CHECKLIST_PHRASE, "confirm": sa.CONFIRM_PHRASE + "!"},
])
def test_both_exact_phrases_are_required(tmp_path, phrases):
    gate1_record(tmp_path)
    board = FakeBoard()
    res = run(board, tmp_path, send=True, **phrases)
    assert res.outcome == "refused" and board.io() == []


@pytest.mark.parametrize("make_record,why", [
    (lambda d: None, "no Gate 1 reply record"),
    (lambda d: gate1_record(d, age=timedelta(minutes=31)), "older than 30 min"),
    (lambda d: gate1_record(d, path=BOOT + "-other"), "for this device"),
    (lambda d: gate1_record(d, valid=False), "does not show a valid reply"),
    (lambda d: gate1_record(d, evidenced=False), "does not show a valid reply"),
])
def test_a_recent_valid_gate1_record_for_this_device_is_required(tmp_path, make_record, why):
    make_record(tmp_path)
    board = FakeBoard()
    res = run(board, tmp_path, send=True, **PHRASES)
    assert res.outcome == "refused" and why in res.message and board.io() == []


@pytest.mark.parametrize("board,why", [
    (FakeBoard(boot_paths=()), "found 0"),
    (FakeBoard(boot_paths=(BOOT, BOOT + "2")), "found 2"),
    (FakeBoard(app_paths=(APP,)), "already present"),
    (FakeBoard(desc=DeviceDescription(BOOT, 0x0DAD, 0x0058, 1, 65, 65)), "not 0DAD:0057"),
    (FakeBoard(desc=DeviceDescription(BOOT, 0x0DAD, 0x0057, 1, 65, 8)), "report sizes"),
])
def test_device_preconditions_refuse_before_any_io(tmp_path, board, why):
    gate1_record(tmp_path)
    res = run(board, tmp_path, send=True, **PHRASES)
    assert res.outcome == "refused" and why in res.message and board.io() == []


# ------------------------------------------------------------------ CLI + evidence
def test_cli_dry_run_and_send_write_evidence(tmp_path, monkeypatch, capsys):
    gate1_record(tmp_path)
    monkeypatch.setattr(sa, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW),
                                                       "fromisoformat": staticmethod(datetime.fromisoformat)}))
    monkeypatch.setattr(sa, "time", type("T", (), {"monotonic": staticmethod(Clock().monotonic),
                                                   "sleep": staticmethod(lambda s: None)}))
    assert sa.main(["--evidence-dir", str(tmp_path)], backend=FakeBoard()) == 0
    assert "[dry_run]" in capsys.readouterr().out
    assert list(tmp_path.glob("led-bootloader-startapp-*-dry_run.json"))


def test_cli_refusal_writes_no_evidence_and_sends_nothing(tmp_path):
    board = FakeBoard()
    assert sa.main(["--send", "--checklist", sa.CHECKLIST_PHRASE, "--confirm", sa.CONFIRM_PHRASE,
                    "--evidence-dir", str(tmp_path)], backend=board) == 1  # no Gate 1 record
    assert board.io() == [] and not list(tmp_path.glob("led-bootloader-startapp-*"))
