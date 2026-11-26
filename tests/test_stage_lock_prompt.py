"""A locked stage offers its release from the UI (operator, 2026-11-18): clicking the Stage pill, and once per lock
when a stage move is refused by it. Simulated drive chain; the dialogs are stubbed."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.backends.technosoft import protocol as tp
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
    settings = AppSettings()
    settings.basic.stage_auto_recovery = False  # these are the operator's own flows; ADR-0067's automatic one is apart
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=settings,
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


def test_unreleasable_lock_offers_the_rehome_with_its_reason(win, monkeypatch):
    """ADR-0058: a lock the release cannot clear offers the software re-home, naming the lock; No sends nothing."""
    from PySide6.QtWidgets import QMessageBox

    from openevos.backends.technosoft import protocol as tp

    app, w, s = win
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), QMessageBox.StandardButton.No)[1])
    svc = s.telemetry[0]
    svc.monitor.trip("overcurrent_trip", "Y", "Y drive over-current trip recorded")
    pump(app, 0.2)
    n = len(svc.simulated_chain.writes)
    w.release_stage_lock()
    assert asked and "Y drive over-current trip recorded" in asked[0] and "cannot be released" in asked[0]
    assert "Re-home" in asked[0] and "lowered fully" not in asked[0]  # Z already at 0: nothing to lower
    pump(app, 0.3)
    assert not [f for f in svc.simulated_chain.writes[n:] if f in tp.XY_HOME_FRAMES.values()]
    assert svc.monitor.latched
    assert w._rehome_why() == ""



def test_the_release_menu_command_reaches_the_rehome_offer_for_a_drive_trip(win, monkeypatch):
    """2026-11-26 on the instrument: "release stage locks button does nothing". Microscope › Release stage lock (and
    Capture's lock notice, which runs the same command) was gated on the release's own blocker, so for a drive-trip
    lock the click only logged a line. It is available whenever the stage is locked and offers the Re-home."""
    from PySide6.QtWidgets import QMessageBox

    app, w, s = win
    assert w._release_menu_why() == "the stage is not locked"
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), QMessageBox.StandardButton.No)[1])
    svc = s.telemetry[0]
    svc.monitor.trip("overcurrent_trip", "Y", "Y drive over-current trip recorded")
    pump(app, 0.2)
    w._refresh_commands()
    assert w.commands["mic.release"].action.isEnabled()
    w.run_command("mic.release")
    assert asked and "Y drive over-current trip recorded" in asked[0] and "Re-home" in asked[0]
    assert svc.monitor.latched  # No: nothing sent, the lock stays

def test_rehome_from_the_ui_clears_the_lock_and_returns(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from openevos.backends.technosoft import protocol as tp

    app, w, s = win
    svc = s.telemetry[0]
    chain = svc.simulated_chain
    for ax, c in (("X", 20000), ("Y", 30000)):
        chain.values[(ax, "APOS")] = chain.values[(ax, "CPOS")] = c
    pump(app, 0.3)
    results = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: results.append(("ok", a[2])))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: results.append(("warn", a[2])))
    svc.monitor.trip("landing_error", "Y", "Y ended at 12, +12 counts from its target 0 (band 5)")
    pump(app, 0.2)
    n = len(chain.writes)
    w.rehome_stage()
    deadline = time.monotonic() + 20
    while not results and time.monotonic() < deadline:
        pump(app, 0.1)
    assert results and results[0][0] == "ok", results
    assert not svc.monitor.latched
    sent = [f for f in chain.writes[n:] if f in tp.XY_HOME_FRAMES.values()]
    assert sent == [tp.XY_HOME_FRAMES["X"], tp.XY_HOME_FRAMES["Y"]]
    assert (chain.values[("X", "APOS")], chain.values[("Y", "APOS")]) == (20000, 30000)
    assert w._rehome_why() == "the stage is not locked"


def _rehome_with_z_up(win, monkeypatch, *, focus_ok: bool):
    from PySide6.QtWidgets import QMessageBox

    from openevos.backends.technosoft import protocol as tp

    app, w, s = win
    svc = s.telemetry[0]
    chain = svc.simulated_chain
    for ax, c in (("X", 20000), ("Y", 30000), ("Z", 8000)):
        chain.values[(ax, "APOS")] = chain.values[(ax, "CPOS")] = c
    pump(app, 0.4)
    z_up = s.controller.focus_z()
    assert z_up and z_up > 100
    asked, results = [], []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.StandardButton.Yes)[1])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: results.append(("ok", a[2])))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: results.append(("warn", a[2])))
    if not focus_ok:
        from openevos.core.command_router import CommandRouter
        real = CommandRouter.submit

        def refuse_focus(self, cmd):  # the focus move fails: nothing may reach X or Y
            if getattr(cmd, "device_id", "") == s.profile.focus_device:
                cmd = cmd.model_copy(update={"z": 1e9})
            return real(self, cmd)
        monkeypatch.setattr(CommandRouter, "submit", refuse_focus)
    svc.monitor.trip("landing_error", "Y", "Y ended at 53, +53 counts from its target 0 (band 5)")
    pump(app, 0.2)
    n = len(chain.writes)
    w.rehome_stage()
    deadline = time.monotonic() + 25
    while not results and time.monotonic() < deadline:
        pump(app, 0.1)
    sent = [f for f in chain.writes[n:] if f in tp.XY_HOME_FRAMES.values()]
    return SimpleNamespace(app=app, s=s, svc=svc, chain=chain, z_up=z_up, asked=asked, results=results, sent=sent)


def test_rehome_lowers_z_itself_then_puts_it_back(win, monkeypatch):
    """Operator 2026-11-19 ("cant [you] do this auto"): Re-home was refused with Z up; now it lowers Z to 0 first,
    re-homes, returns X/Y and then Z."""
    r = _rehome_with_z_up(win, monkeypatch, focus_ok=True)
    assert "lowered fully" in r.asked[0]
    assert r.results and r.results[0][0] == "ok", r.results
    assert r.sent == [tp.XY_HOME_FRAMES["X"], tp.XY_HOME_FRAMES["Y"]]
    assert not r.svc.monitor.latched
    deadline = time.monotonic() + 10
    while abs((r.s.controller.focus_z() or 0) - r.z_up) > 1 and time.monotonic() < deadline:
        pump(r.app, 0.1)
    assert abs(r.s.controller.focus_z() - r.z_up) <= 1
    assert (r.chain.values[("X", "APOS")], r.chain.values[("Y", "APOS")]) == (20000, 30000)


def test_rehome_sends_nothing_to_xy_when_z_cannot_be_lowered(win, monkeypatch):
    r = _rehome_with_z_up(win, monkeypatch, focus_ok=False)
    assert r.results and r.results[0][0] == "warn" and "lowering the objective" in r.results[0][1], r.results
    assert r.sent == []
    assert r.svc.monitor.latched
