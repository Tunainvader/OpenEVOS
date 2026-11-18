"""LED board auto-start at launch (ADR-0055), against a fake HID board. No real hardware.

Proves: the board in its bootloader gets exactly the reviewed sequence (one 0x0A version query with one read, then
one 0x08 StartApp write, then enumeration only) and both evidence records; nothing at all is sent when the application
is already running, when no board is present, or when the version reply does not evidence an application image; the
app's launch path runs it before the illumination is resolved and can be switched off.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager

from openevos.maintenance import led_autostart as la
from openevos.maintenance.led_bootloader import DeviceDescription

BOOT = r"\\?\hid#vid_0dad&pid_0057#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
APP = r"\\?\hid#vid_0dad&pid_0051#fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"


def reply(code=0x8A, a=2, b=1, ver=(3, 0, 25949)):
    r = bytearray(65)
    r[1] = code
    r[2:10] = struct.pack(">II", a, b)
    r[22:34] = struct.pack(">III", *ver)
    return bytes(r)


class Board:
    def __init__(self, *, state="boot", answer=None):
        self.state = state  # "boot" | "app" | "absent"
        self.answer = answer if answer is not None else reply()
        self.io: list[tuple] = []

    def find(self, vid, pid):
        if pid == 0x0057:
            return [BOOT] if self.state == "boot" else []
        if pid == 0x0051:
            return [APP] if self.state == "app" else []
        return []

    def describe(self, path):
        return DeviceDescription(BOOT, 0x0DAD, 0x0057, 1, 65, 65)

    @contextmanager
    def open(self, path):
        board = self

        class Ch:
            def flush_input(self):
                board.io.append(("flush",))

            def write(self, report, timeout_s):
                board.io.append(("write", bytes(report[:2])))
                if report[1] == 0x08:
                    board.state = "app"  # the application enumerates right away
                return 65

            def read(self, length, timeout_s):
                board.io.append(("read",))
                return board.answer

        yield Ch()


def test_bootloader_gets_the_reviewed_sequence_and_evidence(tmp_path):
    b = Board()
    r = la.autostart(b, evidence_dir=tmp_path)
    assert r.outcome == "started" and r.light_available
    assert b.io == [("flush",), ("write", b"\x00\x0a"), ("read",), ("write", b"\x00\x08")]
    names = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.startswith("led-bootloader-gate1-") and n.endswith("-reply.json") for n in names)
    assert any(n.startswith("led-bootloader-startapp-") and n.endswith("-app_appeared.json") for n in names)
    assert set(r.evidence) == set(names)


def test_nothing_sent_when_running_absent_or_no_application_image(tmp_path):
    for state in ("app", "absent"):
        b = Board(state=state)
        r = la.autostart(b, evidence_dir=tmp_path)
        assert b.io == [] and r.outcome == ("already_running" if state == "app" else "absent")
    assert r.light_available is False
    b = Board(answer=reply(ver=(0, 0, 0)))  # blank application version: StartApp must not be considered
    r = la.autostart(b, evidence_dir=tmp_path)
    assert r.outcome == "failed" and ("write", b"\x00\x08") not in b.io
    assert b.state == "boot"


def test_launch_path_runs_it_unless_switched_off(monkeypatch, tmp_path):
    import openevos.maintenance.led_autostart as mod
    from openevos.ui import app as appmod

    calls = []
    monkeypatch.setattr(mod, "autostart", lambda **kw: calls.append(kw) or la.AutostartResult("absent", "x"))
    monkeypatch.setattr(appmod.sys, "platform", "win32")

    class Stop(Exception):
        pass

    def stop(*_a, **_k):
        raise Stop

    monkeypatch.setattr(appmod, "_resolve_illumination", stop)
    for extra, want in (([], 1), (["--no-led-autostart"], 0), (["--illumination", "sim"], 0)):
        calls.clear()
        try:
            appmod.main(["--scans-dir", str(tmp_path), *extra])
        except Stop:
            pass
        assert len(calls) == want, extra
