"""System precheck (operator, 2026-11-18): read-only checks of every mapped device, the drift monitor, the live view
and the objective safe heights, with a fix for each problem. Simulated sessions only."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition import precheck as pc
from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.core.event_bus import CommandLifecycleEvent
from openevos.core.models import Connection, FirmwareMode, Health
from openevos.instrument.settings import AppSettings

MOTORS = {"technosoft": {"port": "sim", "motion": True}}


def _session(**kw):
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(), **kw)
    t0 = time.monotonic()
    while not pc.ready(s) and time.monotonic() - t0 < 8:
        time.sleep(0.05)
    return s


def _by_key(checks):
    return {c.key: c for c in checks}


def test_pure_simulator_reports_info_not_faults():
    s = _session()
    try:
        checks = pc.run_precheck(s)
        assert {c.status for c in checks if c.key.startswith("device.")} == {"info"}
        assert all("simulated" in c.detail for c in checks if c.key.startswith("device."))
        assert _by_key(checks)["safe_heights"].status == "warn"
        assert "precheck" in pc.summary(checks).lower()
    finally:
        s.shutdown()


def test_motor_chain_devices_monitor_stage_box_and_safe_heights():
    s = _session(telemetry=MOTORS)
    try:
        k = _by_key(pc.run_precheck(s))
        for did in ("motor.stage", "motor.focus", "motor.objective_turret", "motor.cube_carriage"):
            assert k[f"device.{did}"].status == "ok", k[f"device.{did}"]
        assert k["monitor"].status == "ok"
        assert "stage_limits" in k
        snap = s.core.store.get("motor.stage")
        for lim, want in ((((0, 50000), (0, 50000)), "fault"), (((-3996, 265640), (-3996, 172744)), "ok")):
            attrs = tuple((a, lim if a == "limits_counts" else v) for a, v in dict(snap.attributes).items())
            if "limits_counts" not in dict(snap.attributes):
                attrs += (("limits_counts", lim),)
            fake_snap = snap.model_copy(update={"attributes": attrs})
            fake = SimpleNamespace(physical_stage=True, profile=s.profile,
                                   core=SimpleNamespace(store=SimpleNamespace(get=lambda _d, fs=fake_snap: fs)))
            c = pc.stage_limits(fake)
            assert c.status == want, (lim, c)
            if want == "fault":
                assert "Gate 7" in c.fix and "home corner" in c.fix
        assert k["stage_box"].status == "info" and "centred" in k["stage_box"].fix  # homed corner 0, 0
        assert k["safe_heights"].status == "warn" and "0 of 5" in k["safe_heights"].detail
        for o in s.profile.objectives:
            s.settings.basic.focus_limit_um[str(o.slot)] = 1000.0
        k = _by_key(pc.run_precheck(s))
        assert k["safe_heights"].status == "ok" and "z_above" not in k
        slot = s.controller.objective_slot()
        s.settings.basic.focus_limit_um[str(slot)] = -1.0  # Z (0) is above it
        assert _by_key(pc.run_precheck(s))["z_above"].status == "warn"
    finally:
        s.shutdown()


def test_precheck_sends_nothing():
    s = _session(telemetry=MOTORS)
    seen = []
    sub = s.core.bus.subscribe(CommandLifecycleEvent, seen.append)
    chain = s.telemetry[0].simulated_chain
    try:
        n_writes = len(chain.writes)
        for _ in range(3):
            pc.run_precheck(s)
        assert seen == []
        motion = [w for w in chain.writes[n_writes:] if w not in __import__(
            "openevos.backends.technosoft.protocol", fromlist=["ALLOWED_REQUESTS"]).ALLOWED_REQUESTS]
        assert motion == []  # only the service's own polling reads
    finally:
        sub.unsubscribe()
        s.shutdown()


@pytest.mark.parametrize("update, status, words", [
    ({"firmware_mode": FirmwareMode.BOOTLOADER, "connection": Connection.DISCONNECTED}, "fault", "led_autostart"),
    ({"connection": Connection.DISCONNECTED, "note": "cable"}, "fault", "cable"),
    ({"recovery_required": True}, "fault", "Recover"),
    ({"health": Health.FAULT}, "fault", "Recover"),
    ({"health": Health.WARNING}, "warn", "Events"),
])
def test_device_faults_name_the_fix(update, status, words):
    s = _session()
    try:
        did = s.profile.illumination_device
        snap = s.core.store.get(did).model_copy(update=update)
        fake = SimpleNamespace(core=SimpleNamespace(store=SimpleNamespace(get=lambda _d: snap)), profile=s.profile)
        c = pc._device(fake, "Light (LED board)", did)
        assert c.status == status and words in (c.fix + c.detail)
    finally:
        s.shutdown()


def test_not_homed_is_a_fault():
    s = _session(telemetry=MOTORS)
    try:
        snap = s.core.store.get("motor.focus")
        attrs = tuple((k, False if k == "homed" else v) for k, v in dict(snap.attributes).items())
        bad = snap.model_copy(update={"attributes": attrs})
        fake = SimpleNamespace(core=SimpleNamespace(store=SimpleNamespace(get=lambda _d: bad)), profile=s.profile)
        c = pc._device(fake, "Z focus", "motor.focus")
        assert c.status == "fault" and "not homed" in c.detail
    finally:
        s.shutdown()


# ---------------------------------------------------------------------------------------- dialogs
def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from openevos.ui.theme import QSS

    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(QSS)
    return a


def test_start_precheck_opens_the_dialog_and_safe_heights_table(app, tmp_path):
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    s = _session(telemetry=MOTORS)
    w = MainWindow(SessionModel(s))
    try:
        w.show()
        w.start_precheck(timeout_s=3)
        t0 = time.monotonic()
        while getattr(w, "_precheck_dlg", None) is None and time.monotonic() - t0 < 10:
            pump(app, 0.1)
        dlg = w._precheck_dlg  # safe heights are missing: the dialog opens by itself
        assert dlg.isVisible() and "warning" in dlg.lbl_summary.text()
        assert any("Precheck" in e[2] for e in w.model.log)
        dlg.grab().save(str(tmp_path / "precheck.png"))
        h = w.open_safe_heights()
        pump(app, 0.5)
        assert set(h.rows) == {o.slot for o in s.profile.objectives if o.installed}
        slot = s.controller.objective_slot()
        val, go, setb, _clr = h.rows[slot]
        assert setb.isEnabled() and not go.isEnabled() and val.text() == "not set"
        other = next(x for x in h.rows if x != slot)
        assert h.rows[other][1].isEnabled() and not h.rows[other][2].isEnabled()
        setb.click()
        pump(app, 0.5)
        assert str(slot) in s.settings.basic.focus_limit_um and "µm" in val.text()
        h.rows[other][1].click()  # stage at 0, 0: refused before anything is sent
        pump(app, 0.2)
        assert "refused" in h.lbl_msg.text() and s.controller.objective_slot() == slot
        h.close()
        dlg.close()
    finally:
        w.close()
        s.shutdown()
        w.deleteLater()
        pump(app, 0.1)
