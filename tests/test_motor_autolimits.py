"""Calibrated X/Y limits at launch (ADR-0056): Gate 7's own run(), dry run first, then the send with the launch
path's confirmations. No hardware: run() is replaced by a recorder."""

from __future__ import annotations

from types import SimpleNamespace

from openevos.maintenance import motor_autolimits as ma
from openevos.maintenance import motor_gate7_xy_limits as g7


def fake_run(outcomes):
    calls = []

    def run(**kw):
        calls.append(kw)
        out = outcomes[len(calls) - 1]
        return SimpleNamespace(outcome=out, message=f"{out} msg", port=SimpleNamespace(device="COM3"),
                               port_state_read_back=kw.get("send", False), to_json=lambda: "{}")
    return run, calls


def test_dry_run_then_the_reviewed_send(tmp_path, monkeypatch):
    monkeypatch.setattr(g7, "write_evidence", lambda res, d: tmp_path / f"motor-gate7-x-{res.outcome}.json")
    run, calls = fake_run(["dry_run", "pass"])
    r = ma.autoload(scans=tmp_path, run=run)
    assert r.outcome == "pass" and r.stage_free and r.evidence.endswith("-pass.json")
    assert calls[0]["send"] is False
    assert calls[1] == {"send": True, "confirm": "LOAD XY CALIBRATED LIMITS ON COM3", "on_for_2_min": True,
                        "stage_clear": True, "scans": tmp_path}


def test_already_loaded_and_refusals_write_nothing_more(tmp_path, monkeypatch):
    monkeypatch.setattr(g7, "write_evidence", lambda res, d: tmp_path / "e.json")
    run, calls = fake_run(["dry_run", "already_loaded"])
    assert ma.autoload(scans=tmp_path, run=run).stage_free
    run, calls = fake_run(["refused"])  # no port / no calibration: the send is never attempted
    r = ma.autoload(scans=tmp_path, run=run)
    assert not r.stage_free and len(calls) == 1
    run, calls = fake_run(["dry_run", "baseline_refused"])  # e.g. the stage was moved off home
    r = ma.autoload(scans=tmp_path, run=run)
    assert not r.stage_free and "not loaded" in r.message


def test_moved_stage_with_limits_already_loaded_counts_as_loaded(tmp_path, monkeypatch):
    """2026-11-18: Gate 7 refused 'not at home' although X/Y already held the calibrated limits (no power cycle since
    they were loaded); the launch line said 'not loaded'."""
    monkeypatch.setattr(g7, "write_evidence", lambda res, d: tmp_path / "e.json")
    (tmp_path / "cal.json").write_text("{}", "utf-8")
    monkeypatch.setattr(g7, "load_limits", lambda p: ({"X": (-4000, 266000), "Y": (-4000, 173000)}, []))

    def run(**kw):
        before = {"X": {"LPOSMIN": -4000, "LPOSMAX": 266000}, "Y": {"LPOSMIN": -4000, "LPOSMAX": 173000}}
        out = "dry_run" if not kw.get("send") else "baseline_refused"
        return SimpleNamespace(outcome=out, message="X not at home", port=SimpleNamespace(device="COM3"),
                               port_state_read_back=kw.get("send", False),
                               baseline={"calibration_file": "cal.json", "before": before})
    r = ma.autoload(scans=tmp_path, run=run)
    assert r.outcome == "already_loaded" and r.stage_free
    monkeypatch.setattr(g7, "load_limits", lambda p: ({"X": (-4000, 266001), "Y": (-4000, 173000)}, []))
    assert not ma.autoload(scans=tmp_path, run=run).stage_free  # power-up or other limits: still not loaded
