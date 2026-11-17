"""Annotations, OME-TIFF, calibration procedures against the simulator's known truths, router stop."""

from __future__ import annotations

import time

import numpy as np
import pytest

from openevos.acquisition import calibration as C
from openevos.acquisition.session import build_sim_session
from openevos.acquisition.timelapse import Scene, TimelapseRoutine, TimelapseRunner, populate_wells
from openevos.analysis.annotations import (
    MAX_ROIS,
    AnnKind,
    Annotation,
    AnnotationSet,
    burn_in,
    roi_stats,
    to_csv,
)
from openevos.backends.rig.rig import PARCENTRIC_UM, PARFOCAL_UM, TRUE_VESSEL_OFFSET_UM
from openevos.core.clock import ThreadClock
from openevos.imaging.ome import write_ome_tiff
from openevos.instrument.profile import CameraRole

M = CameraRole.MONO


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    r = s.vessel.region("D7")
    s.controller.run(s.controller.move_stage(*s.stage_map.to_stage(r.cx, r.cy)))
    yield s
    s.shutdown()


# ------------------------------------------------------------------ annotations (manual p.12-13)
def test_annotation_rois_stats_csv_and_burn_in():
    a = AnnotationSet()
    for i in range(MAX_ROIS):
        a.add(Annotation(AnnKind.RECT, (i, i), (i + 10, i + 10)))
    with pytest.raises(ValueError, match="12"):
        a.add(Annotation(AnnKind.ELLIPSE, (0, 0), (5, 5)))
    a.add(Annotation(AnnKind.LINE, (0, 0), (30, 40)))  # lines are not ROIs
    img = np.zeros((60, 60), np.uint16)
    img[10:21, 10:21] = 100
    rect = Annotation(AnnKind.RECT, (10, 10), (20, 20), name="cell")
    st = roi_stats(rect, {"GFP": img, "DAPI": img * 2}, pixel_um=0.5)
    assert [s["channel"] for s in st] == ["GFP", "DAPI"]
    assert st[0]["area_px"] == 121 and st[0]["mean"] == 100 and st[0]["area_um2"] == pytest.approx(30.25)
    line = Annotation(AnnKind.LINE, (0, 0), (30, 40))
    assert roi_stats(line, {"GFP": img}, 0.5)[0]["length_um"] == pytest.approx(25.0)
    ell = Annotation(AnnKind.ELLIPSE, (10, 10), (30, 20))
    assert 140 < int(ell.mask((60, 60)).sum()) < 180  # ≈ π·10·5
    csv = to_csv(st)
    assert csv.splitlines()[0].startswith("annotation,type,channel")
    out = burn_in(np.zeros((60, 60, 3), np.uint8), AnnotationSet([rect]), pixel_um=0.5,
                  count_markers=[(5, 5, "#FF0000", "Live")], count_summary=["Live: 1 (100.0%)"], grid=3)
    assert out.shape == (60, 60, 3) and out.any()


def test_ome_tiff_roundtrip(tmp_path):
    import tifffile

    data = np.arange(2 * 3 * 1 * 8 * 8, dtype=np.uint16).reshape(2, 3, 1, 8, 8)
    p = write_ome_tiff(tmp_path / "x.ome.tif", data, pixel_um=0.375, z_step_um=2.0, channel_names=["GFP"])
    with tifffile.TiffFile(p) as tf:
        assert tf.is_ome
        assert tf.series[0].axes.upper().replace("Q", "") in ("TZCYX", "TZYX")
        assert np.array_equal(tf.asarray().reshape(data.shape), data)
        assert 'PhysicalSizeX="0.375"' in tf.ome_metadata and 'Name="GFP"' in tf.ome_metadata


# ------------------------------------------------------------------ calibration (manual p.43, 47-50)
def test_pixel_size_parfocal_parcentric_match_simulator_truth(sim):
    ctl, prof = sim.controller, sim.profile
    sim.capture.prepare_channel(M, "cube1")
    for slot in (1, 2, 4):
        r = C.measure_pixel_size(ctl, prof, M, slot)
        assert abs(r.deviation_pct) < 1.0, (slot, r)
    ctl.run(ctl.set_objective(1))
    sim.autofocus.find_sample(M, sim.vessel.focus_nominal_um, sim.vessel.focus_range_um)
    pf = C.measure_parfocality(ctl, prof, sim.autofocus, M, [1, 2, 3])
    for slot in (2, 3):
        assert abs(pf.offsets_um[slot] - PARFOCAL_UM[slot]) < 1.5, pf
    px = {s: prof.camera(M).pixel_size_um / prof.objective(s).magnification for s in (1, 2, 3)}
    pc = C.measure_parcentration(ctl, prof, M, px, [1, 2, 3])
    for slot in (2, 3):
        assert np.hypot(*(np.subtract(pc.offsets_um[slot], PARCENTRIC_UM[slot]))) < 2.0, pc


def test_vessel_alignment_and_hot_pixels(sim):
    ctl = sim.controller
    sim.capture.prepare_channel(M, "trans")
    ctl.run(ctl.set_objective(1))
    va = C.calibrate_vessel_alignment(ctl, sim.vessel, sim.nominal_map, M)
    assert np.hypot(*np.subtract(va.offset_um, TRUE_VESSEL_OFFSET_UM)) < 10
    sim.calibration.vessel_offset_um[sim.vessel.vessel_id] = va.offset_um
    sim.select_vessel(sim.vessel.vessel_id)
    assert sim.stage_map.verified and sim.stage_map.offset_x == pytest.approx(va.offset_um[0])
    hp = C.find_hot_pixels(ctl, M)
    truth = {tuple(p) for p in sim.rig.camera(M).hot_pixels.tolist()}
    assert set(hp.defects) == truth
    sim.calibration.hot_pixels["mono"] = hp.defects
    sim.capture.prepare_channel(M, "cube1")
    img = sim.capture.capture("cube1")
    md = dict(img.extra)
    assert md["hot_pixel_correction"].startswith(str(len(truth)))
    y, x = hp.defects[0]
    assert img.data[y, x] < 3000  # the defect is gone from the corrected capture


def test_calibration_store_roundtrip_and_compensation(sim, tmp_path):
    store = sim.calibration
    store.path = tmp_path / "cal.json"
    store.pixel_size_um["mono"] = {2: 0.3761}
    store.parfocal_um = {1: 0.0, 2: 3.0}
    store.parcentric_um = {1: (0.0, 0.0), 2: (4.0, -3.0)}
    store.touch("parfocality")
    again = C.CalibrationStore.load(store.path)
    assert again.pixel_size_um["mono"][2] == 0.3761 and again.parcentric_um[2] == (4.0, -3.0)
    assert sim.pixel_size_um(M, 2) == 0.3761
    ctl = sim.controller
    ctl.run(ctl.set_objective(1))
    z0, (x0, y0) = ctl.focus_z(), ctl.stage_xy()
    ctl.run(sim.change_objective(2))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and abs(ctl.focus_z() - (z0 + 3.0)) > 0.01:
        time.sleep(0.02)
    while time.monotonic() < deadline and abs(ctl.stage_xy()[0] - (x0 - 4.0)) > 0.01:
        time.sleep(0.02)
    assert ctl.focus_z() == pytest.approx(z0 + 3.0) and ctl.stage_xy() == pytest.approx((x0 - 4.0, y0 + 3.0))
    img = sim.capture.capture("trans")
    assert img.pixel_size_um == 0.3761 and dict(img.extra)["pixel_size_source"] == "calibrated"


# ------------------------------------------------------------------ time-lapse Z-stacks, stop
def test_timelapse_zstack_per_timepoint(sim):
    beacons = populate_wells(sim.vessel)[40:41]
    r = TimelapseRoutine(name="tz", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1"],
                         auto_fine_focus=False, beacons=beacons, scenes=[Scene(duration_s=10, interval_s=5)],
                         capture_one_frame_only=True, zstack=True, zstack_range_um=4, zstack_step_um=2)
    from openevos.acquisition.scan_runner import ScanRunner

    res = TimelapseRunner(ScanRunner(sim.capture, sim.autofocus, sim.vessel), sim.stage_map,
                          sim.core.clock.monotonic).run(r)
    assert res.completed and len(res.stacks) == 1
    stack = next(iter(res.stacks.values()))["cube1"]
    assert len(stack.images) == 3


def test_router_cancel_all(sim):
    ctl = sim.controller
    t = ctl.move_stage(45_000, 40_000)
    time.sleep(0.005)
    assert sim.core.router.cancel_all() >= 1
    assert t.wait(5).value in ("cancelled", "succeeded")  # a very short move may finish first
