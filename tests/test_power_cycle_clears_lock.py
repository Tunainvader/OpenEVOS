"""A stage lock carried over from an earlier session clears when the launch finds the drives freshly powered up and
homed (operator 2026-11-18: power cycles happen with the app closed, and the lock used to survive them)."""

from __future__ import annotations

import json

from openevos.backends.technosoft.stage_monitor import LATCH_FILE, StageMonitor


def _carried(tmp_path) -> StageMonitor:
    (tmp_path / LATCH_FILE).write_text(json.dumps({"rule": "overcurrent_trip", "axis": "Y", "message": "Y tripped",
                                                   "at_utc": "2026-11-18T17:42:49Z"}), "utf-8")
    m = StageMonitor(tmp_path)
    assert m.latched and m.carried_over
    return m


def test_a_carried_over_lock_clears_on_launch_power_cycle_evidence(tmp_path):
    m = _carried(tmp_path)
    assert m.power_cycle_seen("drives on their power-up limits, X and Y at home")
    assert not m.latched and not (tmp_path / LATCH_FILE).exists()
    cleared = list(tmp_path.glob("stage-latch-cleared-*.json"))
    assert cleared and "power cycle found at launch" in json.loads(cleared[0].read_text("utf-8"))["cleared_by"]
    assert StageMonitor(tmp_path).latch is None  # the next launch starts unlocked


def test_a_lock_set_in_this_session_needs_the_live_rehome(tmp_path):
    m = StageMonitor(tmp_path)
    m._trip("overcurrent_trip", "Y", "Y tripped now", {})
    assert m.latched and not m.carried_over
    assert not m.power_cycle_seen("evidence") and m.latched


def test_the_factory_clears_only_with_evidence_and_never_for_the_simulator(tmp_path):
    from openevos.backends.technosoft.telemetry import create_telemetry

    _carried(tmp_path)
    svc = create_telemetry({"port": "sim", "monitor_dir": str(tmp_path), "power_cycled": "x"})
    assert svc.monitor.latched  # a simulated stage never unlocks the real one
    svc = create_telemetry({"port": "sim", "monitor_dir": str(tmp_path)})
    assert svc.monitor.latched


def test_drive_flags_lock_again_if_the_fault_survived(tmp_path):
    from openevos.backends.technosoft import protocol as tp

    m = _carried(tmp_path)
    m.power_cycle_seen("evidence")
    base = {"IINITONCEDONE": tp.INIT_DONE_MARKER, "ESTATEV": 0, "SRH": 831, "SRL": 50176, "APOS": 0, "CPOS": 0}
    m.status({"X": dict(base, ISTATUSFLAGS=53248), "Y": dict(base, ISTATUSFLAGS=0x1001)})
    assert m.latched and m.latch.rule == "overcurrent_trip"
