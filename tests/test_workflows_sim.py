"""Workflows against the optics simulator on a real ThreadClock (fast latencies)."""

from __future__ import annotations

import time

import pytest

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.controller import AcquisitionError, CommandError
from openevos.acquisition.scan_plan import Coverage, ScanRoutine, plan_scan
from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.session import build_sim_session
from openevos.acquisition.timelapse import Scene, TimelapseRoutine, TimelapseRunner, populate_wells
from openevos.backends.rig.rig import PARFOCAL_UM
from openevos.core.clock import ThreadClock
from openevos.core.models import (
    CommandOrigin,
    CommandStatus,
    SetIllumination,
    SetTurretPosition,
    parse_command,
)
from openevos.instrument.profile import CameraRole


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    yield s
    s.shutdown()


def _goto_well(s, well="D7"):
    r = s.vessel.region(well)
    s.controller.run(s.controller.move_stage(*s.stage_map.to_stage(r.cx, r.cy)))


def test_gain_command_parses_and_is_verified(sim):
    # merged contract: the core carries gain in dB; the UI/controller speak a linear factor (2.5x = 7.9588 dB)
    import math

    c = parse_command({"kind": "camera.set_gain", "device_id": "sim-camera", "origin": "test",
                       "gain_db": 20 * math.log10(2.5)})
    t = sim.core.router.submit(c)
    assert t.wait(5) is CommandStatus.SUCCEEDED
    assert sim.rig.camera(CameraRole.MONO).gain == pytest.approx(2.5)
    t2 = sim.controller.set_gain(CameraRole.MONO, 4.0)  # the controller converts x -> dB
    assert t2.wait(5) is CommandStatus.SUCCEEDED
    assert sim.rig.camera(CameraRole.MONO).gain == pytest.approx(4.0)


def test_non_motion_readback_verification_fails_closed(sim):
    sim.rig.turret.verify_result = lambda cmd: (False, "index sensor disagrees")
    sim.controller.run(sim.controller.move_focus(1000.0))  # below the turret-safe height (APP-01 interlock)
    t = sim.core.router.submit(SetTurretPosition(device_id="sim-turret", origin=CommandOrigin.TEST, position=3))
    assert t.wait(5) is not CommandStatus.SUCCEEDED
    assert "index sensor" in (t.record.message or "")


def test_carriage_moves_and_is_interlocked_against_exposure(sim):
    ctl = sim.controller
    t = ctl.set_light("cube2", 50, True)
    time.sleep(0.01)
    assert sim.core.store.get("sim-light").activity.value == "moving"  # F carriage travelling
    ctl.run(t)
    assert sim.rig.light.channel == "cube2" and ctl.light.channel == "cube2"
    # hold an acquisition claim: the light cannot change mid-exposure
    from openevos.safety.interlocks import claim_for_acquisition

    assert sim.core.router.reserve("acq-x", claim_for_acquisition("sim-camera", ["sim-light"])) == []
    t2 = sim.core.router.submit(SetIllumination(device_id="sim-light", origin=CommandOrigin.TEST, channel="cube1",
                                                intensity_percent=50, enabled=True))
    assert t2.wait(5) is CommandStatus.REJECTED and "resource conflict" in t2.record.message
    sim.core.router.release("acq-x")


def test_capture_metadata_and_buffers(sim):
    _goto_well(sim)
    img = sim.capture.capture("cube2")
    assert img.synthetic and img.channel == "cube2" and img.data.dtype.name == "uint16"
    assert img.magnification == 4 and img.pixel_size_um == pytest.approx(3.75 / 4)
    assert img.z_um == pytest.approx(sim.controller.focus_z())
    assert sim.capture.buffers.get("cube2") is img
    assert sim.capture.last_capture_z == img.z_um
    assert img.metadata()["provenance"].startswith("SIMULATED")
    imgs = sim.capture.capture_all()
    assert [i.channel for i in imgs] == ["cube1", "cube2", "cube3"]
    sim.capture.buffers.clear()
    assert sim.capture.buffers.all() == {}


def test_color_camera_refuses_fluorescence(sim):
    with pytest.raises(CaptureRejected, match="brightfield/phase"):
        sim.capture.capture("cube2", CameraRole.COLOR)
    img = sim.capture.capture("trans", CameraRole.COLOR)
    assert img.is_color and img.bit_depth == 8
    with pytest.raises(CaptureRejected):
        sim.capture.capture_all(CameraRole.COLOR)


def test_autofocus_finds_the_simulated_focal_plane(sim):
    _goto_well(sim)
    ctl = sim.controller
    ctl.run(ctl.set_objective(2))
    x, y = ctl.stage_xy()
    truth = sim.rig.world.focal_plane_um(x, y) + PARFOCAL_UM[2]
    ctl.run(ctl.move_focus(truth + 40))  # well out of focus
    dark = sim.autofocus.find_sample(CameraRole.MONO, sim.vessel.focus_nominal_um, sim.vessel.focus_range_um)
    assert not dark.ok and "light" in dark.message  # never "focuses" on a dark field
    sim.capture.prepare_channel(CameraRole.MONO, "cube1")
    r = sim.autofocus.find_sample(CameraRole.MONO, sim.vessel.focus_nominal_um, sim.vessel.focus_range_um)
    assert r.ok, r.message
    fine = sim.autofocus.auto_fine(CameraRole.MONO)
    assert fine.ok, fine.message
    assert abs(ctl.focus_z() - truth) < 4.0  # within ~half a DOF-scale step at 10x


def test_autofocus_reports_no_sample_between_wells(sim):
    ctl = sim.controller
    ctl.run(ctl.move_stage(*sim.stage_map.to_stage(sim.vessel.region("D7").cx + 4.5, sim.vessel.region("D7").cy)))
    sim.capture.prepare_channel(CameraRole.MONO, "cube1")
    r = sim.autofocus.auto_fine(CameraRole.MONO)
    assert not r.ok and "no sample" in r.message


def test_zstack_returns_focus(sim):
    _goto_well(sim)
    z0 = sim.controller.focus_z()
    st = sim.capture.zstack("cube1", z0 - 4, z0 + 4, 2)
    assert len(st.images) == 5 and st.z_um[0] == pytest.approx(z0 - 4)
    assert [round(i.z_um, 3) for i in st.images] == [round(z, 3) for z in st.z_um]
    assert sim.controller.focus_z() == pytest.approx(z0)
    with pytest.raises(CaptureRejected):
        sim.capture.zstack("cube1", -100, 10, 1)


def test_command_errors_surface(sim):
    with pytest.raises(CommandError, match="outside"):
        sim.controller.run(sim.controller.move_focus(99_999))


def test_scan_runner_small_grid(sim, tmp_path):
    r = ScanRoutine(name="unit", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1", "cube2"],
                    regions=["D7"], coverage=Coverage.GRID, grid_rows=1, grid_cols=3, auto_fine=False,
                    output_folder=str(tmp_path))
    plan = plan_scan(r, sim.vessel, sim.profile, sim.stage_map)
    res = ScanRunner(sim.capture, sim.autofocus, sim.vessel).run(plan)
    assert res.completed, res.message
    assert len(res.images) == 3 and set(res.mosaics) == {"cube1", "cube2"}
    assert (tmp_path / "unit" / "manifest.json").exists()
    assert len(list((tmp_path / "unit").glob("tile*.tif"))) == 6


def test_scan_streams_tiles_to_disk_and_survives_a_crash(sim, tmp_path, monkeypatch):
    """Audit APP-05: each tile is on disk as soon as it is captured; memory keeps a 1/4 copy; a failure mid-scan
    leaves every finished tile and the append-only index."""
    import json

    from openevos.acquisition import scan_runner as sr

    r = ScanRoutine(name="crash", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1"],
                    regions=["D7"], coverage=Coverage.GRID, grid_rows=1, grid_cols=3, auto_fine=False,
                    output_folder=str(tmp_path))
    runner = ScanRunner(sim.capture, sim.autofocus, sim.vessel)
    real, calls = runner.visit, {"n": 0}

    def visit(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("power cut")
        return real(*a, **k)

    monkeypatch.setattr(runner, "visit", visit)
    res = runner.run(plan_scan(r, sim.vessel, sim.profile, sim.stage_map))
    out = tmp_path / "crash"
    assert not res.completed and "power cut" in res.message
    lines = (out / "tiles.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2 and all((out / json.loads(x)["files"]["cube1"]).exists() for x in lines)
    img = res.images[0][1]["cube1"]
    assert dict(img.extra)["in_memory_copy"].startswith(f"1/{sr.MEMORY_DOWNSAMPLE}")
    import tifffile

    on_disk = tifffile.imread(out / json.loads(lines[0])["files"]["cube1"])
    assert on_disk.shape[0] == img.data.shape[0] * sr.MEMORY_DOWNSAMPLE or \
        abs(on_disk.shape[0] / img.data.shape[0] - sr.MEMORY_DOWNSAMPLE) < 1
    assert json.loads((out / "manifest.json").read_text("utf-8"))["completed"] is False


def test_scan_runner_stop(sim):
    r = ScanRoutine(name="s", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1"], regions=["D7"],
                    coverage=Coverage.GRID, grid_rows=3, grid_cols=3, auto_fine=False)
    runner = ScanRunner(sim.capture, sim.autofocus, sim.vessel)
    runner.control.stop()
    res = runner.run(plan_scan(r, sim.vessel, sim.profile, sim.stage_map))
    assert not res.completed and "stopped" in res.message


def test_timelapse_one_frame(sim):
    beacons = populate_wells(sim.vessel)[40:42]
    r = TimelapseRoutine(name="tl", vessel_id=sim.vessel.vessel_id, objective_slot=1, channels=["trans"],
                         auto_fine_focus=False, beacons=beacons, scenes=[Scene(duration_s=10, interval_s=5)],
                         capture_one_frame_only=True)
    sr = ScanRunner(sim.capture, sim.autofocus, sim.vessel)
    res = TimelapseRunner(sr, sim.stage_map, sim.core.clock.monotonic).run(r)
    assert res.completed and len(res.frames) == 2


def test_live_view_pauses_for_camera_settings(sim):
    ctl = sim.controller
    ctl.start_live(CameraRole.MONO)
    cam = sim.rig.camera(CameraRole.MONO)
    assert cam.streaming
    ctl.run(ctl.set_exposure(CameraRole.MONO, 33.0))  # would be rejected (camera acquiring) without the pause
    deadline = time.monotonic() + 2
    while not cam.streaming and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cam.exposure_ms == 33.0 and cam.streaming
    img = sim.capture.capture("trans")  # acquisition pauses and restores live
    assert img.exposure_ms > 0 and cam.streaming
    ctl.stop_live(CameraRole.MONO)
    assert not cam.streaming


def test_acquire_refused_while_stage_moves(sim):
    ctl = sim.controller
    t = ctl.move_stage(40_000, 30_000)
    time.sleep(0.01)
    with pytest.raises(AcquisitionError, match="busy"):
        ctl.acquire(CameraRole.MONO)
    ctl.run(t)


def test_zstack_plan_runs_start_to_stop_and_never_overshoots():
    from openevos.acquisition.capture import plan_zstack

    assert plan_zstack(100, 110, 2) == pytest.approx([100, 102, 104, 106, 108, 110])
    assert plan_zstack(110, 100, 2) == pytest.approx([110, 108, 106, 104, 102, 100])  # downward: start above stop
    assert plan_zstack(0, 11, 4) == pytest.approx([0, 4, 8])  # the last slice stays inside start..stop
    assert plan_zstack(5, 5, 1) == pytest.approx([5])
    with pytest.raises(CaptureRejected):
        plan_zstack(0, 10, 0)
    with pytest.raises(CaptureRejected, match="200"):
        plan_zstack(0, 1000, 1)


def test_zstack_downward_goes_from_start_to_stop(sim):
    _goto_well(sim)
    z0 = sim.controller.focus_z()
    st = sim.capture.zstack("cube1", z0 + 4, z0 - 4, 2)
    assert st.z_um == pytest.approx((z0 + 4, z0 + 2, z0, z0 - 2, z0 - 4))
    assert st.complete and st.direction == -1
    assert sim.controller.focus_z() == pytest.approx(z0)


def test_zstack_stop_keeps_the_slices_taken_and_returns_focus(sim):
    _goto_well(sim)
    z0 = sim.controller.focus_z()

    def progress(i, _n, _msg):
        if i == 3:
            sim.capture.cancel_zstack()  # Stop pressed while slice 4 is about to start

    st = sim.capture.zstack("cube1", z0 - 10, z0 + 10, 2, progress=progress)
    assert not st.complete and len(st.images) == 3 and st.planned == 11
    assert sim.capture.buffers.zstack is st
    assert sim.controller.focus_z() == pytest.approx(z0)
    st2 = sim.capture.zstack("cube1", z0 - 2, z0 + 2, 2)  # a stop does not carry over into the next stack
    assert st2.complete and len(st2.images) == 3


def test_zstack_leaves_a_dark_live_view_dark(sim):
    _goto_well(sim)
    z0 = sim.controller.focus_z()
    sim.capture.light_power = False
    sim.controller.run(sim.controller.set_light("cube1", 20.0, False))
    sim.capture.zstack("cube1", z0 - 2, z0 + 2, 2)
    assert not sim.controller.light.enabled  # the LED is not left on, bleaching the sample, after the stack
