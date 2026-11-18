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
