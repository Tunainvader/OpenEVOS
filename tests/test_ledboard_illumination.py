"""Real LED illumination backend (ADR-0048), against a fake LED board only.

Proves: only 0x43/0x49 (values 0/1) and reads 0x45/0x4A/0x47 reach the board; lights start OFF at connect and
go OFF on STOP, disconnect/app exit, link failure and wrong read-back; an unconfirmed OFF says power-cycle;
epi is refused unless the matching cube is in the light path and switches itself off after the hold limit;
a bootloader-only board is quarantined with nothing opened; the app session routes the UI's light commands to
the board through the router.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from openevos.backends.ledboard import illumination as il
from openevos.core.clock import ThreadClock
from openevos.core.models import CommandStatus, Connection, FirmwareMode, Health, SetIllumination
from openevos.instrument.settings import AppSettings
from openevos.maintenance.led_bootloader import DeviceDescription
from openevos.safety.firmware import REVIEWED_APP_WRITES
from tests.test_maintenance_checksum import framed

APP = r"\\?\hid#vid_0dad&pid_0051#fake"
BOOT = r"\\?\hid#vid_0dad&pid_0057#fake"
CUBE = {"RFP": 5, "NONE": 1, "DAPI": 3, "GFP": 4}


class FakeBoard:
    """Minimal model of the LED application: two enables and the cube type in the light path."""

    def __init__(self, cube="GFP", apps=(APP,), boot=(), ignore_on=False, ignore_off=False, dead=False):
        self.bf = self.epi = 0
        self.bf_level = 3  # the board's power-on default (Gate 5, spec Â§7.2a)
        self.epi_level = 1  # power-on default (spec Â§7.2b)
        self.cube = cube
        self.apps, self.boot = list(apps), list(boot)
        self.ignore_on, self.ignore_off, self.dead = ignore_on, ignore_off, dead
        self.sent: list[tuple[int, int, int]] = []
        self.opened = 0
        self._q: list[bytes] = []
        self.lock = threading.Lock()

    def find(self, vid, pid):
        return {0x0051: self.apps, 0x0057: self.boot}.get(pid, [])

    def describe(self, path):
        return DeviceDescription(path, 0x0DAD, 0x0051, 1, 65, 65)

    def _reply(self, req: bytes) -> bytes | None:
        cmd = req[1]
        if self.dead:
            return None
        if cmd == 0x43:
            v = req[3]
            if not ((v and self.ignore_on) or (not v and self.ignore_off)):
                self.bf = v
            return framed(0xC3, bytes([0, self.bf]))
        if cmd == 0x49:
            v = req[2]
            if not ((v and self.ignore_on) or (not v and self.ignore_off)):
                self.epi = v
            return framed(0xC9, bytes([self.epi, 0, 1]))
        if cmd == 0x45:
            return framed(0xC5, bytes([0, self.bf, 0, 0, 0, 0, 0, 2]))
        if cmd == 0x4A:
            return framed(0xCA, bytes([self.epi, 0, self.epi_level, 0, 0x1F, 0xFF, 0xFD]))
        if cmd == 0x4C:
            self.epi_level = req[2]
            return framed(0xCC, bytes([0, self.epi_level]))
        if cmd == 0x47:
            p = bytearray(62)
            p[0] = CUBE[self.cube]
            p[23:27] = bytes([5, 1, 3, 4])
            p[32] = 1
            return framed(0xC7, bytes(p))
        if cmd == 0x5D:
            p = bytearray(62)
            p[33], p[34], p[35] = self.bf_level, 0, 31  # reply bytes 35/36/37
            return framed(0xDD, bytes(p))
        if cmd == 0x5E:
            assert req[2] == 0x04 and [i for i, b in enumerate(req[:64]) if b and i not in (1, 2, 19)] == []
            self.bf_level = req[19]
            return framed(0xDE)
        raise AssertionError(f"command 0x{cmd:02X} must never be sent by the app")

    @contextmanager
    def open(self, path):
        self.opened += 1
        board = self

        class Ch:
            def flush_input(self):
                pass

            def write(self, report, timeout_s):
                with board.lock:
                    board.sent.append((report[1], report[2], report[3]))
                    r = board._reply(bytes(report))
                    if r is not None:
                        board._q.append(r)
                return 65

            def read(self, length, timeout_s):
                with board.lock:
                    if not board._q:
                        raise TimeoutError("no completion within 500 ms")
                    return board._q.pop(0)

        yield Ch()


class Ctx:
    def __init__(self, command_id="c1"):
        self.command_id, self.status, self.message, self.code = command_id, None, "", ""
        self.done = threading.Event()

    def mark_started(self):
        pass

    def report_progress(self, fraction, message=None):
        pass

    def complete(self, result=None, payload=None):
        self.status = "ok"
        self.done.set()

    def fail(self, message, error_code=None):
        self.status, self.message, self.code = "failed", message, error_code
        self.done.set()

    def mark_cancelled(self, result=None):
        self.status = "cancelled"
        self.done.set()


CHANNELS = ("cube1", "cube2", "cube3", "cube4", "trans", "phase")
LABELS = {"cube1": "DAPI", "cube2": "GFP", "cube3": "TxRed", "cube4": "", "trans": "TRANS", "phase": "PHASE"}


class Timers:
    def __init__(self):
        self.started: list[tuple[float, object]] = []

    def __call__(self, delay, fn):
        self.started.append((delay, fn))
        return type("T", (), {"cancel": lambda self: None})()


def make(board, **kw):
    obs = []
    dev = il.RealLedIllumination(board, CHANNELS, LABELS, start_channel="trans", timer_factory=kw.pop("timers", None),
                                 **kw)
    dev.attach(lambda **f: obs.append(f) or True)
    return dev, obs


def run(dev, channel, enabled, pct=50.0, cid="c1"):
    ctx = Ctx(cid)
    dev.start(SetIllumination(device_id=dev.device_id, origin="test", channel=channel, intensity_percent=pct,
                              enabled=enabled, timeout_s=5.0), ctx)
    assert ctx.done.wait(5.0)
    return ctx


def writes(board):
    return [s for s in board.sent if s[0] in (0x43, 0x49)]


# ------------------------------------------------------------------ allowlist
def test_allowlist_matches_registry():
    e = REVIEWED_APP_WRITES["led_application.app_illumination"]
    assert e["commands"] == il.ALLOWED_COMMANDS and e["module"] == "backends/ledboard/illumination.py"
    assert e["vid_pid"] == (0x0DAD, 0x0051)


def test_app_frames_are_identical_to_the_reviewed_tools():
    """The app keeps its own copy of the protocol (ADR-0011); it must build byte-identical frames."""
    from openevos.backends.ledboard import protocol as p
    from openevos.maintenance import led_light_test as lt
    from openevos.maintenance import led_status_read as sr

    for cmd, v in ((0x43, 0), (0x43, 1), (0x49, 0), (0x49, 1)):
        assert p.build_request(cmd, v) == lt.build_request(cmd, v)
    assert p.build_request(0x45) == lt.build_request(0x45)
    assert p.build_request(0x4A) == lt.build_request(0x4A) == sr.REQUESTS[0x4A]
    assert p.build_request(0x47) == sr.REQUESTS[0x47]
    from openevos.maintenance import led_brightness_test as bt

    assert p.build_request(0x5D) == bt.build_request(0x5D)
    for lvl in (0, 1, 10, 20, 31):
        assert p.build_request(0x5E, lvl) == bt.build_request(0x5E, lvl)
        assert p.build_request(0x4C, lvl) == bt.build_request(0x4C, lvl)
    for bad_lvl in (-1, 32, None):
        with pytest.raises(p.ProtocolRefused):
            p.build_request(0x5E, bad_lvl)
    for bad in ((0x41, 1), (0x4C, 32), (0x08, None), (0x52, 0), (0x32, 0), (0x5E, None), (0x43, 2), (0x47, 1),
                (0x5D, 1)):
        with pytest.raises(p.ProtocolRefused):
            p.build_request(*bad)


# ------------------------------------------------------------------ connect
def test_connect_starts_dark_and_reports_application():
    board = FakeBoard()
    dev, obs = make(board)
    dev.connect()
    assert writes(board) == [(0x49, 0, 0), (0x43, 0, 0)]
    last = obs[-1]
    assert last["connection"] is Connection.CONNECTED and last["firmware_mode"] is FirmwareMode.APPLICATION
    assert dev.cube_in_path == "GFP"
    dev.disconnect()


def test_bootloader_only_is_quarantined_and_nothing_opened():
    board = FakeBoard(apps=(), boot=(BOOT,))
    dev, obs = make(board)
    dev.connect()
    assert board.opened == 0 and board.sent == []
    # the app itself still opens nothing on a bootloader board; the launch path starts it (ADR-0055)
    assert obs[-1]["firmware_mode"] is FirmwareMode.BOOTLOADER and "led_autostart" in obs[-1]["reason"]
    dev.disconnect()


# ------------------------------------------------------------------ switching
def test_brightfield_on_then_off():
    board = FakeBoard()
    dev, _ = make(board)
    dev.connect()
    assert run(dev, "trans", True).status == "ok" and board.bf == 1 and dev.emitting
    assert dev.verify_result(SetIllumination(device_id=dev.device_id, origin="test", channel="trans",
                                             intensity_percent=50.0, enabled=True, timeout_s=5.0))[0]
    assert run(dev, "trans", False).status == "ok" and board.bf == 0 and not dev.emitting
    assert {s[0] for s in board.sent} <= il.ALLOWED_COMMANDS
    dev.disconnect()


def test_epi_needs_the_matching_cube_in_the_path():
    board = FakeBoard(cube="RFP")
    dev, _ = make(board)
    dev.connect()
    n = len(writes(board))
    ctx = run(dev, "cube2", True)  # GFP requested, RFP in the path
    assert ctx.status == "failed" and ctx.code == "CUBE_NOT_IN_PATH" and "RFP" in ctx.message
    assert writes(board)[n:] == [(0x49, 0, 0), (0x43, 0, 0)] and board.epi == 0  # refused: left dark, never ON
    board.cube = "GFP"
    assert run(dev, "cube2", True).status == "ok" and board.epi == 1
    dev.disconnect()


def test_switching_channel_turns_the_other_light_off():
    board = FakeBoard(cube="DAPI")
    dev, _ = make(board, timers=Timers())
    dev.connect()
    run(dev, "trans", True)
    run(dev, "cube1", True)  # DAPI
    assert (board.bf, board.epi) == (0, 1)
    run(dev, "phase", True)
    assert (board.bf, board.epi) == (1, 0)
    dev.disconnect()


def test_epi_hold_limit_switches_off():
    board = FakeBoard()
    timers = Timers()
    dev, obs = make(board, timers=timers, epi_max_on_s=42.0)
    dev.connect()
    run(dev, "cube2", True)
    (delay, fn), = timers.started
    assert delay == 42.0 and board.epi == 1
    fn()
    for _ in range(200):
        if board.epi == 0:
            break
        time.sleep(0.005)
    assert board.epi == 0 and any("automatically" in (o.get("reason") or "") for o in obs)
    dev.disconnect()


def test_slider_sets_the_brightfield_level_and_exit_restores_it():
    board = FakeBoard()
    dev, _ = make(board)
    dev.connect()
    assert dev.bf_level_original == 3
    run(dev, "trans", True, pct=50.0)
    assert board.bf == 1 and board.bf_level == il.brightfield_level_for(50.0) == 10
    run(dev, "trans", True, pct=100.0)  # slider moved while on: level only, no extra ON
    assert board.bf_level == il.BF_LEVEL_AT_100 == 20
    assert [s for s in board.sent if s[0] == 0x43].count((0x43, 0, 1)) == 1
    run(dev, "trans", True, pct=0.5)
    assert board.bf_level == 1  # any non-zero % lights at least level 1
    dev.disconnect()
    assert board.bf == 0 and board.bf_level == 3  # off, and the power-on level put back


def test_epi_slider_sets_the_epi_level_and_exit_restores_it():
    board = FakeBoard(cube="GFP")
    dev, _ = make(board, timers=Timers())
    dev.connect()
    assert dev.epi_level_original == 1
    run(dev, "cube2", True, pct=100.0)  # GFP in the path
    assert board.epi == 1 and board.epi_level == il.EPI_LEVEL_AT_100 == 10
    run(dev, "cube2", True, pct=30.0)
    assert board.epi_level == 3 and [s for s in board.sent if s[0] == 0x49].count((0x49, 1, 0)) == 1
    dev.disconnect()
    assert board.epi == 0 and board.epi_level == 1


def test_brightfield_slider_is_calibrated_by_the_measured_curve():
    """Slider % -> the level whose measured output (camera sweep, spec §8.2) is closest to that % of level 20."""
    rel = il.BF_RELATIVE_OUTPUT
    assert rel[0] == 0.0 and rel[il.BF_LEVEL_AT_100] == 1.0 and all(rel[i + 1] > rel[i] for i in range(len(rel) - 1))
    assert il.BF_MEASURED_OUTPUT[20] == pytest.approx(22393.72)  # the measured point, not a model
    for pct in range(1, 101):
        lv = il.brightfield_level_for(pct)
        err = abs(rel[lv] - pct / 100)
        assert all(err <= abs(rel[o] - pct / 100) + 1e-12 for o in range(1, il.BF_LEVEL_AT_100 + 1))
    levels = [il.brightfield_level_for(p) for p in range(101)]
    assert levels[0] == 0 and levels[100] == 20 and levels == sorted(levels) and min(levels[1:]) == 1


def test_level_mapping_is_capped():
    assert [il.epi_level_for(x) for x in (0, 1, 50, 100, 200)] == [0, 1, 5, 10, 10]
    assert [il.brightfield_level_for(x) for x in (0, 1, 25, 50, 100, 150)] == [0, 1, 5, 10, 20, 20]


# ------------------------------------------------------------------ fail safe
def test_stop_and_disconnect_switch_everything_off():
    board = FakeBoard()
    dev, obs = make(board)
    dev.connect()
    run(dev, "trans", True)
    dev.halt()
    assert board.bf == 0 and not dev.enabled and "halted: lights off" in obs[-1]["reason"]
    run(dev, "cube2", True)
    dev.disconnect()
    assert board.epi == 0 and board.bf == 0 and obs[-1]["connection"] is Connection.DISCONNECTED


def test_wrong_readback_fails_safe():
    board = FakeBoard(ignore_on=True)
    dev, obs = make(board)
    dev.connect()
    ctx = run(dev, "trans", True)
    assert ctx.status == "failed" and "read back" in ctx.message and "confirmed off" in ctx.message
    assert obs[-1]["health"] is Health.FAULT
    dev.disconnect()


def test_unconfirmed_off_says_power_cycle():
    board = FakeBoard()
    dev, obs = make(board)
    dev.connect()
    run(dev, "trans", True)
    board.ignore_off = True
    ctx = run(dev, "trans", False)
    assert ctx.status == "failed" and "power-cycle" in ctx.message
    assert obs[-1]["health"] is Health.FAULT and "power-cycle" in obs[-1]["reason"]
    dev.disconnect()


def test_link_failure_fails_safe():
    board = FakeBoard()
    dev, _ = make(board)
    dev.connect()
    board.dead = True
    ctx = run(dev, "trans", True)
    assert ctx.status == "failed" and "power-cycle" in ctx.message
    dev.disconnect()


# ------------------------------------------------------------------ app session
@pytest.fixture
def real_light_session():
    from openevos.acquisition.session import build_sim_session

    board = FakeBoard(cube="GFP")
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), illumination="real",
                          led_hid=board)
    yield s, board
    s.shutdown()


def test_session_routes_light_buttons_to_the_board(real_light_session):
    s, board = real_light_session
    ctl = s.controller
    assert s.profile.illumination_device == il.DEVICE_ID and isinstance(s.rig.light, il.RealLedIllumination)
    ctl.run(ctl.set_light("trans", 40.0, True))
    assert board.bf == 1
    # with the real board the cube buttons are this unit's loadout (ADR-0051): cube1 RFP, cube3 DAPI, cube4 GFP
    ctl.run(ctl.set_light("cube4", 40.0, True))  # GFP is in the path
    assert (board.bf, board.epi) == (0, 1)
    t = ctl.set_light("cube3", 40.0, True)  # DAPI is not in the path (no motor control: F is not moved)
    assert t.wait(5.0) is CommandStatus.FAILED and board.epi == 0
    s.core.router.clear_stop()


def test_session_stop_and_exit_switch_off():
    from openevos.acquisition.session import build_sim_session

    board = FakeBoard()
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), illumination="real",
                          led_hid=board)
    ctl = s.controller
    ctl.run(ctl.set_light("trans", 40.0, True))
    s.core.router.stop_all("test STOP")
    assert board.bf == 0
    s.core.router.clear_stop()
    ctl.run(ctl.set_light("trans", 40.0, True))
    assert board.bf == 1
    s.shutdown()
    assert board.bf == 0 and board.epi == 0


def test_launch_flag_resolution():
    from openevos.ui.app import _resolve_illumination

    assert _resolve_illumination("sim") is None
    assert _resolve_illumination("real") == "real"
    assert _resolve_illumination("auto", probe=lambda: True) == "real"
    assert _resolve_illumination("auto", probe=lambda: False) is None
    assert _resolve_illumination("auto", screenshots=True, probe=lambda: True) is None


def test_auto_probe_counts_interfaces_without_opening():
    from openevos.acquisition.session import real_led_board_present

    assert real_led_board_present(FakeBoard())
    assert not real_led_board_present(FakeBoard(apps=(), boot=(BOOT,)))
    assert not real_led_board_present(FakeBoard(apps=(APP,), boot=(BOOT,)))
    b = FakeBoard()
    real_led_board_present(b)
    assert b.opened == 0
