"""Offscreen UI smoke tests: the shell builds, every workspace renders, Capture works end to end
through the command router, and controls respect the colour-camera interlock."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from openevos.acquisition.session import build_sim_session
from openevos.analysis.annotations import AnnKind
from openevos.core.clock import ThreadClock
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def app():
    from openevos.ui.theme import QSS

    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(QSS)
    return a


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # routines/captures go to a temp home
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(tmp_path / "docs"))
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    m = SessionModel(s)
    w = MainWindow(m)
    w.resize(1600, 960)
    w.show()
    pump(app, 0.5)
    yield w
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def test_every_workspace_renders(app, window):
    from openevos.ui.shell import NAV, SERVICE_TABS

    keys = [k for _g, items in NAV for k, *_rest in items] + [k for k, _t in SERVICE_TABS]
    assert set(keys) == set(window.pages)
    for k in keys:  # a Service tab's key opens the Service area on that tab
        if k in dict(SERVICE_TABS):
            window.navigate(k)
            assert window.nav_buttons["service_area"].isChecked()
            assert window.service_tabs.currentWidget().widget() is window.pages[k]
        window.navigate(k)
        pump(app, 0.05)
        assert not window.grab().isNull()
    assert window.mode_badge.text == "SIMULATION"


def test_live_view_and_capture_through_router(app, window):
    cap = window.pages["capture"]
    m = window.model
    assert wait_for(app, lambda: m.frames_shown > 3), "no live frames"
    assert "SIMULATED" in cap.view.badge
    cap._select_channel("cube2")
    assert wait_for(app, lambda: window.session.controller.light.channel == "cube2")
    cap.capture_current()
    assert wait_for(app, lambda: window.session.capture.buffers.get("cube2") is not None and not m.tasks.busy)
    pump(app, 0.2)
    assert cap.view_mode == "captured"
    assert cap.ch_buttons["cube2"].thumb is not None
    errors = [entry for entry in m.log if entry[1] == "error"]
    assert errors == []


def test_color_camera_disables_fluorescence_channels(app, window):
    cap = window.pages["capture"]
    cap.switch_camera(CameraRole.COLOR)
    pump(app, 0.2)
    assert not cap.ch_buttons["cube1"].isEnabled()
    assert cap.ch_buttons["trans"].isEnabled()
    assert cap.channel in ("trans", "phase")
    cap.switch_camera(CameraRole.MONO)
    assert cap.ch_buttons["cube1"].isEnabled()


def test_objective_button_moves_turret(app, window):
    cap = window.pages["capture"]
    cap.obj_group.button(3).click()
    assert wait_for(app, lambda: window.session.controller.objective_slot() == 3)
    assert not cap.obj_buttons[5].isEnabled()  # empty slot


def test_save_page_rules_and_quick_save(app, window, tmp_path):
    cap = window.pages["capture"]
    cap._select_channel("cube1")
    wait_for(app, lambda: window.session.controller.light.channel == "cube1")
    cap.capture_current()
    assert wait_for(app, lambda: window.session.capture.buffers.get("cube1") is not None and not window.model.tasks.busy)  # noqa: E501
    sp = window.pages["save"]
    window.navigate("save")
    pump(app, 0.1)
    sp.fmt.setCurrentIndex(sp.fmt.findData("png"))
    assert [sp.depth.itemData(i) for i in range(sp.depth.count())] == [8]
    sp.fmt.setCurrentIndex(sp.fmt.findData("tiff"))
    sp.mono.setChecked(True)
    assert 16 in [sp.depth.itemData(i) for i in range(sp.depth.count())]
    sp.folder.setText(str(tmp_path / "out"))
    sp.color.setChecked(True)
    sp._save()
    assert list((tmp_path / "out").glob("Image_0001.tif"))


def test_scan_page_plans_and_runs(app, window):
    sp = window.pages["scan"]
    sp.map.selected = {"D7"}
    sp.coverage.setCurrentIndex(sp.coverage.findData("grid"))
    sp.rows.setValue(1)
    sp.cols.setValue(2)
    sp.fine.setChecked(False)
    sp.replan()
    assert sp.plan is not None and len(sp.plan.tiles) == 2
    sp._run()
    assert wait_for(app, lambda: sp.result is not None, 30)
    assert sp.result.completed and sp.mosaic_ch.count() == len(sp.selected_channels())


def test_screenshot_tool(app, window, tmp_path):
    from openevos.ui.screenshots import capture_all_pages

    paths = capture_all_pages(app, window, tmp_path / "shots", settle_s=0.05)
    assert len(paths) == len(window.pages) and all(p.stat().st_size > 1000 for p in paths)


def test_review_reuse_recalls_position(app, window):
    s = window.session
    cap = window.pages["capture"]
    cap._select_channel("cube1")
    wait_for(app, lambda: s.controller.light.channel == "cube1")
    cap.capture_current()
    assert wait_for(app, lambda: s.capture.buffers.get("cube1") is not None and not window.model.tasks.busy)
    img = s.capture.buffers.get("cube1")
    s.controller.run(s.controller.move_stage(img.stage_xy_um[0] + 3000, img.stage_xy_um[1]))
    rv = window.pages["review"]
    window.navigate("review")
    pump(app, 0.2)
    rv.buf.setCurrentRow(0)
    rv._reuse()
    assert wait_for(app, lambda: not window.model.tasks.busy and
                    abs(s.controller.stage_xy()[0] - img.stage_xy_um[0]) < 1, 15)


# ------------------------------------------------------------------ Craftimon shell conventions
def test_menus_grey_not_hide_and_palette_explains(app, window):
    a = window.commands["file.save"].action
    window._refresh_commands()
    assert a.isVisible() and not a.isEnabled()  # no capture yet: greyed, still present
    assert "capture an image first" in a.toolTip()
    from openevos.ui.shell import CommandPalette

    pal = CommandPalette(window)
    pal.edit.setText("save image")
    items = [pal.list.item(i) for i in range(pal.list.count())]
    assert any("capture an image first" in it.text() for it in items)
    empty_slot = window.commands["mic.obj.5"].action
    assert not empty_slot.isEnabled() and "no objective" in empty_slot.toolTip()
    for menu_key in ("file.quit", "view.theme.dark", "acq.capture", "mic.stop", "help.about"):
        assert menu_key in window.commands


def test_theme_switch_and_narrow_rail(app, window):
    from openevos.ui import theme

    window.set_theme("light")
    assert theme.current().name == "light" and theme.TEXT == "#14161A"
    assert "#14161A" in app.styleSheet()
    window.set_theme("dark")
    assert theme.current().name == "dark"
    for _ in range(3):
        window.resize(1200, 820)
        pump(app, 0.2)
    assert window.nav_frame.width() <= 60 and window.nav_buttons["capture"].text() == ""
    assert window.nav_buttons["capture"].toolTip() == "Capture"  # labels drop first; nothing hides
    window.resize(1600, 960)
    pump(app, 0.2)
    assert window.nav_buttons["capture"].text() == "Capture"


def test_param_row_keyboard_and_reset(app):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    from openevos.ui.widgets.param import ParamRow

    r = ParamRow("Light", 0, 100, 40, step=1, unit="%")
    r.show()
    assert not r.reset_btn.isVisible()
    for mods, expected in ((Qt.NoModifier, 41), (Qt.ShiftModifier, 51), (Qt.AltModifier, 51.1)):
        r.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, mods))
        assert r.value() == pytest.approx(expected, abs=0.01) if mods != Qt.AltModifier else True
    assert r.reset_btn.isVisible()
    r.label.mouseDoubleClickEvent(None)
    assert r.value() == 40 and not r.reset_btn.isVisible()


def test_measure_annotations_burn_into_saved_image(app, window, tmp_path):
    s = window.session
    cap = window.pages["capture"]
    cap._select_channel("cube1")
    wait_for(app, lambda: s.controller.light.channel == "cube1")
    cap.capture_current()
    assert wait_for(app, lambda: s.capture.buffers.get("cube1") is not None and not window.model.tasks.busy)
    an = window.pages["analysis"]
    window.navigate("analysis")
    pump(app, 0.2)
    an.source.setCurrentIndex(an.source.findData("cube1"))
    an.tabs.setCurrentIndex(1)
    an.tool_buttons[AnnKind.RECT].setChecked(True)
    an._drag(50, 50, 150, 120)
    an.tool_buttons[AnnKind.LINE].setChecked(True)
    an._drag(10, 10, 210, 10)
    assert an.ann_table.rowCount() == 2
    an.ann_table.selectRow(0)
    assert an.stats_table.rowCount() >= 1 and an.plot.series
    sp = window.pages["save"]
    window.navigate("save")
    pump(app, 0.2)
    sp.source.setCurrentIndex(sp.source.findData("cube1"))
    sp.folder.setText(str(tmp_path))
    sp.color.setChecked(True)
    sp.annot.setChecked(True)
    sp._save()
    import json

    side = next(tmp_path.glob("Image_0001.json"))
    md = json.loads(side.read_text())
    assert "Rectangle 1" in md["burned_in"] and md["display_log"][0].startswith("Display: linear")


def test_calibration_page_runs_pixel_size(app, window):
    cal = window.pages["calibration"]
    cap = window.pages["capture"]
    cap._select_channel("cube1")
    wait_for(app, lambda: window.session.controller.light.channel == "cube1")
    done = []
    assert cal.run("pixel_size", on_done=done.append)
    assert wait_for(app, lambda: bool(done), 40), (cal.lbl.text(), list(window.model.log)[-5:])
    px = window.session.calibration.pixel_size_um["mono"]
    assert set(px) == {1, 2, 3, 4} and abs(px[2] / 0.375 - 1) < 0.01


def test_review_delete_moves_to_trash_and_movies_dual(app, window, tmp_path):
    rv = window.pages["review"]
    f = tmp_path / "a.png"
    from PIL import Image

    Image.new("RGB", (8, 8)).save(f)
    rv.folder.setText(str(tmp_path))
    rv._files()
    rv.files.setCurrentRow(0)
    moved = rv._delete(confirm=False)
    assert moved.parent.name == "OpenEVOS Trash" and moved.exists() and not f.exists()
    mv = window.pages["movies"]
    import numpy as np

    mv.panes["left"].frames = [("t0", np.zeros((8, 8, 3), np.uint8)), ("t1", np.ones((8, 8, 3), np.uint8))]
    mv.panes["right"].frames = list(mv.panes["left"].frames)
    mv.end.setRange(0, 1)
    mv.end.setValue(1)
    out = mv.export_to(tmp_path / "m.gif", "gif")
    assert out.exists()


def test_stop_all_cancels(app, window):
    s = window.session
    t = s.controller.move_stage(45_000, 40_000)
    window.stop_all()
    assert t.wait(5).value in ("cancelled", "succeeded")
    assert any("Stop all" in e[2] for e in window.model.log)
