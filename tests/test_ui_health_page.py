"""The Health page on the simulator (ADR-0068): overview, capability list, one card per subsystem, recent events and
"Why is operation restricted?" from the real StageMonitor lock; read-only (no buttons). Emulated drives only."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.health.model import SUBSYSTEMS
from openevos.health.service import HealthService
from openevos.instrument.settings import AppSettings


def pump(app, s: float) -> None:
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    m = SessionModel(s)
    m.health = HealthService(s, journal_path=tmp_path / "health.sqlite3").start(sampler=False)
    w = MainWindow(m)
    w.release_stage_lock = lambda: None  # the shell's own lock prompt is modal; not under test here
    w.rehome_stage = lambda **_k: None
    w.show()
    pump(app, 0.5)
    yield app, w, s
    w.close()
    m.health.stop()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def test_health_page_on_the_simulator(win):
    app, w, s = win
    w.navigate("health")
    page = w.pages["health"]
    assert w.nav_buttons["health"].isChecked()
    page.service.sample()
    page.refresh()
    assert page.overall.state == "HEALTHY"
    assert set(page.cards) == {d.key for d in SUBSYSTEMS}
    assert page.cards["incubator"].chip.state == "NOT MONITORED"
    xy = page.cards["xy_stage"]
    assert xy.chip.state == "HEALTHY" and xy.prov.text() == "DEMO"  # simulator values are marked, never Measured
    assert "expected every 0.02 s" in xy.fresh.text() and "Position" in xy.metrics.text()
    assert page.cap_rows["xy_positioning"][0].state == "PERMITTED"
    assert "Nothing is restricted" in page.why.text()
    assert "Journal: ok" in page.journal.text() or "Journal: starting" in page.journal.text()

    svc = s.telemetry[0]
    y = svc.simulated_chain.values[("Y", "APOS")]
    svc.monitor.trip("idle_encoder_drift", "Y", "Y encoder changed -3 counts while the stage was at rest",
                     {"reference": y + 3, "apos": y})
    assert wait_for(app, lambda: s.core.store.get("motor.stage").attr("drift_lock") is True)
    s.controller.move_stage(1000.0, 0.0).wait(5)
    pump(app, 0.2)
    page.service.sample()
    page.refresh()
    assert page.overall.state == "RESTRICTED" and "■" in page.overall.text()  # shape + text, not colour alone
    assert page.cards["xy_stage"].chip.state == "RESTRICTED"
    assert page.cap_rows["xy_positioning"][0].state == "RESTRICTED"
    assert "[SafetyManager]" in page.cap_rows["xy_positioning"][2].text()
    assert page.cap_rows["mono_imaging"][0].state == "PERMITTED"
    why = page.why.toPlainText() if hasattr(page.why, "toPlainText") else page.why.text()
    assert "idle_encoder_drift" in why and "StageMonitor" in why and "Release stage lock" in why
    assert svc.monitor.refusal().split(";")[0] in why  # the monitor's own words, quoted once
    kinds = [page.events.item(r, 3).text() for r in range(page.events.rowCount())]
    assert "stage_monitor.latched" in kinds
    assert any(k in kinds for k in ("command.rejected", "command.failed"))
    assert not w.grab().isNull()


def test_health_page_is_read_only(win):
    from PySide6.QtWidgets import QAbstractButton

    _app, w, s = win
    page = w.pages["health"]
    buttons = [b.objectName() for b in page.findChildren(QAbstractButton)
               if b.objectName() != "qt_tableview_cornerbutton"]
    # nothing proposed, restricted or to acknowledge: the only button writes a diagnostic zip (Phase 1); the
    # Protection panel's buttons appear only when there is something to decide (Phase 2)
    assert buttons == ["health_export"]
    from openevos.backends.technosoft import protocol as tp

    reads = {v.frame for vs in tp.VARS.values() for v in vs.values()} | {bytes([0xFF])}
    chain = s.telemetry[0].simulated_chain
    n = len(chain.writes)
    page.service.sample()
    page.export_btn.click()
    assert wait_for(_app, lambda: page.export_btn.isEnabled() and "Written:" in page.export_note.text(), 20), \
        page.export_note.text()
    assert [f for f in list(chain.writes)[n:] if f not in reads] == []  # the export commanded nothing
    assert list(page.export_folder().glob("health-diagnostics-*.zip"))


def test_page_shows_a_rule_watch_and_its_incident(win):
    """Three failed light commands (the simulator's own fail-next hook) raise the error-burst rule's WATCH; the page
    lists it with its rule, limit and unconfirmed causes, and the incident table groups the failures."""
    app, w, s = win
    page = w.pages["health"]
    w.navigate("health")
    h = page.service
    h.sample()
    light = s.rig.light
    for _ in range(3):
        light.fail_next = "simulated LED driver fault"
        s.controller.set_light("trans", 10.0, True).wait(5)
        light.recover()
    pump(app, 0.2)
    h.sample()
    page.refresh()
    assert any(a.detector == "comm.error_burst" for a in h.latest.assessments)
    text = page.rules.text()
    assert "WATCH" in text and "comm.error_burst" in text and "not confirmed" in text
    assert "rules:" in page.detectors.text() and "running" in page.detectors.text()
    titles = [page.incidents.item(r, 4).text() for r in range(page.incidents.rowCount())]
    assert any("simulated LED driver fault" in t for t in titles), titles
    assert page.cards["illumination"].chip.state in ("WATCH", "FAULT")



def test_protection_panel_asks_confirms_and_lifts(win):
    """Phase 2 (ADR-0070): a filling data drive raises a WATCH; the page proposes restricting new saving runs; the
    operator confirms (the dialog is stubbed); the capability reads RESTRICTED by the SafetyManager; Lift asks and
    lifts. No click sends a device command."""
    from datetime import timedelta

    from PySide6.QtWidgets import QPushButton

    from openevos.backends.technosoft import protocol as tp
    from openevos.health.inject import SimulatorFaultInjector

    app, w, s = win
    w.navigate("health")
    page = w.pages["health"]
    h = page.service
    t = [h._now()]
    h._now = lambda: t[0]
    h.disk_period_s = 0.0
    SimulatorFaultInjector(s, h).disk_filling(20 * 10**9, 20e6)
    for _ in range(8):
        t[0] += timedelta(seconds=30)
        h.sample()
    page.refresh()
    names = {b.objectName() for b in page.findChildren(QPushButton)}
    assert {"health_confirm", "health_decline", "health_ack"} <= names
    asked = []
    page.ask = lambda title, text, choices: asked.append((title, text, choices)) or choices[0]
    reads = {v.frame for vs in tp.VARS.values() for v in vs.values()} | {bytes([0xFF])}
    chain = s.telemetry[0].simulated_chain
    n = len(chain.writes)
    next(b for b in page.findChildren(QPushButton) if b.objectName() == "health_confirm").click()
    pump(app, 0.1)
    assert asked and asked[0][2] == ["Restrict", "Cancel"] and "Stays available" in asked[0][1]
    assert page.cap_rows["data_saving"][0].state == "RESTRICTED"
    assert "[SafetyManager]" in page.cap_rows["data_saving"][2].text()
    assert "RESTRICTED" in page._protect_labels[next(iter(page._protect_labels))].text()
    lift = next(b for b in page.findChildren(QPushButton) if b.objectName() == "health_lift")
    lift.click()
    pump(app, 0.1)
    assert h.registry.open() == [] and "Lifted by the operator" in page.protect_note.text()
    assert [f for f in list(chain.writes)[n:] if f not in reads] == []  # nothing on the panel commanded a drive

def test_page_without_an_app_service_runs_its_own_memory_only_one():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    m = SessionModel(s)
    w = MainWindow(m)
    try:
        svc = w.pages["health"].service
        assert svc is m.health and svc.running and svc.journal.state == "off"
        assert wait_for(app, lambda: svc.samples > 0)
        w.pages["health"].refresh()
        # the simulator rig alone has no device link: Communications is NOT MONITORED, never green
        assert w.pages["health"].cards["communications"].chip.state == "NOT MONITORED"
    finally:
        w.close()
        s.shutdown()
        w.deleteLater()
        pump(app, 0.1)
    assert not svc.running  # the window's close stopped the service it owned
