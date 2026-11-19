"""Plate alignment on the physical stage (operator 2026-11-20: "since these plates are standard can we do it once and
save automatically"): two centred wells give the vessel <-> stage map, saved in the calibration file and used for
every standard-footprint vessel; the Capture page's vessel map then shows the stage and a well click moves there.
Simulated drive chain."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.calibration import CalibrationStore
from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings
from openevos.instrument.vessels import PlateAlignmentError, VesselStageMap, solve_plate_alignment

UM_PER_COUNT = 1270 / 2880


def counts(um: float) -> int:
    return round(um / UM_PER_COUNT)


# a stage whose Y runs against the plate's rows, as a mirrored axis would
TRUE = VesselStageMap(stage_frame="motor.stage", offset_x=58000.0, offset_y=39000.0, sign_x=1.0, sign_y=-1.0)


def test_two_wells_give_the_axis_directions_and_offset():
    a1, h12 = (14.38, 11.24), (113.38, 74.24)
    m, residual = solve_plate_alignment(a1, TRUE.to_stage(*a1), h12, TRUE.to_stage(*h12))
    assert (m.sign_x, m.sign_y) == (1.0, -1.0) and m.verified
    assert m.offset_x == pytest.approx(58000.0) and m.offset_y == pytest.approx(39000.0)
    assert residual == pytest.approx(0.0, abs=1e-6)
    for v in ((50.0, 40.0), (0.0, 85.48)):
        assert m.to_vessel(*m.to_stage(*v)) == pytest.approx(v)
    # centring is never exact: 60 µm off on one well
    s = TRUE.to_stage(*h12)
    _, residual = solve_plate_alignment(a1, TRUE.to_stage(*a1), h12, (s[0] + 60, s[1]))
    assert 25 < residual < 35


def test_bad_alignments_are_refused():
    a1, a2 = (14.38, 11.24), (23.38, 11.24)
    with pytest.raises(PlateAlignmentError, match="apart in X"):
        solve_plate_alignment(a1, TRUE.to_stage(*a1), a2, TRUE.to_stage(*a2))
    h12 = (113.38, 74.24)
    s = TRUE.to_stage(*h12)
    with pytest.raises(PlateAlignmentError, match="did not move"):  # 9 mm short in X: a wrong well, a slipped plate
        solve_plate_alignment(a1, TRUE.to_stage(*a1), h12, (s[0] - 9000, s[1]))


def test_calibration_file_keeps_the_alignment(tmp_path):
    c = CalibrationStore(path=tmp_path / "calibration.json")
    c.plate_alignment = {"sign_x": 1.0, "sign_y": -1.0, "offset_x": 58000.0, "offset_y": 39000.0}
    c.touch("plate_alignment")
    assert CalibrationStore.load(tmp_path / "calibration.json").plate_alignment == c.plate_alignment


def pump(app, s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          calibration_path=tmp_path / "calibration.json",
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    w = MainWindow(SessionModel(s))
    w.release_stage_lock = lambda: None  # a lock fails the test instead of opening a modal prompt
    w.show()
    pump(app, 0.5)
    yield app, w, s
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def put_stage(app, s, um):
    """Stand in for the operator's jog: the encoders jump, so the drift monitor is told a move is under way."""
    svc = s.telemetry[0]
    chain = svc.simulated_chain
    svc.monitor.begin_move({"X": counts(um[0]), "Y": counts(um[1])}, {})
    for ax, v in zip("XY", um, strict=True):
        chain.values[(ax, "APOS")] = chain.values[(ax, "CPOS")] = counts(v)
    pump(app, 0.2)
    svc.monitor.end_move(["X", "Y"], "test jog")
    pump(app, 0.3)
    assert not svc.monitor.latched


def test_align_once_then_the_map_shows_and_moves_the_stage(win, tmp_path):
    app, w, s = win
    cap = w.pages["capture"]
    s.select_vessel("plate-96")
    assert cap.align_row.isVisible() and not s.stage_map.verified
    chain = s.telemetry[0].simulated_chain
    before = (chain.values[("X", "APOS")], chain.values[("Y", "APOS")])
    v = s.vessel
    cap._goto_vessel(v.regions[5].cx, v.regions[5].cy)  # not aligned: nothing moves
    pump(app, 0.5)
    assert (chain.values[("X", "APOS")], chain.values[("Y", "APOS")]) == before
    assert cap.map.position_mm is None

    # a true map that puts well D6 in the middle of the drives' travel box
    lx, ly = (s.controller.axis_limits(s.profile.stage_device, a) for a in "xy")
    d6 = next(r for r in v.regions if r.region_id == "D6")
    mid = ((lx[0] + lx[1]) / 2, (ly[0] + ly[1]) / 2)
    probe = VesselStageMap(sign_x=1.0, sign_y=-1.0)
    true = probe.model_copy(update={"offset_x": mid[0] - probe.to_stage(d6.cx, d6.cy)[0],
                                    "offset_y": mid[1] - probe.to_stage(d6.cx, d6.cy)[1]})
    first, last = v.regions[0], v.regions[-1]
    cap._align_step()  # start
    assert cap.btn_align.text() == f"Record {first.region_id}" and cap.lbl_align.isVisible()
    put_stage(app, s, true.to_stage(first.cx, first.cy))
    cap._align_step()
    assert cap.btn_align.text() == f"Record {last.region_id}"
    put_stage(app, s, true.to_stage(last.cx, last.cy))
    cap._align_step()
    assert cap.btn_align.text() == "Align plate…" and not cap.lbl_align.isVisible()
    m = s.stage_map
    assert m.verified and (m.sign_x, m.sign_y) == (1.0, -1.0)
    assert m.offset_x == pytest.approx(true.offset_x, abs=1.0) and m.offset_y == pytest.approx(true.offset_y, abs=1.0)
    saved = CalibrationStore.load(tmp_path / "calibration.json").plate_alignment
    assert saved["sign_y"] == -1.0 and [x["region"] for x in saved["wells"]] == [first.region_id, last.region_id]

    # another standard plate uses the same alignment, without asking
    s.select_vessel("plate-24")
    assert s.stage_map.verified and s.stage_map.offset_x == m.offset_x
    s.select_vessel("plate-96")

    # the map shows the stage, and a click on D6 moves it there (X then Y, by the stage service)
    cap._goto_vessel(d6.cx, d6.cy)
    target = (counts(mid[0]), counts(mid[1]))
    end = time.monotonic() + 20
    while time.monotonic() < end and (abs(chain.values[("X", "APOS")] - target[0]) > 5
                                      or abs(chain.values[("Y", "APOS")] - target[1]) > 5):
        pump(app, 0.1)
    assert abs(chain.values[("X", "APOS")] - target[0]) <= 5 and abs(chain.values[("Y", "APOS")] - target[1]) <= 5
    pump(app, 0.5)
    cap._refresh_state()
    assert cap.map.position_mm == pytest.approx((d6.cx, d6.cy), abs=0.01)
    assert cap.lbl_well.text().startswith("In D6")

    # a well outside the travel box is refused, nothing sent
    here = (chain.values[("X", "APOS")], chain.values[("Y", "APOS")])
    cap._goto_vessel(first.cx, first.cy)
    pump(app, 0.5)
    assert (chain.values[("X", "APOS")], chain.values[("Y", "APOS")]) == here


def test_a_bad_recording_saves_nothing(win, tmp_path):
    app, w, s = win
    cap = w.pages["capture"]
    s.select_vessel("plate-96")
    cap._align_step()
    put_stage(app, s, (5000.0, 5000.0))
    cap._align_step()
    put_stage(app, s, (6000.0, 5500.0))  # barely moved: not the far well
    cap._align_step()
    assert not s.stage_map.verified and s.calibration.plate_alignment == {}
    assert cap.btn_align.text() == "Align plate…"
