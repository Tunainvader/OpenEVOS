"""Motor Gate 8 (ADR-0028): planning, refusals and frames. Synthetic calibration values only."""

from __future__ import annotations

import pytest
from test_maintenance_motor_gate7_xy_limits import PORT, SYN, cal_file

from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate7_xy_limits as g7
from openevos.maintenance import motor_gate8_full_range as g8
from openevos.safety.firmware import REVIEWED_MAINTENANCE


def test_targets_stay_two_mm_inside_the_calibrated_max():
    t = g8.plan_targets(SYN)
    for ax in ("X", "Y"):
        assert t[ax] == SYN[ax][1] - round(2.0 * g7.COUNTS_PER_MM)
        assert 0 < t[ax] < SYN[ax][1]


def test_target_frames_are_bounded_and_checksummed():
    f = g8._w(1, 1000)
    assert f[:5] == bytes.fromhex("08 00 10 25 D4") and sum(f[:-1]) & 0xFF == f[-1]
    assert f[5:9] == g7.vendor32(1000)
    assert g8._w(1, 2268) == g4.M1_TARGET_OUT  # same frame as Gate 4's verified +1 mm target
    for bad in ((3, 1000), (1, -1), (2, 300_000)):
        with pytest.raises(g4.MotorGate4Refused):
            g8._w(*bad)
    assert REVIEWED_MAINTENANCE["motor.xy_full_range_traverse"]["adr"] == "ADR-0028"


def test_dry_run_opens_nothing(tmp_path):
    opened = []
    res = g8.run(calibration=cal_file(tmp_path), find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == [] and len(res.exchanges) == 2


def test_return_home_dry_run_and_phrase(tmp_path):
    res = g8.run(return_home=True, calibration=cal_file(tmp_path), find_ports=lambda: [PORT],
                 open_port=lambda d: 1 / 0)
    assert res.outcome == "dry_run" and [e.request_hex for e in res.exchanges] == [
        g8._w(1, 0).hex(" "), g8._w(2, 0).hex(" ")]
    assert g8._w(1, 0) == g4.M2_TARGET_HOME  # the verified Gate 4 return-leg frame
    res = g8.run(return_home=True, send=True, on_for_2_min=True, stage_empty_watching=True,
                 confirm="TRAVERSE XY FULL RANGE ON COM3", calibration=cal_file(tmp_path),
                 find_ports=lambda: [PORT], open_port=lambda d: 1 / 0)
    assert res.outcome == "refused" and "RETURN XY TO ZERO" in res.message


def test_stepped_y_dry_run_and_bounds(tmp_path):
    res = g8.run(axes="Y", y_target_mm=40.0, cycles=1, calibration=cal_file(tmp_path), find_ports=lambda: [PORT],
                 open_port=lambda d: 1 / 0)
    assert res.outcome == "dry_run" and list(res.baseline["targets_mm"]) == ["Y"]
    assert res.baseline["targets_mm"]["Y"] == pytest.approx(40.0, abs=0.001) and "Y to 40.0" in res.message
    assert [e.request_hex for e in res.exchanges] == [g8._w(2, round(40.0 * g7.COUNTS_PER_MM)).hex(" ")]
    too_far = g8.plan_targets(SYN)["Y"] / g7.COUNTS_PER_MM + 0.5
    for bad in (too_far, 1.0):
        res = g8.run(axes="Y", y_target_mm=bad, calibration=cal_file(tmp_path), find_ports=lambda: [PORT])
        assert res.outcome == "refused" and "Y target must be" in res.message
    res = g8.run(axes="Z", calibration=cal_file(tmp_path), find_ports=lambda: [PORT])
    assert res.outcome == "refused"


class _PeakLink:
    def __init__(self, hi, lo):
        self.v = {"IIQMAX": hi, "IIQMIN": lo}

    def read(self, r):
        return {"i16": self.v[r.var]}


def test_peak_current_is_recorded_and_flags_low_margin():
    res = g4.MotorGate4Result("x", "", "", "")
    res.legs["c1 Y home"] = {}
    assert g8._peaks(_PeakLink(79, -16244), res, "c1 Y home", "Y", 17117) == ["c1 Y home 16244/17117"]
    assert res.legs["c1 Y home"]["current_fraction"] == pytest.approx(0.949, abs=0.001)
    res.legs["c1 X out"] = {}
    assert g8._peaks(_PeakLink(1122, -4122), res, "c1 X out", "X", 17117) == []


def test_idle_axis_encoder_range_is_summarised():
    res = g4.MotorGate4Result("x", "", "", "")
    res.legs["c1 X out"] = {"trace": [{"Y_APOS": 0}, {"Y_APOS": -2}, {"Y_APOS": 1}, {"SRH": 1}]}
    g8._watch_summary(res, "c1 X out", "Y")
    assert res.legs["c1 X out"]["Y_APOS_range"] == [-2, 1]


def test_leg_samples_watched_reads_each_poll():
    from test_maintenance_motor_gate4_x_move import FakeX, go  # noqa: F401
    fake = FakeX(move_polls=3)
    res = g4.MotorGate4Result("x", "", "", "")
    link = g4._Link(fake, res, lambda: 0.0, lambda s: None,
                    allowed_reads=g4.ALLOWED_READS | {g4.XR["APOS"].frame})
    link.port.configure(115200)
    g4._leg(link, res, "out", "M1 CPOSCMD=2268", g4.M1_TARGET_OUT, g4.TARGET, 8, timeout_s=1.0,
            watch=(("W_APOS", g4.XR["APOS"]),))
    assert all("W_APOS" in p for p in res.legs["out"]["trace"])


def test_cross_axis_drift_raises_beyond_three_counts():
    res = g4.MotorGate4Result("x", "", "", "")
    res.legs["c3 X home"] = {"trace": [{"Y_APOS": -3}, {"Y_APOS": -3}]}
    g8._cross_drift(res, "c3 X home", "Y", 0)  # 3 counts: allowed
    res.legs["c3 X home"]["trace"].append({"Y_APOS": -8})
    with pytest.raises(g8._CrossDrift, match="cross_axis_encoder_drift"):
        g8._cross_drift(res, "c3 X home", "Y", 0)
    assert res.legs["c3 X home"]["Y_drift"] == -8


def test_refusals(tmp_path):
    res = g8.run(send=True, calibration=cal_file(tmp_path), find_ports=lambda: [PORT], open_port=lambda d: 1 / 0)
    assert res.outcome == "refused"
    bad = {"X": (-1000, 200000), "Y": SYN["Y"]}
    res = g8.run(calibration=cal_file(tmp_path, bad), find_ports=lambda: [PORT])
    assert res.outcome == "calibration_refused"
