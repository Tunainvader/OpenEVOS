"""LED camera sweep (ADR-0050), fake LED board + fake camera only.

Proves: dark frame first with the light off; levels stepped in order with read-back; stops at saturation without
sending higher levels; light off, LED level restored and camera restored (verified) on success and on failure;
epi refused for DAPI / empty; argument limits; dry run / phrases / preconditions open nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pytest

from openevos.maintenance import led_camera_sweep as sw
from openevos.safety.firmware import REVIEWED_MAINTENANCE
from tests.test_maintenance_checksum import start_record
from tests.test_maintenance_led_brightness_test import Board

SERIAL = 42
POWER_UP_SNAPSHOT = {"video_mode": 17, "frame_rate_enum": 3, "properties": {
    "shutter": {"present": True, "on": True, "auto": True, "value": 256, "abs_value": 12.0},
    "gain": {"present": True, "on": True, "auto": True, "value": 160, "abs_value": 0.0}}}


@dataclass
class Dev:
    problem_code: int = 0


class Pnp:
    def __init__(self, n=1, code=0):
        self.n, self.code = n, code

    def present(self, vid, pid):
        return [Dev(self.code) for _ in range(self.n)]


class FakeCam:
    """Duck-typed FlyCapture2SettingsBackend: frames brighten with the fake LED board's level while it is on."""

    def __init__(self, board: Board, light="brightfield", per_level=1000.0, restore_breaks=False, fail_grab_at=None):
        self.board, self.light, self.per_level = board, light, per_level
        self.restore_breaks, self.fail_grab_at = restore_breaks, fail_grab_at
        self.state = json.loads(json.dumps(POWER_UP_SNAPSHOT))
        self.props = {"auto_exposure": {"on": True, "auto": True, "abs_value": 0.0},
                      "gain": {"on": True, "auto": True, "abs_value": 0.0},
                      "brightness": {"on": True, "auto": False, "abs_value": 0.0},
                      "shutter": {"on": True, "auto": True, "abs_value": 12.0}}
        self.grabs, self.closed, self.connected = 0, False, False
        self.skipped = 0

    def sdk_available(self):
        return True

    def count_cameras(self):
        return 1

    def connect(self):
        self.connected = True

    def identity(self):
        return {"serial": SERIAL}

    def snapshot(self):
        return json.loads(json.dumps(self.state))

    def set_format7_mono16_full(self):
        self.state["video_mode"] = 21
        return {"packet_used": 3904}

    def set_prop(self, name, *, on, auto, abs_value=None, raw_value=None):
        self.props.setdefault(name, {}).update(on=on, auto=auto)
        if abs_value is not None:
            self.props[name]["abs_value"] = abs_value
        if name in self.state["properties"]:
            self.state["properties"][name].update(on=on, auto=auto)
            if raw_value is not None:
                self.state["properties"][name]["value"] = raw_value

    def get_prop(self, name):
        return dict(self.props[name])

    def set_video_mode(self, vm, fr):
        self.state["video_mode"], self.state["frame_rate_enum"] = (vm + 1 if self.restore_breaks else vm), fr

    def grab_series(self, n_discard, n_keep, timeout_s):
        self.grabs += 1
        if self.fail_grab_at == self.grabs:
            raise RuntimeError("no frame within 5 s")
        b = self.board
        on = b.bf if self.light == "brightfield" else b.epi
        level = b.bf_level if self.light == "brightfield" else b.epi_level
        mean = 200.0 + (self.per_level * level if on else 0.0)
        return [np.full((sw.F7_HEIGHT, sw.F7_WIDTH), min(mean, 65520), np.uint16) for _ in range(n_keep)]

    def close(self):
        self.closed = True


def gate1_record(d):
    rec = {"identity": {"serial": SERIAL}, "steps_done": ["modes"], "modes": POWER_UP_SNAPSHOT}
    (d / "camera-gate1-mono-20261113T000000-complete.json").write_text(json.dumps(rec), "utf-8")


def run(tmp_path, board=None, cam=None, **kw):
    start_record(tmp_path)
    gate1_record(tmp_path)
    board = board or Board()
    cam = cam or FakeCam(board, kw.get("light", "brightfield"))
    kw.setdefault("light", "brightfield")
    res = sw.led_camera_sweep(board, Pnp(), cam, evidence_dir=tmp_path, send=True, confirm=sw.CONFIRM_PHRASE,
                              checklist=sw.CHECKLIST_PHRASE, sleep=lambda s: None, **kw)
    return res, board, cam


def test_registered():
    e = REVIEWED_MAINTENANCE["led_application.camera_sweep"]
    assert e["module"] == "maintenance/led_camera_sweep.py" and e["adr"] == "ADR-0050"


def test_brightfield_sweep_measures_and_restores_everything(tmp_path):
    res, board, cam = run(tmp_path, levels=(1, 2, 5, 10))
    assert res.outcome == "complete", res.message
    assert res.dark["mean"] == pytest.approx(200.0)
    assert res.analysis["net_by_level"] == {1: 1000.0, 2: 2000.0, 5: 5000.0, 10: 10000.0}
    assert res.analysis["monotonic"] and res.analysis["fit_r2"] == pytest.approx(1.0)
    assert board.bf == 0 and board.bf_level == 7  # off, original level back
    assert res.camera_restore_verified and cam.closed
    sent = [c for c in board.cmds() if c[0] in (0x43, 0x5E)]
    assert sent == [(0x5E, 4, 1), (0x43, 0, 1), (0x5E, 4, 2), (0x5E, 4, 5), (0x5E, 4, 10), (0x43, 0, 0), (0x5E, 4, 7)]
    assert len(res.frame_files) == 5  # dark + 4 levels


def test_stops_at_saturation_without_higher_levels(tmp_path):
    board = Board()
    cam = FakeCam(board, per_level=30000.0)  # level 3 -> 90200, clipped at the camera's 65520: saturated
    res, board, _ = run(tmp_path, board=board, cam=cam, levels=(1, 2, 3, 5))
    assert res.outcome == "saturated" and [r["level"] for r in res.rows] == [1, 2, 3]
    assert (0x5E, 4, 5) not in board.cmds() and board.bf == 0 and board.bf_level == 7


def test_camera_full_scale_65520_counts_as_saturated():
    """The live Chameleon clips at 4095 << 4 = 65520, never 65535 (first sweep, 2026-11-16)."""
    clipped = [np.full((4, 4), 65520, np.uint16)] * 3
    assert sw.frame_stats(clipped)["saturated_fraction"] == 1.0


def test_epi_sweep_checks_cube(tmp_path):
    board = Board(cube="GFP")
    res, board, _ = run(tmp_path, board=board, light="epi", levels=(1, 2, 3))
    assert res.outcome == "complete" and res.cube_in_path == "GFP" and board.epi == 0 and board.epi_level == 1


@pytest.mark.parametrize("cube", ["DAPI", "NONE"])
def test_epi_refused_for_dapi_or_empty(tmp_path, cube):
    board = Board(cube=cube)
    res, board, cam = run(tmp_path, board=board, light="epi", levels=(1, 2))
    assert res.outcome == "stopped" and cube in res.message
    assert not cam.connected and [c[0] for c in board.cmds()] == [0x4A, 0x47]


def test_camera_failure_mid_sweep_switches_off_and_restores(tmp_path):
    board = Board()
    cam = FakeCam(board, fail_grab_at=3)  # dark, level 1, then the level-2 grab fails
    res, board, cam = run(tmp_path, board=board, cam=cam, levels=(1, 2, 3))
    assert res.outcome == "stopped" and "no frame" in res.message
    assert board.bf == 0 and board.bf_level == 7 and res.camera_restore_verified and cam.closed


def test_unverified_camera_restore_is_reported(tmp_path):
    board = Board()
    cam = FakeCam(board, restore_breaks=True)
    res, *_ = run(tmp_path, board=board, cam=cam, levels=(1, 2, 3))
    assert res.outcome == "restore_failed" and "re-run Camera Gate 1" in res.message


@pytest.mark.parametrize("kw", [{"levels": (0, 1)}, {"levels": (2, 1)}, {"levels": (1, 21)},
                                {"light": "epi", "levels": (1, 11)}, {"shutter_ms": 5.0}, {"light": "dapi"}])
def test_argument_limits(tmp_path, kw):
    res, board, cam = run(tmp_path, **kw)
    assert res.outcome == "refused" and board.sent == [] and not cam.connected


def test_dry_run_and_phrases(tmp_path):
    start_record(tmp_path)
    gate1_record(tmp_path)
    board = Board()
    cam = FakeCam(board)
    res = sw.led_camera_sweep(board, Pnp(), cam, evidence_dir=tmp_path, light="brightfield")
    assert res.outcome == "dry_run" and board.sent == [] and not cam.connected
    res = sw.led_camera_sweep(board, Pnp(), cam, evidence_dir=tmp_path, light="brightfield", send=True,
                              confirm=sw.CONFIRM_PHRASE)
    assert res.outcome == "refused" and board.sent == [] and not cam.connected


def test_camera_preconditions(tmp_path):
    start_record(tmp_path)
    board = Board()
    cam = FakeCam(board)
    res = sw.led_camera_sweep(board, Pnp(), cam, evidence_dir=tmp_path, light="brightfield")
    assert res.outcome == "refused" and "Gate 1" in res.message  # no camera Gate 1 record
    gate1_record(tmp_path)
    res = sw.led_camera_sweep(board, Pnp(n=0), cam, evidence_dir=tmp_path, light="brightfield")
    assert res.outcome == "refused" and not cam.connected
