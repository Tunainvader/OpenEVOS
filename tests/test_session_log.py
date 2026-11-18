"""Session fault log on disk: faults, refused/failed commands and device condition changes with their reasons."""

from __future__ import annotations

import json

from openevos.acquisition.session import build_sim_session
from openevos.acquisition.session_log import SessionLog
from openevos.core.clock import ThreadClock
from openevos.core.models import Health
from openevos.instrument.settings import AppSettings


def test_faults_commands_and_condition_changes_reach_the_file(tmp_path):
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    log = SessionLog(s.core.bus, tmp_path / "Logs")
    try:
        did = s.profile.camera(s.settings.active_camera).device_id
        cur = s.core.store.get(did)
        from openevos.core.event_bus import DeviceStateChanged

        bad = cur.model_copy(update={"health": Health.FAULT, "note": "frame retrieval failed: timeout"})
        s.core.bus.publish(DeviceStateChanged(device_id=did, previous=cur, current=bad,
                                              reason="camera stream failed"))
        # a refused command: a focus target far outside the range
        t = s.controller.move_focus(1e9)
        t.wait(5)
    finally:
        log.close()
        s.shutdown()
    rows = [json.loads(x) for x in log.path.read_text("utf-8").splitlines()]
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "session" and kinds[-1] == "session"
    dev = next(r for r in rows if r["kind"] == "device" and r["device"] == did)
    assert dev["changed"]["health"] == {"from": "normal", "to": "fault"} and dev["reason"] == "camera stream failed"
    assert "timeout" in dev["note"]
    assert any(r["kind"] == "command" and r["status"] in ("rejected", "failed") for r in rows)
