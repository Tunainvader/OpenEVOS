"""Power-up record (ADR-0040 §9): index-to-hard-stop distance and holding current, read-only, once per homing."""

from __future__ import annotations

import time

from test_maintenance_motor_gate7_xy_limits import PORT
from test_maintenance_motor_gate8_t1_runs import FakeChain

from openevos.backends.technosoft import power_up_record as pur
from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.stage_monitor import StageMonitor
from openevos.backends.technosoft.transport import SimulatedDriveChain
from openevos.maintenance import motor_gate8_t1_runs as t1
from openevos.maintenance import motor_power_up_record as cli


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_new_reads_are_the_frames_gate8_rev6_read_on_the_unit():
    for ax in ("X", "Y"):
        assert tp.VARS[ax]["HOMEDONE"].frame == t1.HOME_READS[ax]["HOMEDONE"].frame
        assert tp.VARS[ax]["LHOMEINDEXDELTA"].frame == t1.HOME_READS[ax]["LHOMEINDEXDELTA"].frame
        assert tp.VARS[ax]["LHOMEINDEXDELTA"].bits == 32 and tp.VARS[ax]["HOMEDONE"].bits == 16


def _read(values):
    return lambda ax, var: values.get((ax, var), 0)


def test_record_and_index_jump(tmp_path):
    v = {("X", "LHOMEINDEXDELTA"): 2184, ("Y", "LHOMEINDEXDELTA"): 728, ("X", "IQ"): 100, ("Y", "IQ"): 0xFE70}
    row = pur.take(_read(v))
    assert row["y_iq_mean"] == -400 and row["x_iq_mean"] == 100  # IQ is signed
    first = pur.append(tmp_path, row, "test", "app start")
    assert not any("changed" in n for n in first) and "Y 4.0x X" in first[-1]
    v[("Y", "LHOMEINDEXDELTA")] = 728 + 11
    notes = pur.append(tmp_path, pur.take(_read(v)), "test", "re-home observed")
    assert any(n.startswith("Y index-to-hard-stop distance changed +11") for n in notes)
    v[("X", "LHOMEINDEXDELTA")] = 2184 + 10  # within 10 counts: no note
    assert not any(n.startswith("X ") for n in pur.append(tmp_path, pur.take(_read(v)), "test", "x"))
    assert [r["trigger"] for r in pur.history(tmp_path)] == ["app start", "re-home observed", "x"]


def test_the_app_records_at_connect_and_after_each_re_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "POWER_UP_SETTLE_S", 0.0)
    chain = SimulatedDriveChain({("Y", "LHOMEINDEXDELTA"): 728, ("X", "LHOMEINDEXDELTA"): 2184})
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.02, monitor=StageMonitor(tmp_path))
    svc.start()
    try:
        assert _wait(lambda: len(pur.history(tmp_path)) == 1)
        time.sleep(0.2)
        assert len(pur.history(tmp_path)) == 1  # once per homing, not every poll
        chain.values[("Y", "IINITONCEDONE")] = 0  # a power cycle: Y re-homes...
        chain.values[("Y", "LHOMEINDEXDELTA")] = 760  # ...and its index is 32 counts further from the stop
        assert _wait(lambda: svc.monitor.homed["Y"] is False)
        chain.values[("Y", "IINITONCEDONE")] = tp.INIT_DONE_MARKER
        assert _wait(lambda: len(pur.history(tmp_path)) == 2)
        rows = pur.history(tmp_path)
        assert [r["trigger"] for r in rows] == ["app start", "re-home observed"] and rows[1]["source"] == "app"
        assert _wait(lambda: any("Y index-to-hard-stop" in w for w in svc.monitor.state()["warnings"]))
        assert not svc.monitor.latched  # a warning, never a lock
    finally:
        svc.stop()
    assert all(w == bytes([tp.SYNC_BYTE]) or w in tp.ALLOWED_REQUESTS for w in chain.writes)  # reads only


def test_a_simulated_stage_never_records(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "POWER_UP_SETTLE_S", 0.0)
    svc = tm.TelemetryService(lambda: SimulatedDriveChain(), "SIM", poll_interval_s=0.02, monitor=StageMonitor())
    svc.start()
    try:
        assert _wait(lambda: svc.monitor.status_seen)
        time.sleep(0.2)
    finally:
        svc.stop()
    assert not list(tmp_path.iterdir())


def test_cli_reads_only_and_appends(tmp_path):
    c = FakeChain()
    rc = cli.main(["--instrument-on-2min", "--folder", str(tmp_path)], find_ports=lambda: [PORT],
                  open_port=lambda d: c, monotonic=c.monotonic, sleep=c.sleep)
    assert rc == 0 and c.write_frames() == []
    row = pur.history(tmp_path)[0]
    assert row["source"] == "cli" and row["y_lhomeindexdelta"] == "1234" and row["y_srl"] == "0xC400"
    res, _ = cli.run(on_for_2_min=False, folder=tmp_path, find_ports=lambda: [PORT], open_port=lambda d: c)
    assert res.outcome == "refused"
