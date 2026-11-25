"""Z-stack engine, projections, saving and page (ADR-0064), on the simulator.

Each audit finding (ZF-*, ZU-*) that the upgrade fixes has a test here that would fail on the old code.
"""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np
import pytest

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.session import build_sim_session
from openevos.acquisition.zstack import (
    MAX_SLICES,
    ChannelOrder,
    ZStackNeedsConfirmation,
    ZStackPlan,
    ZStackRunner,
    optimal_step_um,
    plan_from_settings,
    plan_positions,
    preflight,
)
from openevos.core.clock import ThreadClock
from openevos.imaging.zproject import METHODS, best_index, focus_curve, project
from openevos.imaging.zstack_io import save_zstack_set
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings

MONO = CameraRole.MONO


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fps=10.0, settings=AppSettings())
    time.sleep(0.3)
    yield s
    s.shutdown()


def plan(sim, lo=-4.0, hi=4.0, step=2.0, channels=("cube1",), **kw):
    z = sim.controller.focus_z()
    return ZStackPlan(channels=tuple(channels), role=MONO, bottom_um=z + lo, top_um=z + hi, step_um=step, **kw)


class Spy:
    """Records focus targets and light commands (what the drive and the LED board are asked to do)."""

    def __init__(self, ctl):
        self.targets: list[tuple[float, bool]] = []
        self.lights: list[tuple] = []
        mf, sl = ctl.move_focus, ctl.set_light

        def move_focus(z, *a, **k):
            self.targets.append((round(z, 3), bool(k.get("confirmed"))))
            return mf(z, *a, **k)

        def set_light(ch, pct, on):
            self.lights.append((ch, pct, on))
            return sl(ch, pct, on)

        ctl.move_focus, ctl.set_light = move_focus, set_light


# ------------------------------------------------------------------------------------------------ planning
def test_range_mode_stays_inside_the_range():  # ZF-07: 10 + 10 with step 3 went to +11
    assert plan_positions(-10, 10, 3) == [-10, -7, -4, -1, 2, 5, 8]
    assert plan_positions(10, -10, 2.5)[0] == -10 and plan_positions(10, -10, 2.5)[-1] == 10
    assert len(plan_positions(0, 20, 0.1)) == 201  # float steps do not lose the last slice


def test_centre_mode_always_includes_the_focus_plane():
    pos = plan_positions(-10, 10, 3, centre_um=0)
    assert 0 in pos and pos == [-9, -6, -3, 0, 3, 6, 9]
    assert plan_positions(-4, 10, 2, centre_um=0) == [-4, -2, 0, 2, 4, 6, 8, 10]


def test_optimal_step_from_the_objective():
    assert optimal_step_um(0.3, 520) == pytest.approx(5.64, abs=0.05)  # 10x / 0.30, green emission
    assert optimal_step_um(0.65, 460) == pytest.approx(0.96, abs=0.05)  # 40x / 0.65, DAPI
    assert optimal_step_um(None, 520) is None and optimal_step_um(1.2, 520) is None  # dry objective cannot be 1.2


def test_plan_from_settings_both_modes():
    zs = AppSettings().zstack
    p = plan_from_settings(zs, MONO, ("cube1",), 1000.0)
    assert (p.bottom_um, p.top_um, p.centre_um) == (990.0, 1010.0, 1000.0)
    zs.mode = "range"
    with pytest.raises(CaptureRejected, match="top and the bottom"):
        plan_from_settings(zs, MONO, ("cube1",), 1000.0)
    p = plan_from_settings(zs, MONO, ("cube1",), 1000.0, bottom_um=1020, top_um=995)
    assert (p.bottom_um, p.top_um, p.centre_um) == (995, 1020, None)


# ------------------------------------------------------------------------------------------------ pre-flight
def test_preflight_finds_problems_before_anything_moves(sim):
    spy = Spy(sim.controller)
    pf = preflight(sim.capture, plan(sim, -100, 100, 0.5))
    assert any(f"limit is {MAX_SLICES}" in e for e in pf.errors)
    lim = sim.controller.axis_limits(sim.profile.focus_device, "z")
    z = sim.controller.focus_z()
    pf = preflight(sim.capture, plan(sim, lim[0] - z - 5, lim[0] - z + 5, 1))
    assert any("outside the focus limits" in e for e in pf.errors)
    pf = preflight(sim.capture, ZStackPlan(("cube1",), CameraRole.COLOR, z - 2, z + 2, 1))
    assert any("cannot image" in e for e in pf.errors)
    pf = preflight(sim.capture, plan(sim, -10, 10, 3))
    assert pf.ok and pf.notes and "short of the top" in pf.notes[0]
    assert pf.seconds > 0 and pf.megabytes > 0 and "slices" in pf.summary()
    assert spy.targets == []


def test_safe_height_is_asked_before_the_first_move(sim):  # ZF-04: refused mid-stack after 5 slices
    z = sim.controller.focus_z()
    sim.settings.basic.focus_limit_um = {str(sim.controller.objective_slot()): z + 4}
    spy = Spy(sim.controller)
    with pytest.raises(ZStackNeedsConfirmation, match="safe height"):
        ZStackRunner(sim.capture).run(plan(sim, -4, 8, 2))
    assert spy.targets == []  # nothing moved
    res = ZStackRunner(sim.capture).run(plan(sim, -4, 8, 2, confirmed_above_safe=True))
    assert res.complete and len(res.stack().images) == 7
    above = [c for zt, c in spy.targets if zt > z + 4.5]
    assert above and all(above)  # only the moves above the safe height carry the confirmation
    assert not any(c for zt, c in spy.targets if zt <= z + 4.5)


# ------------------------------------------------------------------------------------------------ running
def test_light_is_put_back_as_it_was(sim):  # ZF-01: left on at 75 %
    ctl = sim.controller
    assert not ctl.light.enabled
    ZStackRunner(sim.capture).run(plan(sim))
    assert not ctl.light.enabled
    ctl.run(ctl.set_light("cube2", 33.0, True))
    ZStackRunner(sim.capture).run(plan(sim))
    assert (ctl.light.channel, ctl.light.intensity_percent, ctl.light.enabled) == ("cube2", 33.0, True)


def test_live_view_pauses_once_per_stack(sim):  # ZF-03: 24 stops and starts for 11 slices
    ctl = sim.controller
    ctl.start_live(MONO)
    time.sleep(0.3)
    cam = ctl.camera_backend(MONO)
    live_starts = []
    orig = cam.start_stream

    def start_stream(*a, **k):
        if k.get("acquisition_id") is None:
            live_starts.append(1)
        return orig(*a, **k)

    cam.start_stream = start_stream
    res = ZStackRunner(sim.capture).run(plan(sim, -10, 10, 2))
    assert res.complete and len(live_starts) == 1 and ctl.is_live(MONO)


def test_every_slice_is_approached_from_below(sim):  # ZF-08
    spy = Spy(sim.controller)
    z = sim.controller.focus_z()
    res = ZStackRunner(sim.capture).run(plan(sim, -4, 4, 2))
    t = [zt for zt, _c in spy.targets]
    assert t[0] == pytest.approx(z - 4 - 2)  # under-shoot, then up to the first slice
    assert t[1:6] == pytest.approx([z - 4, z - 2, z, z + 2, z + 4])
    assert t[-1] == pytest.approx(z) and res.complete


def test_stop_keeps_slices_and_causes_no_move(sim):  # ZF-02, ZF-05, ZF-11
    spy = Spy(sim.controller)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", sim.capture.run_zstack(plan(sim, -20, 20, 1))))
    th.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(spy.targets) < 8:
        time.sleep(0.01)
    assert sim.capture.stop_zstack()
    n = len(spy.targets)
    th.join(30)
    res = out["r"]
    assert res.status == "stopped" and 0 < len(res.stack().images) < 41 and not res.stack().complete
    assert len(spy.targets) == n  # no new move after the Stop, not even back to the start
    assert sim.capture.buffers.zstack_set is res  # the partial stack is what the page shows
    assert not sim.controller.light.enabled
    assert not sim.capture.stop_zstack()  # nothing running any more


def test_stop_all_reaches_the_stack(sim):
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", sim.capture.run_zstack(plan(sim, -20, 20, 1))))
    th.start()
    time.sleep(0.8)
    sim.capture.stop_zstack()
    sim.core.router.cancel_all()  # what SessionModel.stop_all does after stop_zstack
    th.join(30)
    assert out["r"].status == "stopped"


def test_a_failure_keeps_the_slices_and_returns_focus(sim):
    z = sim.controller.focus_z()
    acq = sim.controller.acquire
    calls = []

    def flaky(role, n=1, timeout=30.0):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("camera went away")
        return acq(role, n, timeout)

    sim.controller.acquire = flaky
    res = ZStackRunner(sim.capture).run(plan(sim, -4, 4, 2))
    assert res.status == "failed" and "camera went away" in res.message and len(res.stack().images) == 2
    assert sim.controller.focus_z() == pytest.approx(z) and not sim.controller.light.enabled


def test_channel_order(sim):
    spy = Spy(sim.controller)
    ZStackRunner(sim.capture).run(plan(sim, -2, 2, 2, channels=("cube1", "cube2")))
    on = [ch for ch, _p, enabled in spy.lights if enabled]
    assert on == ["cube1", "cube2"]  # one change of light cube for the whole stack
    spy.lights.clear()
    res = ZStackRunner(sim.capture).run(plan(sim, -2, 2, 2, channels=("cube1", "cube2"),
                                             order=ChannelOrder.Z_FIRST))
    on = [ch for ch, _p, enabled in spy.lights if enabled]
    assert on == ["cube1", "cube2"] * 3 and {s.channel for s in res.stacks} == {"cube1", "cube2"}


def test_light_off_between_slices(sim):
    spy = Spy(sim.controller)
    ZStackRunner(sim.capture).run(plan(sim, -2, 2, 2, light_between_slices=True))
    states = [enabled for _c, _p, enabled in spy.lights]
    assert states[:7] == [False, True, False, True, False, True, False]  # set up dark, on only to expose
    assert not sim.controller.light.enabled


def test_settle_time_is_waited(sim):
    waits = []
    ZStackRunner(sim.capture, sleep=waits.append).run(plan(sim, -2, 2, 2, settle_ms=40))
    assert waits == [0.04, 0.04, 0.04]


def test_old_single_channel_call_still_works(sim):
    z = sim.controller.focus_z()
    st = sim.capture.zstack("cube1", z - 4, z + 4, 2)
    assert len(st.images) == 5 and st.complete and sim.capture.buffers.zstack is st


# ------------------------------------------------------------------------------------------------ projections
def _blurred_stack():
    from scipy import ndimage

    rng = np.random.default_rng(3)
    sharp = (rng.random((64, 64)) * 4000).astype(np.uint16)
    stack = [ndimage.gaussian_filter(sharp.astype(float), s).astype(np.uint16) for s in (3, 1.5, 0, 1.5, 3)]
    return sharp, stack


def test_projections_keep_values_and_dtype():
    _sharp, stack = _blurred_stack()
    for m in METHODS:
        out = project(stack, m)
        want = {"sum": np.uint32, "std": np.float32}.get(m, np.uint16)  # derived sums leave the camera's range
        assert out.dtype == want and out.shape == (64, 64)
    assert np.array_equal(project(stack, "max"), np.max(np.stack(stack), axis=0))
    assert np.array_equal(project(stack, "min"), np.min(np.stack(stack), axis=0))


def test_best_focus_and_extended_focus():
    sharp, stack = _blurred_stack()
    assert best_index(stack) == 2 and np.argmax(focus_curve(stack)) == 2
    assert np.array_equal(project(stack, "best"), sharp)
    # left half sharp in slice 0, right half sharp in slice 1: extended focus takes each half from its sharp slice
    from scipy import ndimage

    blur = ndimage.gaussian_filter(sharp.astype(float), 3).astype(np.uint16)
    a, b = blur.copy(), blur.copy()
    a[:, :32], b[:, 32:] = sharp[:, :32], sharp[:, 32:]
    edf = project([a, b], "edf")
    assert np.mean(edf[:, 4:28] == sharp[:, 4:28]) > 0.95 and np.mean(edf[:, 36:60] == sharp[:, 36:60]) > 0.95


def test_colour_slices_project_in_colour():
    rgb = [np.full((8, 8, 3), v, np.uint8) for v in (10, 200, 50)]
    assert project(rgb, "max").shape == (8, 8, 3) and project(rgb, "max")[0, 0, 0] == 200
    assert project(rgb, "edf").shape == (8, 8, 3)


# ------------------------------------------------------------------------------------------------ saving
def test_save_writes_ome_projections_and_record(sim, tmp_path):
    import tifffile

    res = ZStackRunner(sim.capture).run(plan(sim, -2, 2, 2, channels=("cube1", "cube2")))
    folder = save_zstack_set(tmp_path, res, projections=("max", "edf"))
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["cube1_edf.tif", "cube1_max.tif", "cube2_edf.tif", "cube2_max.tif", "stack.ome.tif",
                     "zstack.json"]
    with tifffile.TiffFile(folder / "stack.ome.tif") as tf:
        assert tf.series[0].shape[:2] == (3, 2) and tf.series[0].axes.endswith("ZCYX")
        assert tf.ome_metadata.count("PositionZ=") == 6 and "SIMULATED" in tf.ome_metadata
    rec = json.loads((folder / "zstack.json").read_text())
    assert rec["status"] == "completed" and rec["synthetic"] and len(rec["channels"][0]["slices"]) == 3
    again = save_zstack_set(tmp_path, res)
    assert again != folder  # two saves never share a folder
    assert not any(p.name.startswith(".") for p in folder.iterdir())  # no temporary files left behind


def test_partial_runs_save_per_channel(sim, tmp_path):
    from dataclasses import replace

    res = ZStackRunner(sim.capture).run(plan(sim, -2, 2, 2, channels=("cube1", "cube2")))
    short = replace(res.stacks[1], z_um=res.stacks[1].z_um[:2], images=res.stacks[1].images[:2], complete=False)
    partial = replace(res, stacks=(res.stacks[0], short), status="stopped", message="stopped")
    folder = save_zstack_set(tmp_path, partial, projections=())
    assert sorted(p.name for p in folder.iterdir()) == ["cube1.ome.tif", "cube2.ome.tif", "zstack.json"]
    assert json.loads((folder / "zstack.json").read_text())["channels"][1]["complete"] is False


# ------------------------------------------------------------------------------------------------ time-lapse
def test_timelapse_stacks_go_to_disk_with_an_output_folder(sim, tmp_path):  # ZF-06
    from openevos.acquisition.scan_runner import ScanRunner
    from openevos.acquisition.timelapse import Scene, TimelapseRoutine, TimelapseRunner, populate_wells

    beacons = populate_wells(sim.vessel)[40:41]
    r = TimelapseRoutine(name="tz", vessel_id=sim.vessel.vessel_id, objective_slot=2, channels=["cube1"],
                         auto_fine_focus=False, beacons=beacons, scenes=[Scene(duration_s=10, interval_s=5)],
                         capture_one_frame_only=True, zstack=True, zstack_range_um=4, zstack_step_um=2,
                         output_folder=str(tmp_path))
    before = sim.capture.buffers.zstack_set
    res = TimelapseRunner(ScanRunner(sim.capture, sim.autofocus, sim.vessel), sim.stage_map,
                          sim.core.clock.monotonic).run(r)
    assert res.completed and not res.stacks and len(res.stack_folders) == 1
    folder = next(iter(res.stack_folders.values()))
    assert os.path.exists(os.path.join(folder, "stack.ome.tif"))
    assert sim.capture.buffers.zstack_set is before  # the Z-stack page's stack is not replaced


# ------------------------------------------------------------------------------------------------ settings
def test_settings_without_a_zstack_section_still_load(tmp_path):
    s = AppSettings()
    s.zstack.step_um = 0.7
    p = tmp_path / "ui_settings.json"
    s.save(p)
    assert AppSettings.load(p).zstack.step_um == 0.7
    d = json.loads(p.read_text())
    d.pop("zstack")
    p.write_text(json.dumps(d))
    assert AppSettings.load(p).zstack.mode == "centre"


# ------------------------------------------------------------------------------------------------ the page
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from openevos.ui import theme

    a = QApplication.instance() or QApplication([])
    theme.apply_theme("dark", a)
    return a


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    s = build_sim_session(ThreadClock(), fast=True, fps=20, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.resize(1440, 900)
    w.show()
    pump(app, 0.5)
    w.navigate("zstack")
    pump(app, 0.3)
    yield w
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def pump(app, t):
    end = time.monotonic() + t
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_idle(app, w, t=30):
    end = time.monotonic() + t
    pump(app, 0.2)
    while time.monotonic() < end and w.model.tasks.busy:
        pump(app, 0.05)
    pump(app, 0.2)


def test_page_shows_the_plan_before_running(app, window):
    pg = window.pages["zstack"]
    n = len(pg.zview.positions)
    assert pg.fig["slices"].text().startswith(str(n)) and "MB" in pg.fig["size"].text()  # ZU-04; design U6
    assert pg.zview.positions and pg.zview.focus is not None  # ZU-03: the stack is drawn
    assert pg.btn_optimal.isEnabled() and pg.btn_optimal.text().startswith("Use Nyquist ")  # design U2: shows its value
    pg._use_optimal()
    assert pg.step.value() == pytest.approx(pg._optimal(), abs=0.01)
    assert window.session.settings.zstack.step_um == pytest.approx(pg.step.value())  # remembered


def test_page_top_and_bottom_from_live_focus(app, window):  # ZU-01
    pg = window.pages["zstack"]
    ctl = window.session.controller
    pg.mode.buttons["range"].click()
    pump(app, 0.1)
    assert not pg.btn.isEnabled() and "top and the bottom" in pg.lbl_error.text().lower()  # an error, in red
    z = ctl.focus_z()
    pg._set_end("bottom")
    ctl.run(ctl.move_focus(z + 6))
    pg._set_end("top")
    pump(app, 0.1)
    assert pg.btn.isEnabled()
    assert pg.zview.positions[0] == pytest.approx(z) and pg.zview.positions[-1] == pytest.approx(z + 6)


def test_page_runs_shows_progress_stop_and_projections(app, window, tmp_path):
    pg = window.pages["zstack"]
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(1)
    assert pg.acquire()
    pump(app, 0.4)
    assert pg.btn.text().startswith("Stop") and pg.progress.isVisible()  # ZU-02
    pg._acquire_or_stop()  # Stop (keep slices)
    wait_idle(app, window)
    zs = window.session.capture.buffers.zstack_set
    assert zs is not None and zs.status == "stopped" and pg.lbl_status.isVisible()
    assert pg.btn.text() == "Acquire Z-stack"
    pg.step.setValue(2)
    assert pg.acquire()
    wait_idle(app, window)
    zs = window.session.capture.buffers.zstack_set
    assert zs.complete and not pg.lbl_status.isVisible() and pg.view_mode.current() == "slice"
    assert len(pg.curve.values) == len(zs.stack().images)
    for m in ("max", "best", "edf"):
        pg.view_mode.pick(m)
        pump(app, 0.1)
        assert not pg.slice.isEnabled() and not pg.slice.isVisibleTo(pg)  # design U12: no dead slider
        assert pg.view.badge.startswith("SHARPEST PLANE" if m == "best" else METHODS[m].upper())  # ZU-06
    pg._save(ask=False, root=str(tmp_path))
    end = time.monotonic() + 15
    while time.monotonic() < end and not list(tmp_path.glob("zstack_*/zstack.json")):
        pump(app, 0.05)
    assert list(tmp_path.glob("zstack_*/stack.ome.tif"))


def test_page_asks_before_going_above_the_safe_height(app, window):
    pg = window.pages["zstack"]
    s = window.session
    z = s.controller.focus_z()
    s.settings.basic.focus_limit_um = {str(s.controller.objective_slot()): z + 2}
    pg._update_setup()
    assert "safe height" in pg.lbl_warn.text()
    asked = []
    assert not pg.acquire(ask=lambda q: asked.append(q) or False)
    assert asked and not window.model.tasks.busy
    assert pg.acquire(ask=lambda q: True)
    wait_idle(app, window)
    assert s.capture.buffers.zstack_set.complete


def test_hot_button_dialog_shares_the_settings(app, window):
    from openevos.ui.dialogs import ZStackDialog

    zs = window.session.settings.zstack
    d = ZStackDialog(1000.0, zs, lambda b, a, st: f"{b}/{a}/{st}")
    d.below.setValue(3)
    d.step.setValue(0.5)
    assert d.lbl_estimate.text() == f"3.0/{zs.above_um}/0.5"
    d._accept()
    assert (zs.below_um, zs.step_um) == (3.0, 0.5)


# ------------------------------------------------------------------ design review 2026-11-24 (Design re-review tab)
def test_page_slices_and_step_edit_each_other(app, window):  # U3
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(1)
    pump(app, 0.1)
    assert pg.slices.value() == 21
    pg.slices.setValue(11)
    pump(app, 0.1)
    assert pg.step.value() == pytest.approx(2.0) and len(pg.zview.positions) == 11


def test_page_notes_heavy_oversampling_and_types_its_messages(app, window):  # U2, U5
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(0.5)
    pump(app, 0.1)
    opt = pg._optimal()
    assert opt and opt > 4 * 0.5  # the simulator's 4x NA 0.13
    note = pg.lbl_note.text()
    assert "finer than Nyquist" in note and "41 slices where 2 would do" in note
    assert pg.lbl_note.isVisibleTo(pg) and not pg.lbl_error.isVisibleTo(pg)
    pg.step.setValue(round(opt, 2))
    pump(app, 0.1)
    assert "finer than Nyquist" not in pg.lbl_note.text()


def test_page_channel_chips_show_state_without_colour(app, window):  # U1, A1
    pg = window.pages["zstack"]
    chips = pg.chips
    assert chips.slots() == pg.channel_slots() and chips.checked()
    slot = next(s for s in chips.slots() if s not in chips.checked())
    chips.set_checked(slot, True)
    pump(app, 0.05)
    assert slot in pg.checked_channels() and chips.buttons[slot].text().startswith("✓ ")
    assert window.session.settings.zstack.channels == pg.checked_channels()  # remembered
    chips.set_checked(slot, False)
    assert not chips.buttons[slot].text().startswith("✓ ")


def test_page_controls_have_names_and_one_tab_stop_per_choice(app, window):  # A2, A4
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox, QSlider

    pg = window.pages["zstack"]
    for w in pg.findChildren(QAbstractButton) + pg.findChildren(QAbstractSpinBox) + pg.findChildren(QComboBox) + \
            pg.findChildren(QSlider):
        text = w.text() if isinstance(w, QAbstractButton) else ""
        has_words = any(ch.isalpha() for ch in text)
        assert has_words or w.accessibleName(), f"{type(w).__name__} {text!r} has no name"
    tab = [b for b in pg.view_mode.results.buttons.values() if b.focusPolicy() & Qt.FocusPolicy.TabFocus]
    assert len(tab) <= 1


def test_page_shows_slices_as_they_arrive_and_fills_the_drawing(app, window):  # U4
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(0.5)
    seen = []
    orig = pg._arrived

    def spy(slot, index, done, img):
        seen.append((index, done))
        orig(slot, index, done, img)

    pg._arrived = spy
    assert pg.acquire()
    end = time.monotonic() + 10
    while time.monotonic() < end and len(seen) < 3:
        pump(app, 0.05)
    assert seen and pg.view.badge.startswith("ACQUIRING") and pg.zview.taken is not None
    assert pg.curve.live and len(pg.curve.values) >= 1
    pg._acquire_or_stop()
    wait_idle(app, window)
    assert pg.zview.taken is None and not pg.curve.live


def test_page_safe_height_line_turns_red_only_when_crossed(app, window):  # U9
    pg = window.pages["zstack"]
    s = window.session
    z = s.controller.focus_z()
    pg.mode.buttons["centre"].click()
    pg.below.setValue(4)
    pg.above.setValue(4)
    s.settings.basic.focus_limit_um = {str(s.controller.objective_slot()): z + 20}
    pg._update_setup()
    assert pg.zview.crossed() == 0 and not pg.lbl_warn.isVisibleTo(pg)
    s.settings.basic.focus_limit_um = {str(s.controller.objective_slot()): z + 2}
    pg._update_setup()
    assert pg.zview.crossed() > 0 and "above the" in pg.lbl_warn.text()


def wait_for(app, cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        pump(app, 0.05)
    return bool(cond())


def test_page_keyboard_nudge_and_reading_order(app, window):  # U13, A4
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    pg = window.pages["zstack"]
    ctl = window.session.controller
    z0 = ctl.focus_z()
    pg.btn_optimal.setFocus()
    QTest.keyClick(pg.btn_optimal, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    assert wait_for(app, lambda: ctl.focus_z() is not None and ctl.focus_z() > z0 + 0.5)
    chips = list(pg.chips.buttons.values())
    assert pg.btn_go_bottom.nextInFocusChain() is chips[0]  # rebuilt channel rows stay in reading order


# ------------------------------------------------------------------ redesign kit review 2026-11-25 (patch v3)
def test_projections_sum_median_std_match_numpy():  # kit ZP-020, ZP-021
    rng = np.random.default_rng(3)
    st = [rng.integers(0, 4096, (40, 30), dtype=np.uint16) for _ in range(7)]
    arr = np.stack(st).astype(np.float64)
    s = project(st, "sum")
    assert s.dtype == np.uint32 and np.array_equal(s, arr.sum(0).astype(np.uint32))
    m = project(st, "median")
    assert m.dtype == np.uint16 and np.array_equal(m, np.rint(np.median(arr, 0)).astype(np.uint16))
    d = project(st, "std")
    assert d.dtype == np.float32 and np.allclose(d, arr.std(0), atol=1e-2)
    assert METHODS["best"] == "Sharpest plane"  # one whole slice, named for what it is (kit ZP-022)


def test_runner_stamps_the_start_before_the_first_move(sim):
    from datetime import datetime, timezone

    t0 = datetime.now(timezone.utc)
    res = ZStackRunner(sim.capture).run(plan(sim, -2, 2, 2))
    assert t0 <= res.started_at and (res.started_at - t0).total_seconds() < res.seconds


def test_pause_turns_the_light_off_and_resume_continues_once(sim):  # kit ZA-003..ZA-005
    from openevos.acquisition.scan_runner import RunControl

    ctl = sim.controller
    control = RunControl()
    seen_paused: list[bool] = []
    lights: list[bool] = []

    def progress(done, total, msg):
        if msg.startswith("Paused"):
            seen_paused.append(True)
            lights.append(bool(ctl.light.enabled))
            threading.Timer(0.3, control.resume).start()

    def on_image(slot, index, done, img):
        if done == 2:
            control.pause()

    res = ZStackRunner(sim.capture, control=control).run(plan(sim, -4, 4, 2), progress, on_image=on_image)
    assert seen_paused and lights == [False]  # paused once, with the light off
    assert res.complete and len(res.stack().images) == 5  # resumed: no slice repeated or skipped
    assert [round(z, 3) for z in res.stack().z_um] == [round(z, 3) for z in res.planned_z_um]


def test_page_keep_slices_redivides_a_new_range(app, window):  # kit ZP-004, ZP-005
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(2)
    pump(app, 0.05)
    assert pg.slices.value() == 11
    pg.keep.buttons["slices"].click()
    pg.above.setValue(20)
    pg.below.setValue(20)
    pump(app, 0.05)
    assert pg.slices.value() == 11 and pg.step.value() == pytest.approx(4.0)
    pg.keep.buttons["interval"].click()
    pg.above.setValue(10)
    pg.below.setValue(10)
    pump(app, 0.05)
    assert pg.step.value() == pytest.approx(4.0) and pg.slices.value() == 5  # 2 whole steps each side + centre
    assert window.session.settings.zstack.keep == "interval"


def test_page_checklist_and_storage_block(app, window, monkeypatch, tmp_path):  # kit ZP-014
    import shutil as _sh

    pg = window.pages["zstack"]
    zs = window.session.settings.zstack
    zs.save_folder = str(tmp_path)
    pg.chk_autosave.setChecked(True)
    pump(app, 0.05)
    assert "free" in pg.lbl_checks.text() and pg.btn.isEnabled()
    usage = _sh.disk_usage(tmp_path)
    monkeypatch.setattr("openevos.ui.pages.zstack.shutil.disk_usage",
                        lambda _p: usage._replace(free=1000))
    pg._update_setup()
    assert not pg.btn.isEnabled() and "Not enough space" in pg.lbl_error.text()
    monkeypatch.undo()
    pg.chk_autosave.setChecked(False)
    pg._update_setup()
    assert pg.btn.isEnabled() and "Auto-save off" in pg.lbl_checks.text()


def test_page_pause_resume_filmstrip_and_projection_picker(app, window):  # kit ZA-003, ZR-001, ZR-002
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(0.5)
    assert pg.acquire()
    assert wait_for(app, lambda: pg.btn_pause.isVisible() and pg.strip.count() == 41)
    pg._pause_or_resume()
    assert wait_for(app, lambda: pg.lbl_progress.text().startswith("Paused"))
    assert not window.session.controller.light.enabled
    pg._pause_or_resume()
    assert wait_for(app, lambda: not pg.lbl_progress.text().startswith(("Paused", "Resuming")))
    pg._acquire_or_stop()  # Stop (keep slices)
    wait_idle(app, window)
    zs = window.session.capture.buffers.zstack_set
    taken = len(zs.stack().images)
    assert 0 < taken < 41 and pg.strip.count() == 41  # gaps stay visible
    assert pg.strip.item(taken - 1).data(0x0101) and not pg.strip.item(taken).data(0x0101)
    for m in ("sum", "median", "std", "best"):
        pg.view_mode.pick(m)
        pump(app, 0.05)
        assert pg.view_mode.current() == m and pg.view.badge.split(" • ")[0] in (METHODS[m].upper(), "SHARPEST PLANE")
    row = 0 if pg.strip.currentRow() != 0 else 1
    pg.strip.setCurrentRow(row)
    assert pg.view_mode.current() == "slice" and pg.slice.value() == row


# ------------------------------------------------------------------ design v4: merged, sections, range, details
def test_zviews_merge_sections_and_sharp_range():
    from openevos.imaging.zviews import merge_rgb, ortho_composite, sharp_range, z_at

    red = np.zeros((4, 5, 3), np.uint8)
    red[..., 0] = 200
    grey = np.full((4, 5, 3), 100, np.uint8)
    m = merge_rgb([(red, True), (grey, False)])
    assert m[0, 0].tolist() == [250, 50, 50]  # fluorescence added on a half-bright brightfield base
    assert merge_rgb([(red, True), (red, True)])[0, 0, 0] == 255  # clipped, never wrapped

    vol = np.zeros((10, 40, 60, 3), np.uint8)
    for k in range(10):
        vol[k, :, :, 1] = k * 20  # brightness = slice number
    canvas, lay = ortho_composite(vol, 30, 20, 4, step_um=2.0, pixel_um=1.0)
    assert lay.to_scale and lay.depth == 20 and lay.stretch == 1.0  # 10 slices x 2 µm / 1 µm px
    assert canvas.shape == (40 + lay.gap + 20, 60 + lay.gap + 20, 3)
    assert lay.region(5, 5) == "xy" and lay.region(70, 5) == "yz" and lay.region(5, 50) == "xz"
    assert lay.slice_at(19.9, 10) == 9 and lay.slice_at(0, 10) == 0
    _c2, thin = ortho_composite(vol, 0, 0, 0, step_um=0.1, pixel_um=1.0)  # 1 px true depth: stretched, and said so
    assert thin.stretch > 1 and thin.depth >= 10
    _c3, unknown = ortho_composite(vol, 0, 0, 0, step_um=1.0, pixel_um=None)
    assert not unknown.to_scale

    curve = [1, 1, 2, 5, 9, 5, 2, 1, 1]
    lo, hi = sharp_range(curve)
    assert 2 < lo < 4 < hi < 6 and z_at(lo, [10 + k for k in range(9)]) == pytest.approx(10 + lo)
    assert sharp_range([1, 2, 3, 4, 5]) is None  # peak at the edge: half height not crossed on both sides
    assert sharp_range([3, 3, 3]) is None


def test_page_merged_sections_range_and_details(app, window):
    pg = window.pages["zstack"]
    for slot in pg.channel_slots()[:2]:
        pg.chips.set_checked(slot, True)
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(1)
    assert pg.acquire()
    wait_idle(app, window)
    assert pg.view_channel.findData("__merged__") >= 0
    pg.view_channel.setCurrentIndex(pg.view_channel.findData("__merged__"))
    pg.view_mode.pick("max")
    pump(app, 0.1)
    assert pg.view.badge.startswith("MERGED ") and "MAXIMUM" in pg.view.badge
    assert pg.proj_from.isVisibleTo(pg) and pg.proj_to.value() == 21
    pg.proj_from.setValue(5)
    pg.proj_to.setValue(9)
    pump(app, 0.05)
    assert "SLICES 5–9 OF 21" in pg.view.badge
    pg._use_sharp_range()
    pump(app, 0.05)
    assert 1 <= pg.proj_from.value() <= pg.proj_to.value() <= 21

    pg.view_mode.pick("ortho")
    pump(app, 0.1)
    lay = pg._ortho_layout
    assert lay is not None and "SECTIONS" in pg.view.badge and pg.slice.isVisibleTo(pg)
    pg._on_view_click(5, lay.height + lay.gap + lay.depth - 1, None)  # bottom row of XZ: the last slice
    assert pg.slice.value() == pg.slice.maximum()
    assert pg.curve.summary().startswith("sharpest ")

    pg.btn_details.setChecked(True)
    pump(app, 0.05)
    text = pg.details.text()
    assert pg.details.isVisibleTo(pg) and "21 slices" in text and "ms, gain" in text and "simulated" in text


def test_saved_stack_is_reproducible(tmp_path, sim):  # core capability 8
    import json

    import tifffile

    from openevos.acquisition.zstack import ZStackPlan
    from openevos.imaging.zstack_io import save_zstack_set

    z = sim.controller.focus_z()
    zs = sim.capture.run_zstack(ZStackPlan(channels=("cube1", "cube2"), role=sim.settings.active_camera,
                                           bottom_um=z - 2, top_um=z + 2, step_um=1.0, centre_um=z))
    folder = save_zstack_set(tmp_path, zs, projections=("max",))
    rec = json.loads((folder / "zstack.json").read_text("utf-8"))
    assert len(rec["run_id"]) == 36 and rec["software"]["name"] == "OpenEVOS" and len(rec["plan_sha256"]) == 64
    assert rec["focus"]["cube1"]["sharpest_slice"] >= 1 and len(rec["focus"]["cube1"]["values"]) == 5
    with tifffile.TiffFile(folder / "stack.ome.tif") as t:
        ome = t.ome_metadata
    assert ome.count("ExposureTime=") == 10 and ome.count("DeltaT=") == 10 and ome.count("PositionZ=") == 10


def test_page_notes_undersampling(app, window):  # core capability 2
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(60)
    pg.above.setValue(60)
    opt = pg._optimal()
    pg.step.setValue(round(opt * 1.5, 2))  # still 3 slices inside ±60 µm
    pump(app, 0.1)
    assert len(pg.zview.positions) == 3 and "coarser than Nyquist" in pg.lbl_note.text()
    pg.step.setValue(round(opt, 2))
    pump(app, 0.1)
    assert "coarser than Nyquist" not in pg.lbl_note.text()


def test_zview_slices_turn_green_as_they_complete(app):
    from openevos.ui.pages.zstack import ZRangeView

    v = ZRangeView()
    v.set_state([0.0, 1.0, 2.0, 3.0], 1.5, None, None)
    assert [v.slice_state(i) for i in range(4)] == ["planned"] * 4
    v.set_run(2, 2, complete=1)  # slice 0 has every channel, slice 1 one so far, slice 2 being taken
    assert [v.slice_state(i) for i in range(4)] == ["done", "taken", "current", "pending"]
    v.set_run(None)
    v.set_finished(4)  # the whole stack: every plate green
    assert [v.slice_state(i) for i in range(4)] == ["done"] * 4 and v.all_done()
    v.set_state([0.0, 1.0, 2.0, 3.0], 1.5, None, None)  # same plan redrawn: stays green
    assert v.all_done()
    v.set_state([0.0, 2.0, 4.0], 2.0, None, None)  # a new plan: back to planned
    assert not v.all_done() and v.slice_state(0) == "planned"
    v.set_finished(2)  # stopped after two slices: only those are green
    assert [v.slice_state(i) for i in range(3)] == ["done", "done", "pending"] and not v.all_done()


def test_page_stack_and_seek_bar_turn_green_on_completion(app, window):
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(4)
    pg.above.setValue(4)
    pg.step.setValue(1)
    greens = []
    orig = pg._on_progress

    def spy(name, i, n, msg):
        orig(name, i, n, msg)
        greens.append(sum(pg.zview.slice_state(k) == "done" for k in range(len(pg.zview.positions))))

    pg._on_progress = spy
    window.model.tasks.progress.disconnect(orig)
    window.model.tasks.progress.connect(spy)
    assert not pg.slice.property("complete")
    assert pg.acquire()
    pump(app, 0.2)
    assert not pg.slice.property("complete")  # not green while running
    wait_idle(app, window)
    zs = window.session.capture.buffers.zstack_set
    assert zs.complete
    assert greens == sorted(greens) and greens[-1] < len(pg.zview.positions)  # filled in one by one
    assert pg.zview.all_done() and pg.slice.property("complete")  # the whole stack, then the seek bar
    assert "green" in pg.slice.toolTip().lower() or "complete" in pg.slice.toolTip().lower()
    pg.below.setValue(6)  # a new plan: back to the plain colours
    pump(app, 0.1)
    assert not pg.zview.all_done() and not pg.slice.property("complete")


def test_page_seek_bar_stays_plain_when_stopped(app, window):
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(0.5)
    assert pg.acquire()
    pump(app, 0.4)
    pg._acquire_or_stop()
    wait_idle(app, window)
    assert window.session.capture.buffers.zstack_set.status == "stopped"
    assert not pg.zview.all_done() and not pg.slice.property("complete")


def test_page_floating_focus_sets_top_and_bottom(app, window):
    pg = window.pages["zstack"]
    ctl = window.session.controller
    assert not pg.focus_float.isVisible() and pg.btn_focus_tab.isVisible()
    tab = pg.btn_focus_tab.geometry()
    assert tab.right() >= pg.width() - 40  # docked on the right edge
    pg.btn_focus_tab.click()
    pump(app, 0.3)
    f = pg.focus_float
    assert f.isVisible() and f.geometry().right() <= pg.width() and f.geometry().left() > pg.width() // 2
    assert f.panel.readout.isVisible() and f.panel.pad.isVisible()  # the Capture page's Z focus control
    z0 = ctl.focus_z()
    f.panel.step_requested.emit(5.0, False)
    assert wait_for(app, lambda: abs(ctl.focus_z() - (z0 + 5)) < 0.05)
    f.btn_top.click()
    pump(app, 0.2)
    assert pg.mode.current() == "range" and pg.top_um == pytest.approx(z0 + 5, abs=0.05)
    f.panel.step_requested.emit(-8.0, False)
    assert wait_for(app, lambda: abs(ctl.focus_z() - (z0 - 3)) < 0.05)
    f.btn_bottom.click()
    pump(app, 0.2)
    assert pg.bottom_um == pytest.approx(z0 - 3, abs=0.05)
    assert pg.zview.positions[0] == pytest.approx(z0 - 3, abs=0.05)
    assert pg.zview.positions[-1] == pytest.approx(z0 + 5, abs=0.05)
    assert "3" in f.lbl_ends.text() and pg.btn.isEnabled()
    pump(app, 0.6)
    assert f.panel.z == pytest.approx(ctl.focus_z(), abs=0.05)  # the readout follows the drive
    pg.btn_focus_tab.click()
    pump(app, 0.1)
    assert not f.isVisible()


def test_page_floating_focus_is_held_while_a_stack_runs(app, window):
    pg = window.pages["zstack"]
    pg.mode.buttons["centre"].click()
    pg.below.setValue(10)
    pg.above.setValue(10)
    pg.step.setValue(0.5)
    pg.btn_focus_tab.click()
    pump(app, 0.2)
    assert pg.focus_float.isEnabled()
    assert pg.acquire()
    pump(app, 0.4)
    assert not pg.focus_float.isEnabled()
    pg._acquire_or_stop()
    wait_idle(app, window)
    assert pg.focus_float.isEnabled()
