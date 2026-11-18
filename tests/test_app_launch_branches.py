"""Audit APP-15: the app's launch branches (one instance per instrument, stage-limits auto-load) without hardware."""

from __future__ import annotations

import pytest

from openevos.ui import app as appmod


class Stop(Exception):
    pass


@pytest.fixture
def stop_at_illumination(monkeypatch):
    """main() runs its launch steps and stops right before it would build the session."""
    def stop(*_a, **_k):
        raise Stop
    monkeypatch.setattr(appmod, "_resolve_illumination", stop)
    monkeypatch.setattr(appmod.sys, "platform", "win32")
    import openevos.maintenance.led_autostart as la

    monkeypatch.setattr(la, "autostart", lambda **kw: la.AutostartResult("absent", "x"))


def _run(args):
    try:
        return appmod.main(args)
    except Stop:
        return "stopped"


def test_limits_autoload_runs_only_with_motor_control_on_a_real_port(monkeypatch, tmp_path, stop_at_illumination):
    import openevos.maintenance.motor_autolimits as ma

    calls = []
    monkeypatch.setattr(ma, "autoload", lambda **kw: calls.append(kw) or ma.AutolimitsResult("already_loaded", "ok"))
    base = ["--scans-dir", str(tmp_path), "--settings", str(tmp_path / "s.json")]
    for extra, want in ((["--motor-telemetry", "COM9", "--motor-control"], 1),
                        (["--motor-telemetry", "COM9", "--motor-control", "--no-limits-autoload"], 0),
                        (["--motor-telemetry", "sim", "--motor-control"], 0),
                        (["--motor-telemetry", "COM9"], 0)):
        calls.clear()
        assert _run([*base, *extra]) == "stopped"
        assert len(calls) == want, extra


def test_second_instance_on_real_hardware_is_refused(monkeypatch, tmp_path, stop_at_illumination):
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(a[2]))
    settings = tmp_path / "s.json"
    held = QLockFile(str(settings.with_name("openevos-instrument.lock")))
    assert held.tryLock(100)  # the first app
    try:
        assert _run(["--settings", str(settings), "--illumination", "real", "--no-led-autostart"]) == 1
        assert shown and "already running" in shown[0]
    finally:
        held.unlock()
    shown.clear()
    assert _run(["--settings", str(settings), "--illumination", "real", "--no-led-autostart"]) == "stopped"
    assert not shown
