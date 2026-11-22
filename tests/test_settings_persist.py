"""Settings reach the file as they change, not only on a clean close (operator 2026-11-22: "the plate selection is
not saved on quitting, defaults to 96 well plate on start"). That evening a run ended without a clean close (no
"app closed" in its session log) and the settings file kept an older vessel. Simulated hardware, temporary files."""

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


def _session(tmp_path):
    return build_sim_session(ThreadClock(), fast=True, fps=30, settings_path=tmp_path / "ui_settings.json",
                             calibration_path=tmp_path / "calibration.json")


def test_a_vessel_choice_is_written_at_once_and_comes_back_at_the_next_start(tmp_path):
    s = _session(tmp_path)
    try:
        assert s.vessel.vessel_id == "plate-96"  # the default on a fresh install
        s.select_vessel("plate-24")
        # on disk before any close: a run that dies now still starts on the 24-well plate next time
        assert AppSettings.load(tmp_path / "ui_settings.json").last_vessel_id == "plate-24"
    finally:
        s.shutdown()  # a session shutdown is not a window close: nothing else saves here
    s2 = _session(tmp_path)
    try:
        assert s2.vessel.vessel_id == "plate-24"
    finally:
        s2.shutdown()


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _session(tmp_path)
    w = MainWindow(SessionModel(s))
    w.show()
    pump(app, 0.3)
    yield app, w, s, tmp_path / "ui_settings.json"
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def test_any_settings_change_is_autosaved_without_a_close(win):
    from openevos.ui import shell

    app, w, s, path = win
    s.settings.basic.show_scale_bar = not s.settings.basic.show_scale_bar
    wanted = s.settings.basic.show_scale_bar
    pump(app, shell.SETTINGS_AUTOSAVE_MS / 1000 + 0.6)
    assert AppSettings.load(path).basic.show_scale_bar is wanted
    before = path.stat().st_mtime_ns
    assert w.autosave_settings() is True
    assert path.stat().st_mtime_ns == before  # unchanged settings: nothing written


def test_close_saves_before_the_hardware_shutdown(win, monkeypatch):
    _app, w, s, path = win
    s.settings.basic.show_histogram = not s.settings.basic.show_histogram
    wanted = s.settings.basic.show_histogram
    seen = []
    real = w.model.shutdown

    def shutdown():  # what a crash or hang in the shutdown would leave on disk
        seen.append(AppSettings.load(path).basic.show_histogram if path.exists() else None)
        real()

    monkeypatch.setattr(w.model, "shutdown", shutdown)
    w.close()
    assert seen == [wanted]
