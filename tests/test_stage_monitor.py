"""Stage drift monitor (ADR-0040; Y-axis plan v3 §6.3): rules, latch, recovery, evidence. Pure logic, fake clock."""

from __future__ import annotations

import csv
import json

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import stage_monitor as sm

HOMED = tp.INIT_DONE_MARKER


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def status(x=0, y=0, *, x_init=HOMED, y_init=HOMED, x_isf=0xD000, y_isf=0xD000, x_srl=0xC400, y_srl=0xC400,
           x_cpos=None, y_cpos=None):
    return {"X": {"IINITONCEDONE": x_init, "ISTATUSFLAGS": x_isf, "SRL": x_srl, "APOS": x,
                  "CPOS": x if x_cpos is None else x_cpos},
            "Y": {"IINITONCEDONE": y_init, "ISTATUSFLAGS": y_isf, "SRL": y_srl, "APOS": y,
                  "CPOS": y if y_cpos is None else y_cpos}}


def settled(tmp_path=None, **kw):
    clk = Clock()
    m = sm.StageMonitor(tmp_path, clock=clk, **kw)
    m.status(status())
    clk.t += sm.SETTLE_S + 0.01
    m.fast(0, 0)  # references taken
    assert m.ref == {"X": 0, "Y": 0}
    return m, clk


def test_idle_drift_of_two_counts_is_tolerated_three_latches():
    m, clk = settled()
    for y in (1, -1, 2, -2, 0):  # dither and +/-2 stay inside the band
        clk.t += 0.05
        assert m.fast(0, y) is None
    clk.t += 0.05
    latch = m.fast(0, -3)
    assert latch is not None and latch.rule == "idle_encoder_drift" and latch.axis == "Y"
    assert "while the stage was at rest" in latch.message and m.latched
    assert "locked" in m.refusal() and "power-cycle" in m.refusal()


def test_the_still_axis_is_watched_while_the_other_moves_the_moving_one_is_not():
    m, clk = settled()
    m.begin_move({"X": 20000}, {"X": 0, "Y": 0})
    for x in range(0, 20000, 2000):  # X travels: not checked
        clk.t += 0.05
        assert m.fast(x, 0) is None
    clk.t += 0.05
    # 2026-11-19: a knock up to CROSS_AXIS_TOLERANCE_COUNTS while X moves is tracked (put back by the service)
    assert m.fast(20000, -3) is None and m.disturbed == {"Y": -3}
    clk.t += 0.05
    latch = m.fast(20000, -(sm.CROSS_AXIS_TOLERANCE_COUNTS + 1))
    assert latch.rule == "idle_encoder_drift" and "while X moved" in latch.message


def test_a_moved_axis_takes_its_new_reference_after_settling():
    m, clk = settled()
    m.begin_move({"X": 2268}, {"X": 0, "Y": 0})
    clk.t += 0.3
    m.fast(2268, 0)
    m.end_move(["X"])
    clk.t += 0.1
    assert m.fast(2265, 0) is None and m.ref["X"] is None  # settling: no reference yet
    clk.t += sm.SETTLE_S
    m.fast(2267, 0)
    assert m.ref["X"] == 2267
    clk.t += 0.05
    assert m.fast(2270, 0) is not None  # 3 counts from the settled reference


def test_a_still_axis_gets_its_reference_from_the_fresh_pre_move_read_at_once():
    clk = Clock()
    m = sm.StageMonitor(clock=clk)
    m.status(status())  # just homed: settling, no reference yet
    m.begin_move({"X": 2268}, {"X": 0, "Y": 0})
    assert m.ref["Y"] == 0
    clk.t += 0.05
    # checked even inside the settle time (beyond the tracked-knock bound)
    assert m.fast(500, -(sm.CROSS_AXIS_TOLERANCE_COUNTS + 1)).rule == "idle_encoder_drift"


def test_unhomed_axes_are_not_judged():
    clk = Clock()
    m = sm.StageMonitor(clock=clk)
    m.status(status(x_init=0, y_init=0, x_srl=0x4400))  # power-on homing: stage off, position meaningless
    clk.t += 1
    assert m.fast(-50000, 30000) is None and not m.latched


@pytest.mark.parametrize("kw, rule", [({"y_isf": 0x1001}, "overcurrent_trip"),
                                      ({"y_isf": 0xF000}, "oscillation_axis_off"),
                                      ({"x_isf": 0xD010}, "comm_error_abort"),
                                      ({"y_srl": 0x4400}, "power_stage_off")])
def test_drive_flags_latch(kw, rule):
    m, _clk = settled()
    latch = m.status(status(**kw))
    assert latch is not None and latch.rule == rule


def test_the_flags_gate_9_read_on_a_healthy_stage_do_not_latch():
    m, _clk = settled()
    assert m.status(status(x_isf=0xD000, y_isf=0xD000, x_srl=0xC400, y_srl=0xC400)) is None
    assert m.after_move("X", 0x1000, 0xC400) is None  # right after a move: bit 12 only, stage on


def test_command_mismatch_before_a_move_latches_and_warns_at_rest():
    m, _clk = settled()
    m.status(status(y=-8, y_cpos=0))
    assert m.warnings["Y"].startswith("Y is -8 counts from its command")
    assert not m.latched  # a warning only, until someone tries to move Y
    assert "drift -8" in m.pre_move("Y", -8, 0)
    assert m.latch.rule == "command_mismatch"
    assert m.pre_move("X", 1, 0) == ""


def test_the_latch_survives_a_restart_and_clears_only_when_x_and_y_re_home(tmp_path):
    m, clk = settled(tmp_path)
    m.fast(0, -9)
    assert (tmp_path / sm.LATCH_FILE).exists()
    m2 = sm.StageMonitor(tmp_path, clock=clk)  # the app restarted without a power cycle
    assert m2.latched and m2.latch.rule == "idle_encoder_drift"
    m2.status(status(y=0))
    assert m2.latched  # APOS back on CPOS does not clear it
    m2.status(status(x_init=0))  # only X re-initialises
    m2.status(status())
    assert m2.latched
    m2.status(status(x_init=0, y_init=0))  # both: power-on homing seen
    assert m2.latched  # not until both report homed again
    m2.status(status())
    assert not m2.latched and not (tmp_path / sm.LATCH_FILE).exists()
    assert list(tmp_path.glob("stage-latch-cleared-*.json"))


def test_a_homing_seen_before_the_latch_does_not_clear_it():
    m, clk = settled()
    m.status(status(x_init=0, y_init=0))
    m.status(status())
    clk.t += sm.SETTLE_S + 0.1
    m.fast(0, 0)
    m.fast(0, 5)
    assert m.latched
    m.status(status(y=5))
    assert m.latched


def test_an_unreadable_latch_file_fails_closed(tmp_path):
    (tmp_path / sm.LATCH_FILE).write_text("{not json", encoding="utf-8")
    m = sm.StageMonitor(tmp_path)
    assert m.latched and m.latch.rule == "latch_file_unreadable"


def test_evidence_is_written_ten_seconds_after_the_latch_in_the_correlate_format(tmp_path):
    m, clk = settled(tmp_path)
    m.begin_move({"X": 20000}, {"X": 0, "Y": 0})
    for i in range(40):
        clk.t += 0.05
        m.fast(i * 500, 0 if i < 30 else -14)  # beyond the tracked-knock bound (10)
    assert m.latched and not m.evidence_due()
    assert m.write_evidence() is None  # too early
    m.add_extra({"Y": {"ISTATUSFLAGS": 0xD000, "IIQMAX": 1200}})
    for _ in range(int(sm.POST_EVENT_S / 0.05) + 2):  # keeps logging after the event
        clk.t += 0.05
        m.fast(20000, -14)
    assert m.evidence_due()
    path = m.write_evidence()
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["latch"]["rule"] == "idle_encoder_drift" and rec["thresholds"]["idle_drift_max_counts"] == 2
    assert rec["extra_drive_reads"]["Y"]["IIQMAX"] == 1200
    with open(path.with_suffix(".csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(sm.CSV_COLUMNS)
    assert {"t", "x_apos", "y_apos", "y_cpos"} <= set(rows[0])  # what y_drift_correlate.py reads
    assert all(float(r["t"]) >= 0 for r in rows) and rows[-1]["y_apos"] == "-14" and rows[-1]["y_cpos"] == "0"
    assert any(r["x_cmd"] == "1" for r in rows)
    assert json.loads((tmp_path / sm.LATCH_FILE).read_text(encoding="utf-8"))["evidence"] == str(path)


def test_the_ring_buffer_keeps_forty_seconds():
    m, clk = settled()
    for _ in range(int(60 / 0.05)):
        clk.t += 0.05
        m.fast(0, 0)
    span = m.samples[-1][0] - m.samples[0][0]
    assert sm.RING_S - 0.1 <= span <= sm.RING_S


def test_record_mode_logs_every_sample(tmp_path):
    m, clk = settled(tmp_path, record=True)
    for i in range(10):
        clk.t += 0.05
        m.fast(i, 0)
    m.close()
    with open(m.record_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 11 and rows[-1]["x_apos"] == "9"


def test_nothing_is_written_without_an_evidence_folder():
    m, _clk = settled()
    m.fast(0, 9)
    m.write_evidence(force=True)
    assert m.latched and m.evidence_written == []
