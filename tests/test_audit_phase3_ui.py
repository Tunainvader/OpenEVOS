"""Audit Phase 3 items covered here: APP-13 (Z-stack channels follow the camera), APP-21 (focus nominal near home asks
first and can be undone)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings, HotButtonAction


def pump(app, s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture(scope="module")
def win():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.show()
    pump(app, 0.4)
    yield app, w, s
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def test_zstack_channels_follow_the_active_camera(win):
    app, w, _s = win
    zs = next(p for p in w.pages.values() if type(p).__name__ == "ZStackPage")
    mono = {zs.channel.itemData(i) for i in range(zs.channel.count())}
    assert any(slot.startswith("cube") for slot in mono)
    w.pages["capture"].switch_camera(CameraRole.COLOR)
    pump(app, 0.3)
    colour = {zs.channel.itemData(i) for i in range(zs.channel.count())}
    assert colour and not any(slot.startswith("cube") for slot in colour)
    w.pages["capture"].switch_camera(CameraRole.MONO)
    pump(app, 0.3)


def test_focus_nominal_near_home_asks_and_can_be_undone(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _app, w, s = win
    cap = w.pages["capture"]
    old = s.vessel.focus_nominal_um
    s.controller.run(s.controller.move_focus(50.0))
    answers = [QMessageBox.No]
    monkeypatch.setattr(QMessageBox, "question", lambda *a: answers.pop(0))
    cap.run_action(HotButtonAction.CAPTURE_FOCUS_NOMINAL)
    assert s.vessel.focus_nominal_um == old  # declined: unchanged
    answers.append(QMessageBox.Yes)
    cap.run_action(HotButtonAction.CAPTURE_FOCUS_NOMINAL)
    assert abs(s.vessel.focus_nominal_um - 50.0) < 1
    cap.undo_focus_nominal()
    assert s.vessel.focus_nominal_um == old
