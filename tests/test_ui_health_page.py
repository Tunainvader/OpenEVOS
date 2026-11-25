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

    _app, w, _s = win
    page = w.pages["health"]
    buttons = [b for b in page.findChildren(QAbstractButton) if b.objectName() != "qt_tableview_cornerbutton"]
    assert buttons == []  # nothing to press: no acknowledge, release or command here (Phase 0)


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
