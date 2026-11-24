"""Saving Z-stacks: a manual stack saves as OME-TIFF (Z, Z step, every slice height), automatically after each
acquisition when the operator ticks it (a stopped stack too), and a time-lapse with an output folder writes every
stack to disk as it is taken."""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")

from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.session import build_sim_session
from openevos.acquisition.timelapse import Scene, TimelapseRoutine, TimelapseRunner, populate_wells
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings


def wait_for(app, cond, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    yield s
    s.shutdown()


def test_save_zstack_writes_z_and_every_slice_height(sim, tmp_path):
    import tifffile

    from openevos.imaging.ome import save_zstack

    z0 = sim.controller.focus_z()
    st = sim.capture.zstack("cube1", z0 + 2, z0 - 2, 1)  # downward
    p = save_zstack(tmp_path, st)
    assert p.parent == tmp_path and p.name.endswith(".ome.tif") and "cube1" in p.name
    with tifffile.TiffFile(p) as tf:
        shape = dict(zip(tf.series[0].axes, tf.series[0].shape, strict=True))
        assert shape.get("Z") == 5
        meta = tf.ome_metadata
    assert "PhysicalSizeZ" in meta
    desc = json.loads(meta.split("<Description>")[1].split("</Description>")[0].replace("&quot;", '"'))
    assert desc["z_um"] == pytest.approx(list(st.z_um), abs=1e-3)
    assert desc["direction"] == "down" and desc["complete"] is True
    assert np.array_equal(tifffile.imread(p)[0], st.images[0].data)
    assert save_zstack(tmp_path, st) != p  # a second save never overwrites the first


@pytest.fixture(scope="module")
def win():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.resize(1600, 960)
    w.show()
    wait_for(app, lambda: False, 0.4)
    w.navigate("zstack")
    wait_for(app, lambda: False, 0.2)
    yield app, w, s, w.pages["zstack"]
    w.close()
    s.shutdown()
    w.deleteLater()
    wait_for(app, lambda: False, 0.1)


def _acquire(app, s, zp, start, stop, step):
    zp.start.setValue(start)
    zp.stop.setValue(stop)
    zp.step.setValue(step)
    before = s.capture.buffers.zstack
    assert zp.btn.isEnabled(), zp.btn.toolTip()
    zp.btn.click()
    assert wait_for(app, lambda: s.capture.buffers.zstack is not before and not zp._running and zp.btn.isEnabled())
    return s.capture.buffers.zstack


def test_zstack_page_saves_automatically_when_ticked(win, tmp_path):
    app, _w, s, zp = win
    z = s.controller.focus_z()
    zp.autosave.setChecked(False)
    _acquire(app, s, zp, z - 2, z + 2, 2)
    assert not list(tmp_path.glob("*.ome.tif"))
    zp.save_folder.setText(str(tmp_path))
    zp.autosave.setChecked(True)
    st = _acquire(app, s, zp, z - 2, z + 2, 2)
    assert wait_for(app, lambda: zp.lbl_saved.text().startswith("Saved "), 10), zp.lbl_saved.text()
    assert len(list(tmp_path.glob("*.ome.tif"))) == 1
    assert str(tmp_path) in zp.lbl_saved.text()
    assert s.settings.zstack.autosave and s.settings.zstack.folder == str(tmp_path)  # kept for the next launch
    assert any("saved" in text and str(tmp_path) in text for _t, _lvl, text in zp.model.log)
    assert st.complete


def test_a_stopped_stack_is_saved_too(win, tmp_path):
    app, _w, s, zp = win
    z = s.controller.focus_z()
    zp.save_folder.setText(str(tmp_path))
    zp.autosave.setChecked(True)
    zp.start.setValue(z - 40)
    zp.stop.setValue(z + 40)
    zp.step.setValue(1)
    before = s.capture.buffers.zstack
    zp.btn.click()
    assert wait_for(app, lambda: zp._running and zp.bar.value() >= 3)
    zp.btn_cancel.click()
    assert wait_for(app, lambda: s.capture.buffers.zstack is not before and not zp._running)
    assert wait_for(app, lambda: len(list(tmp_path.glob("*.ome.tif"))) == 1, 5)
    (p,) = tmp_path.glob("*.ome.tif")
    assert "stopped" in p.name


def test_save_button_writes_the_stack_shown(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    app, _w, s, zp = win
    zp.autosave.setChecked(False)
    z = s.controller.focus_z()
    _acquire(app, s, zp, z - 1, z + 1, 1)
    target = tmp_path / "mine.ome.tif"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    zp.btn_ome.click()
    assert target.exists()


def test_timelapse_with_an_output_folder_writes_every_stack(sim, tmp_path):
    import tifffile

    r = TimelapseRoutine(name="tz", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1"],
                         auto_fine_focus=False, beacons=populate_wells(sim.vessel)[40:42],
                         scenes=[Scene(duration_s=0.2, interval_s=0.2)], zstack=True, zstack_range_um=4,
                         zstack_step_um=2, output_folder=str(tmp_path))
    res = TimelapseRunner(ScanRunner(sim.capture, sim.autofocus, sim.vessel), sim.stage_map,
                          sim.core.clock.monotonic).run(r)
    assert res.completed, res.message
    files = sorted((tmp_path / "tz").glob("*.ome.tif"))
    assert len(files) == 4  # 2 time points x 2 beacons
    assert files[0].name.startswith("t001_")
    with tifffile.TiffFile(files[0]) as tf:
        assert dict(zip(tf.series[0].axes, tf.series[0].shape, strict=True)).get("Z") == 3
    assert res.output_folder == str(tmp_path / "tz")
