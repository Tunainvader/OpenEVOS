"""Saving Z-stacks outside the Z-stack page (whose Save and auto-save tests are in test_zstack.py): a single stack
saves as OME-TIFF (Z, Z step, every slice height; the Scan writer uses it), a time-lapse with an output folder
writes every stack as it is taken and keeps only the folder, and a saved folder reads back for the Z-stack page."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.session import build_sim_session
from openevos.acquisition.timelapse import Scene, TimelapseRoutine, TimelapseRunner, populate_wells
from openevos.core.clock import ThreadClock


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    yield s
    s.shutdown()


def test_save_zstack_writes_z_and_every_slice_height(sim, tmp_path):
    import tifffile

    from openevos.imaging.ome import save_zstack

    z0 = sim.controller.focus_z()
    st = sim.capture.zstack("cube1", z0 - 2, z0 + 2, 1)
    p = save_zstack(tmp_path, st)
    assert p.parent == tmp_path and p.name.endswith(".ome.tif") and "cube1" in p.name
    with tifffile.TiffFile(p) as tf:
        shape = dict(zip(tf.series[0].axes, tf.series[0].shape, strict=True))
        assert shape.get("Z") == 5
        meta = tf.ome_metadata
    assert "PhysicalSizeZ" in meta
    desc = json.loads(meta.split("<Description>")[1].split("</Description>")[0].replace("&quot;", '"'))
    assert desc["z_um"] == pytest.approx(list(st.z_um), abs=1e-3)
    assert desc["direction"] == "up" and desc["complete"] is True
    assert np.array_equal(tifffile.imread(p)[0], st.images[0].data)
    assert save_zstack(tmp_path, st) != p  # a second save never overwrites the first


def _timelapse(sim, folder: str, channels=("cube1",)) -> TimelapseRoutine:
    return TimelapseRoutine(name="tz", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=list(channels),
                            auto_fine_focus=False, beacons=populate_wells(sim.vessel)[40:42],
                            scenes=[Scene(duration_s=0.2, interval_s=0.2)], zstack=True, zstack_range_um=4,
                            zstack_step_um=2, output_folder=folder)


def test_timelapse_with_an_output_folder_writes_every_stack_and_reads_back(sim, tmp_path):
    from openevos.imaging.zstack_io import load_zstack_set

    res = TimelapseRunner(ScanRunner(sim.capture, sim.autofocus, sim.vessel), sim.stage_map,
                          sim.core.clock.monotonic).run(_timelapse(sim, str(tmp_path), ("cube1", "cube2")))
    assert res.completed, res.message
    assert res.output_folder == str(tmp_path / "tz")
    assert not res.stacks and len(res.stack_folders) == 4  # 2 time points x 2 positions, only the folders kept
    folders = sorted(res.stack_folders.values())
    assert os.path.basename(folders[0]).startswith("t001_")
    zs = load_zstack_set(folders[0])
    assert zs.complete and zs.channels == ("cube1", "cube2")
    st = zs.stack("cube2")
    assert len(st.images) == 3 and st.planned == 3 and st.images[0].channel == "cube2"
    assert st.images[1].z_um == pytest.approx(st.z_um[1], abs=0.5)


def test_a_saved_stack_reads_back_as_it_was(sim, tmp_path):
    from openevos.acquisition.zstack import ZStackPlan
    from openevos.imaging.zstack_io import load_zstack_set, save_zstack_set

    z0 = sim.controller.focus_z()
    zs = sim.capture.run_zstack(ZStackPlan(channels=("cube1",), role=sim.settings.active_camera, bottom_um=z0 - 2,
                                           top_um=z0 + 2, step_um=2, store=False))
    back = load_zstack_set(save_zstack_set(tmp_path, zs))
    assert back.status == zs.status and back.planned_z_um == pytest.approx(zs.planned_z_um)
    a, b = zs.stack(), back.stack()
    assert all(np.array_equal(x.data, y.data) for x, y in zip(a.images, b.images, strict=True))
    assert [i.metadata() for i in b.images] == [i.metadata() for i in a.images]
    assert back.plan.step_um == zs.plan.step_um and back.started_at == zs.started_at


def test_settings_saved_with_the_old_zstack_keys_still_load(tmp_path):
    from openevos.instrument.settings import AppSettings

    p = tmp_path / "ui_settings.json"
    d = json.loads(AppSettings().model_dump_json())
    d["zstack"] = {"autosave": True, "folder": "D:/stacks"}  # as written by the app on 7deb66e
    p.write_text(json.dumps(d), "utf-8")
    s = AppSettings.load(p)
    assert s.zstack.auto_save and s.zstack.save_folder == "D:/stacks"
