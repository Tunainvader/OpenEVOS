"""Always-on telemetry of every device (operator 2026-11-19): state changes with positions, commands and periodic
snapshots of all devices, to Logs/telemetry-YYYYMMDD.jsonl; reads only."""

from __future__ import annotations

import json
import time

from openevos.acquisition.session import build_sim_session
from openevos.acquisition.telemetry_log import TelemetryLog, describe
from openevos.core.clock import ThreadClock


def _rows(path):
    return [json.loads(x) for x in path.read_text("utf-8").splitlines()]


def test_records_states_positions_commands_and_snapshots(tmp_path):
    s = build_sim_session(ThreadClock(), fast=True)
    log = TelemetryLog(s.core.bus, s.core.store, tmp_path, snapshot_interval_s=0.1)
    try:
        ctl = s.controller
        ctl.run(ctl.move_stage(1000.0, 2000.0))
        ctl.run(ctl.move_focus(500.0))
        ctl.run(s.change_objective(2))
        time.sleep(0.35)
    finally:
        log.close()
        s.shutdown()
    rows = _rows(log.path)
    kinds = {r["kind"] for r in rows}
    assert {"session", "snapshot", "state", "command"} <= kinds
    snaps = [r for r in rows if r["kind"] == "snapshot"]
    assert len(snaps) >= 3  # at start, periodically, at close
    devices = {d["device"] for d in snaps[0]["devices"]}
    assert {s.profile.stage_device, s.profile.focus_device, s.profile.turret_device} <= devices
    stage = [r for r in rows if r["kind"] == "state" and r["device"] == s.profile.stage_device and "position" in r]
    assert stage and stage[-1]["position"] == {"x": 1000.0, "y": 2000.0}
    statuses = {r["status"] for r in rows if r["kind"] == "command"}
    assert "succeeded" in statuses
    turret = snaps[-1]["devices"][[d["device"] for d in snaps[-1]["devices"]].index(s.profile.turret_device)]
    assert list(turret["position"].values()) == [2.0]  # the turret's slot (axis name differs sim vs drive)


def test_position_only_updates_are_rate_limited(tmp_path):
    from openevos.core.event_bus import DeviceStateChanged
    from openevos.core.models import Position

    s = build_sim_session(ThreadClock(), fast=True)
    t = {"now": 0.0}
    log = TelemetryLog(s.core.bus, s.core.store, tmp_path, snapshot_interval_s=60, clock=lambda: t["now"])
    try:
        base = s.core.store.get(s.profile.stage_device)
        n0 = len(_rows(log.path))
        for i in range(10):  # 10 position-only updates inside 0.1 s
            t["now"] = i * 0.01
            cur = base.model_copy(update={"observed_position": Position(axes=("x", "y"), values=(float(i), 0.0),
                                                                        unit="um", frame="stage")})
            log._device(DeviceStateChanged(s.profile.stage_device, base, cur))
        assert len(_rows(log.path)) - n0 == 1
        t["now"] = 1.0
        log._device(DeviceStateChanged(s.profile.stage_device, base, base.model_copy(
            update={"observed_position": Position(axes=("x", "y"), values=(99.0, 0.0), unit="um", frame="stage")})))
        assert len(_rows(log.path)) - n0 == 2
    finally:
        log.close()
        s.shutdown()


def test_describe_keeps_small_attributes_only():
    s = build_sim_session(ThreadClock(), fast=True)
    try:
        snap = s.core.store.get(s.profile.stage_device).model_copy(
            update={"attributes": (("homed", True), ("big", tuple(range(50))), ("limits", ((0, 1), (2, 3))))})
        d = describe(snap)
        assert d["attributes"] == {"homed": True, "limits": ((0, 1), (2, 3))}
    finally:
        s.shutdown()
