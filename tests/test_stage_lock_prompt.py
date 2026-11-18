"""A locked stage offers its release from the UI (operator, 2026-11-18): clicking the Stage pill, and once per lock
when a stage move is refused by it. Simulated drive chain; the dialogs are stubbed."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings


def pump(app, s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def win():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    w = MainWindow(SessionModel(s))
    w.show()
    pump(app, 0.5)
    yield app, w, s
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def test_pill_click_and_refused_move_offer_the_release(win, monkeypatch):
    app, w, s = win
    calls = []
    monkeypatch.setattr(w, "release_stage_lock", lambda: calls.append("prompt"))
    pill = w.pills[s.profile.stage_device]
    pill.clicked.emit()
    assert calls == []  # not locked: nothing to offer
    svc = s.telemetry[0]
    y = svc.simulated_chain.values[("Y", "APOS")]
    svc.monitor.trip("idle_encoder_drift", "Y", "test", {"reference": y + 3, "apos": y})
    pump(app, 0.3)
    pill.clicked.emit()
    assert calls == ["prompt"]
    for _ in range(3):  # three refused jogs: one offer per lock
        s.controller.move_stage(1000.0, 0.0).wait(5)
        pump(app, 0.2)
    assert calls == ["prompt", "prompt"]


def test_unreleasable_lock_explains_instead_of_asking(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    app, w, s = win
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: pytest.fail("must not ask"))
    svc = s.telemetry[0]
    svc.monitor.trip("overcurrent_trip", "Y", "Y drive over-current trip recorded")
    pump(app, 0.2)
    w.release_stage_lock()
    assert shown and "cannot be released from the app" in shown[0] and "power-cycle" in shown[0]
