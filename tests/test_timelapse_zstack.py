"""Time-lapse routines with "Z-stack" ticked: every stack is kept (not only the last), the manual Z-stack is left alone,
Stop ends a stack mid-way, OME export carries Z, and each stack opens on the Z-stack page."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.session import build_sim_session
from openevos.acquisition.timelapse import Scene, TimelapseRoutine, TimelapseRunner, populate_wells
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    yield s
    s.shutdown()


def _routine(sim, *, timepoints: int = 2, beacons: int = 2, channels=("cube1",), rng=4.0, step=2.0):
    return TimelapseRoutine(name="tz", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=list(channels),
                            auto_fine_focus=False, beacons=populate_wells(sim.vessel)[40:40 + beacons],
                            scenes=[Scene(duration_s=(timepoints - 1) * 0.2, interval_s=0.2)],
                            zstack=True, zstack_range_um=rng, zstack_step_um=step)


def _runner(sim):
    return TimelapseRunner(ScanRunner(sim.capture, sim.autofocus, sim.vessel), sim.stage_map,
                           sim.core.clock.monotonic)


def test_every_timelapse_stack_is_kept_and_the_manual_stack_is_left_alone(sim):
    z0 = sim.controller.focus_z()
    manual = sim.capture.zstack("cube1", z0 - 2, z0 + 2, 2)
    res = _runner(sim).run(_routine(sim))
    assert res.completed, res.message
    assert len(res.stacks) == 4  # 2 time points x 2 beacons
    for by_slot in res.stacks.values():
        st = by_slot["cube1"]
        assert st.complete and len(st.images) == 3
    assert sim.capture.buffers.zstack is manual  # the routine's stacks do not replace the operator's Z-stack


def test_stop_during_a_timelapse_stack_ends_the_stack_and_the_routine(sim):
    r = _routine(sim, timepoints=3, beacons=1, rng=60, step=1)  # 61 slices
    runner = _runner(sim)
    z_seen: list[float] = []

    def progress(_i, _n, msg):
        if "slice 5/" in msg and not runner.control.stopped:
            z_seen.append(sim.controller.focus_z())
            runner.control.stop()

    res = runner.run(r, progress)
    assert not res.completed and "stopped" in res.message
    assert len(res.stacks) == 1
    st = next(iter(res.stacks.values()))["cube1"]
    assert not st.complete and len(st.images) < 61  # Stop did not wait for the remaining slices
    assert not sim.controller.light.enabled or sim.capture.light_power


def test_timelapse_ome_export_has_z(sim, tmp_path):
    import tifffile

    from openevos.imaging.ome import timelapse_to_ome

    res = _runner(sim).run(_routine(sim, channels=("cube1", "cube2")))
    assert res.completed, res.message
    b = res.routine.beacons[0].label
    p = timelapse_to_ome(tmp_path / "tz.ome.tif", res, b, 0.2)
    with tifffile.TiffFile(p) as tf:
        s = tf.series[0]
        shape = dict(zip(s.axes, s.shape, strict=True))
        assert shape.get("T") == 2 and shape.get("Z") == 3 and shape.get("C") == 2
        assert "PhysicalSizeZ" in tf.ome_metadata
    first = res.stacks[(0, b)]["cube1"].images[0].data
    data = tifffile.imread(p)
    assert np.array_equal(data.reshape(2, 3, 2, *first.shape)[0, 0, 0], first)


def test_timelapse_page_opens_a_stack_on_the_zstack_page():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.show()
    try:
        res = _runner(s).run(_routine(s))
        tl = w.pages["timelapse"]
        tl._done(res)
        assert tl.stack_pick.count() == 4 and tl.btn_open_stack.isEnabled()
        tl.stack_pick.setCurrentIndex(3)
        key, slot = tl.stack_pick.currentData()
        tl.btn_open_stack.click()
        end = time.monotonic() + 2
        while time.monotonic() < end and not w.pages["zstack"].isVisible():
            app.processEvents()
        assert s.capture.buffers.zstack is res.stacks[key][slot]
        assert w.pages["zstack"].isVisible()
        assert w.pages["zstack"].map.taken == 3
    finally:
        w.close()
        s.shutdown()
        w.deleteLater()
        app.processEvents()
