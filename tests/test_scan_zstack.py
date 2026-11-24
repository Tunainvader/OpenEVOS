"""Scan routines with "Z-stack" ticked: one stack per tile and channel, centred on the tile's focus, kept apart from
the operator's Z-stack, written to disk as each finishes, ended by Stop, and opened on the Z-stack page."""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition import scan_runner as sr
from openevos.acquisition.scan_plan import Coverage, ScanRoutine, plan_scan
from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    yield s
    s.shutdown()


def _routine(sim, *, cols=2, channels=("cube1",), rng=4.0, step=2.0, folder="", zstack=True):
    return ScanRoutine(name="sz", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=list(channels),
                       regions=["D7"], coverage=Coverage.GRID, grid_rows=1, grid_cols=cols, auto_fine=False,
                       output_folder=folder, zstack=zstack, zstack_range_um=rng, zstack_step_um=step)


def test_scan_plan_counts_the_slices_and_refuses_too_many(sim):
    flat = plan_scan(_routine(sim, zstack=False), sim.vessel, sim.profile, sim.stage_map)
    stacked = plan_scan(_routine(sim), sim.vessel, sim.profile, sim.stage_map)
    assert stacked.est_seconds > flat.est_seconds and stacked.est_bytes > flat.est_bytes
    with pytest.raises(ValueError, match="200"):
        plan_scan(_routine(sim, rng=400, step=1), sim.vessel, sim.profile, sim.stage_map)


def test_scan_takes_a_stack_at_every_tile_and_leaves_the_manual_stack_alone(sim):
    z0 = sim.controller.focus_z()
    manual = sim.capture.zstack("cube1", z0 - 2, z0 + 2, 2)
    res = ScanRunner(sim.capture, sim.autofocus, sim.vessel).run(
        plan_scan(_routine(sim, channels=("cube1", "cube2")), sim.vessel, sim.profile, sim.stage_map))
    assert res.completed, res.message
    assert sorted(res.stacks) == [t.index for t, _imgs in res.images]
    for by_slot in res.stacks.values():
        assert set(by_slot) == {"cube1", "cube2"}
        for st in by_slot.values():
            assert st.complete and len(st.images) == 3
            assert st.z_um[0] == pytest.approx(z0 - 2, abs=0.2) and st.z_um[-1] == pytest.approx(z0 + 2, abs=0.2)
    assert set(res.mosaics) == {"cube1", "cube2"}
    assert sim.capture.buffers.zstack is manual
    assert sim.controller.focus_z() == pytest.approx(z0, abs=0.2)


def test_scan_writes_each_stack_to_disk(sim, tmp_path):
    import tifffile

    res = ScanRunner(sim.capture, sim.autofocus, sim.vessel).run(
        plan_scan(_routine(sim, folder=str(tmp_path)), sim.vessel, sim.profile, sim.stage_map))
    assert res.completed, res.message
    out = tmp_path / "sz"
    lines = [json.loads(x) for x in (out / "tiles.jsonl").read_text("utf-8").splitlines()]
    assert len(lines) == 2
    for entry in lines:
        name = entry["stacks"]["cube1"]
        with tifffile.TiffFile(out / name) as tf:
            shape = dict(zip(tf.series[0].axes, tf.series[0].shape, strict=True))
            assert shape.get("Z") == 3 and "PhysicalSizeZ" in tf.ome_metadata
    st = next(iter(res.stacks.values()))["cube1"]
    assert dict(st.images[0].extra)["in_memory_copy"].startswith(f"1/{sr.MEMORY_DOWNSAMPLE}")
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert all("stacks" in t for t in manifest["tiles"])


def test_stop_during_a_scan_stack_keeps_and_saves_the_slices_taken(sim, tmp_path):
    runner = ScanRunner(sim.capture, sim.autofocus, sim.vessel)

    def progress(_i, _n, msg):
        if "slice 4/" in msg and not runner.control.stopped:
            runner.control.stop()

    res = runner.run(plan_scan(_routine(sim, cols=3, rng=60, step=1, folder=str(tmp_path)), sim.vessel,
                               sim.profile, sim.stage_map), progress)
    assert not res.completed and "stopped" in res.message
    assert len(res.images) == 1 and len(res.stacks) == 1
    st = res.stacks[res.images[0][0].index]["cube1"]
    assert not st.complete and len(st.images) < 61
    entry = json.loads((tmp_path / "sz" / "tiles.jsonl").read_text("utf-8").splitlines()[0])
    assert (tmp_path / "sz" / entry["stacks"]["cube1"]).exists()


def test_scan_page_runs_stacks_and_opens_one_on_the_zstack_page():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.show()
    try:
        page = w.pages["scan"]
        page.zstack.setChecked(True)
        page.z_range.setValue(4)
        page.z_step.setValue(2)
        assert page.routine().zstack and page.routine().zstack_step_um == 2
        res = ScanRunner(s.capture, s.autofocus, s.vessel).run(
            plan_scan(_routine(s), s.vessel, s.profile, s.stage_map))
        page._done(res)
        assert page.stack_pick.count() == 2 and page.btn_open_stack.isEnabled()
        page.stack_pick.setCurrentIndex(1)
        idx, slot = page.stack_pick.currentData()
        page.btn_open_stack.click()
        end = time.monotonic() + 2
        while time.monotonic() < end and not w.pages["zstack"].isVisible():
            app.processEvents()
        assert s.capture.buffers.zstack is res.stacks[idx][slot]
        assert w.pages["zstack"].isVisible()
    finally:
        w.close()
        s.shutdown()
        w.deleteLater()
        app.processEvents()
