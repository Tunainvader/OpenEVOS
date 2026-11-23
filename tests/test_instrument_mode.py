"""Impeccable critique 2026-11-24 (P1): on the real instrument the header said the cameras and light were simulators
while every one of them was physical. The title, header, chip, footer and About now come from each device's own
backend. Simulated hardware; real devices are stood in for by reporting a PHYSICAL backend for them."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.core.models import BackendKind
from openevos.ui.instrument_mode import instrument_mode

#: what the real-instrument launch registers (MCP readout 2026-11-23): the profile's own ids, physical backends
REAL_LAUNCH = {"stage_device": "motor.stage", "focus_device": "motor.focus",
               "turret_device": "motor.objective_turret", "illumination_device": "led.board"}


@pytest.fixture
def sim(tmp_path):
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings_path=tmp_path / "ui_settings.json",
                          calibration_path=tmp_path / "calibration.json")
    yield s
    s.shutdown()


def _fake(sim, physical: set[str], *, profile_update=None, telemetry=()):
    """`sim`'s profile (with `profile_update`) where the devices in `physical` report a PHYSICAL backend."""
    profile = sim.profile.model_copy(update=profile_update or {})
    ids = [profile.stage_device, profile.focus_device, profile.turret_device, profile.illumination_device,
           *(c.device_id for c in profile.cameras)]
    snaps = {d: SimpleNamespace(backend_kind=BackendKind.PHYSICAL if d in physical else BackendKind.SIMULATED)
             for d in ids if d}
    return SimpleNamespace(profile=profile, core=SimpleNamespace(store=SimpleNamespace(get=snaps.get)),
                           telemetry=list(telemetry))


def test_the_real_instrument_is_called_real(sim):
    cams = {c.device_id for c in sim.profile.cameras}
    s = _fake(sim, set(REAL_LAUNCH.values()) | cams, profile_update=REAL_LAUNCH,
              telemetry=[SimpleNamespace(motion=True, emulated=False, travel_box_note="calibrated envelope")])
    m = instrument_mode(s)
    assert m.badge == "REAL INSTRUMENT" and m.state == "warn" and m.footer == "REAL"
    assert "Simulated" not in m.name and m.name == "EVOS FL Auto"
    assert "simulator" not in m.tooltip and "synthetic" not in m.tooltip
    assert "real excitation light" in m.tooltip and "calibrated envelope" in m.tooltip
    assert m.about.startswith("Real instrument: X/Y stage, focus, objective turret, light, mono camera")
    assert not m.simulated


def test_the_old_physical_stage_only_mode_names_only_the_stage(sim):
    s = _fake(sim, {"motor.stage"}, profile_update={"stage_device": "motor.stage"},
              telemetry=[SimpleNamespace(motion=True, emulated=False)])
    m = instrument_mode(s)
    assert m.badge == "SIMULATION · REAL STAGE" and m.state == "warn" and m.footer == "MIXED"
    assert m.name.endswith("partly simulated")
    assert "X/Y stage: real" in m.tooltip and "Light: simulator" in m.tooltip and "synthetic frames" in m.tooltip


def test_one_simulated_part_on_a_real_instrument_is_named(sim):
    cams = {c.device_id for c in sim.profile.cameras}
    s = _fake(sim, {"motor.stage", "motor.focus", "motor.objective_turret"} | cams,
              profile_update={k: v for k, v in REAL_LAUNCH.items() if k != "illumination_device"})
    m = instrument_mode(s)
    assert m.badge == "REAL INSTRUMENT · SIM LIGHT"
    assert m.about == ("Real: X/Y stage, focus, objective turret, mono camera, colour camera. Simulated: light.")


def test_both_cameras_simulated_read_as_cameras(sim):
    s = _fake(sim, {"motor.stage", "motor.focus", "motor.objective_turret", "led.board"},
              profile_update=REAL_LAUNCH)
    assert instrument_mode(s).badge == "REAL INSTRUMENT · SIM CAMERAS"


def test_simulation_emulated_drives_and_read_only_telemetry(sim):
    m = instrument_mode(sim)
    assert m.badge == "SIMULATION" and m.state == "simulated" and m.footer == "SIM"
    assert m.name == "Simulated EVOS FL Auto" and "no physical hardware" in m.about
    emu = _fake(sim, set(), telemetry=[SimpleNamespace(motion=True, emulated=True)])
    assert instrument_mode(emu).badge == "SIMULATION + EMULATED DRIVES"
    ro = _fake(sim, set(), telemetry=[SimpleNamespace(motion=False, emulated=False)])
    m = instrument_mode(ro)
    assert m.badge == "SIMULATION + MOTOR TELEMETRY" and "display only" in m.tooltip


def test_a_device_missing_from_the_store_is_not_called_real(sim):
    s = _fake(sim, set(), profile_update={"illumination_device": "led.nowhere"})
    s.core.store.get = lambda d, _g=s.core.store.get: None if d == "led.nowhere" else _g(d)
    m = instrument_mode(s)
    assert "Light: not registered" in m.tooltip and m.badge == "SIMULATION"


def test_the_window_title_header_chip_footer_and_about_agree(sim, monkeypatch):
    """The window takes every line from the mode; here every camera reports a physical backend."""
    from PySide6.QtWidgets import QApplication

    from openevos.ui import dialogs, shell
    from openevos.ui.session_model import SessionModel

    app = QApplication.instance() or QApplication([])
    real_get = sim.core.store.get
    cams = {c.device_id for c in sim.profile.cameras}

    def get(did):
        snap = real_get(did)
        return snap.model_copy(update={"backend_kind": BackendKind.PHYSICAL}) if snap and did in cams else snap

    monkeypatch.setattr(sim.core.store, "get", get)
    shown = []

    class Dialog:  # the About box's text, without a modal
        def __init__(self, _title, text, *_a, **_k):
            shown.append(text.splitlines()[2])

        def exec(self):
            return 0

    monkeypatch.setattr(dialogs, "InfoDialog", Dialog)
    w = shell.MainWindow(SessionModel(sim))
    try:
        m = w.mode
        assert m.badge == "SIMULATION · REAL CAMERAS"
        assert w.windowTitle().endswith("EVOS FL Auto, partly simulated")
        assert w.lbl_profile.text().endswith("EVOS FL Auto, partly simulated")
        assert w.mode_badge.text == m.badge and w.mode_badge.toolTip() == m.tooltip
        w._tick()
        assert w.f_session.text().startswith("SESSION MIXED · ")
        w.show_about()
        assert shown == ["Real: mono camera, colour camera. Simulated: X/Y stage, focus, objective turret, light."]
    finally:
        w.close()
        w.deleteLater()
        end = time.monotonic() + 0.1
        while time.monotonic() < end:
            app.processEvents()
