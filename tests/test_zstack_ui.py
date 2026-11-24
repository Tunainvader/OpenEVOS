"""Z-stack page offscreen on the simulator: start/stop/step plan, acquisition end to end with progress, Stop, the slice
map (extent, direction, current slice) and playback (play/pause, forward/backward, loop/bounce/once)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from openevos.acquisition.session import build_sim_session
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
    r = s.vessel.region("D7")
    s.controller.run(s.controller.move_stage(*s.stage_map.to_stage(r.cx, r.cy)))
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


def test_page_opens_centred_on_focus_and_states_the_plan(win):
    app, _w, s, zp = win
    z = s.controller.focus_z()
    assert zp.start.value() == pytest.approx(z - 10, abs=0.01) and zp.stop.value() == pytest.approx(z + 10, abs=0.01)
    plan = zp.lbl_plan.text()
    assert "20.00 µm range" in plan and "11 slices" in plan and "upward" in plan
    assert zp.map.planned and zp.map.direction == 1 and len(zp.map.planned) == 11
    zp.btn_swap.click()
    assert "downward" in zp.lbl_plan.text() and zp.map.direction == -1
    assert zp.map.planned[0] == pytest.approx(z + 10, abs=0.01)  # the map starts at the start, wherever it is
    zp.btn_swap.click()
    zp.step.setValue(0.05 if zp.step.minimum() <= 0.05 else 0.1)  # 201 slices over 20 µm: over the cap
    assert not zp.btn.isEnabled() and "200" in zp.lbl_warn.text() and zp.lbl_warn.isVisibleTo(zp)
    zp.step.setValue(2)
    lo, _hi = zp._limits()
    zp.start.setValue(lo)
    zp.stop.setValue(lo + 1)
    zp.start.setValue(lo + 5)
    assert zp.btn.isEnabled()
    zp._centre()
    assert zp.btn.isEnabled()
    _ = app


def test_zstack_runs_end_to_end_with_progress_and_map(win):
    app, _w, s, zp = win
    z0 = s.controller.focus_z()
    seen: list[tuple[int, int]] = []
    def s_prog(name: str, i: int, n: int, _m: str) -> None:
        if name == "Z-Stack":
            seen.append((i, n))

    zp.model.tasks.progress.connect(s_prog)
    try:
        st = _acquire(app, s, zp, z0 - 6, z0 + 6, 2)
    finally:
        zp.model.tasks.progress.disconnect(s_prog)
    assert st.complete and len(st.images) == 7 and st.z_um[0] == pytest.approx(z0 - 6)
    assert s.controller.focus_z() == pytest.approx(z0)
    assert (0, 7) in seen and (7, 7) in seen  # progress counted every slice
    assert zp.bar.value() == 7 and zp.bar.maximum() == 7 and "7/7" in zp.bar.text()
    assert zp._map_mode == "stack" and zp.map.taken == 7 and zp.map.current == 0
    assert "slice 1/7" in zp.lbl_slice.text()
    zp.slice.setValue(3)
    assert zp.map.current == 3
    zp.map.slice_clicked.emit(5)
    assert zp.slice.value() == 5


def test_downward_stack_is_shown_top_down(win):
    app, _w, s, zp = win
    z0 = s.controller.focus_z()
    st = _acquire(app, s, zp, z0 + 4, z0 - 4, 2)
    assert st.direction == -1 and st.z_um[0] == pytest.approx(z0 + 4)
    assert zp.map.direction == -1 and zp.map.planned[0] == pytest.approx(z0 + 4)
    # the map draws higher Z higher: the start (top) has a smaller y than the stop
    assert zp.map._y(zp.map.planned[0]) < zp.map._y(zp.map.planned[-1])


def test_stop_ends_the_stack_and_keeps_the_slices(win):
    app, _w, s, zp = win
    z0 = s.controller.focus_z()
    zp.start.setValue(z0 - 40)
    zp.stop.setValue(z0 + 40)
    zp.step.setValue(1)
    zp.btn.click()
    assert wait_for(app, lambda: zp._running and zp.bar.value() >= 2)
    assert zp.btn_cancel.isEnabled()
    assert zp._map_mode == "run" and len(zp.map.planned) == 81 and 0 < zp.map.taken < 81
    zp.btn_cancel.click()
    assert wait_for(app, lambda: not zp._running)
    st = s.capture.buffers.zstack
    assert not st.complete and 2 <= len(st.images) < 81 and st.planned == 81
    assert "stopped" in zp.bar.text()
    assert s.controller.focus_z() == pytest.approx(z0)
    assert not zp.btn_cancel.isEnabled() and zp.btn.isEnabled()


def test_playback_forward_backward_loop_bounce_once(win):
    app, _w, s, zp = win
    z0 = s.controller.focus_z()
    _acquire(app, s, zp, z0 - 4, z0 + 4, 2)  # 5 slices
    zp.slice.setValue(0)
    zp.direction.set_current("fwd")
    zp.mode.setCurrentIndex(zp.mode.findData("loop"))
    seq = []
    for _ in range(6):
        zp._tick()
        seq.append(zp.slice.value())
    assert seq == [1, 2, 3, 4, 0, 1]  # forward = start -> stop, then loops
    zp.direction.set_current("back")
    seq = []
    for _ in range(3):
        zp._tick()
        seq.append(zp.slice.value())
    assert seq == [0, 4, 3]  # backward = stop -> start
    zp.mode.setCurrentIndex(zp.mode.findData("bounce"))
    zp.slice.setValue(1)
    seq = []
    for _ in range(3):
        zp._tick()
        seq.append(zp.slice.value())
    assert seq == [0, 1, 2] and zp.direction.current() == "fwd"  # turned round at the start
    zp.mode.setCurrentIndex(zp.mode.findData("once"))
    zp.b_play.setChecked(True)
    assert zp.timer.isActive() and zp.b_play.text() == "Pause"
    zp.slice.setValue(4)
    zp._tick()
    assert not zp.b_play.isChecked() and not zp.timer.isActive() and zp.b_play.text() == "Play"
    # real timer: playing advances on its own and pause stops it
    zp.mode.setCurrentIndex(zp.mode.findData("loop"))
    zp.fps.setValue(30)
    zp.slice.setValue(0)
    zp.b_play.setChecked(True)
    assert wait_for(app, lambda: zp.slice.value() >= 2, 3)
    zp.b_play.setChecked(False)
    v = zp.slice.value()
    wait_for(app, lambda: False, 0.2)
    assert zp.slice.value() == v


def test_channel_follows_the_capture_page_until_picked(win):
    app, w, _s, zp = win
    cap = w.pages["capture"]
    w.navigate("capture")
    wait_for(app, lambda: False, 0.1)
    w.navigate("zstack")
    wait_for(app, lambda: False, 0.1)
    assert zp.channel.currentData() == cap.channel


def test_hot_button_dialog_states_the_plan(win):
    from openevos.ui.dialogs import ZStackDialog

    _app, w, _s, _zp = win
    d = ZStackDialog(4000.0, w)
    assert "20.0 µm range · 11 slices" in d.lbl_plan.text() and d.ok.isEnabled()
    d.step.setValue(0.1)
    assert "Cannot start" in d.lbl_plan.text() and not d.ok.isEnabled()
    d.deleteLater()
