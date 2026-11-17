"""Motor Gate 7 (ADR-0022) against a fake drive chain. Synthetic calibration values only (not the real ones)."""

from __future__ import annotations

import json

import pytest
from test_maintenance_motor_gate4_x_move import FakeX

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate7_xy_limits as g7
from openevos.safety.firmware import REVIEWED_MAINTENANCE

PORT = mg1.PortInfo("COM3", "USB VID:PID=0403:6001 SER=FAKE", "FAKE", None)
CPM = g7.COUNTS_PER_MM
SYN = {"X": (-1000, -1000 + round(119.1 * CPM)), "Y": (-1500, -1500 + round(78.1 * CPM))}


def cal_file(tmp_path, lim=SYN, name="led-application-stepd-20990101T000000-parsed.json"):
    mb = {f"{ax}AxisPositionLimits.{k}": v for ax, (lo, hi) in lim.items() for k, v in (("lMin", lo), ("lMax", hi))}
    p = tmp_path / name
    p.write_text(json.dumps({"layout": "test", "motor_base": mb}), encoding="utf-8")
    return p


class FakeLim(FakeX):
    """Six drives at rest at home; X/Y limit writes update LPOSMIN/LPOSMAX (or are corrupted)."""

    def __init__(self, *, corrupt=None, start=None, **kw):
        super().__init__(**kw)
        for ax, aid in g7.AXES:
            for r in g7.READS[ax].values():
                self.by_frame[r.frame] = r
                self.v.setdefault((ax, r.var), {"SRH": 0x0200, "LCONVERGEBAND": 5, "LPOSMAX": 50000}.get(r.var, 0))
            self.v[(ax, "SRH")], self.v[(ax, "LCONVERGEBAND")] = 0x0200, 5
            self.v[(ax, "LPOSMIN")], self.v[(ax, "LPOSMAX")] = (start or {}).get(ax, (0, 50000))
        self.corrupt = corrupt
        self.limit_frames = {}
        for ax, aid in g7.AXES:
            for var in ("LPOSMIN", "LPOSMAX"):
                self.limit_frames[(aid, var)] = (ax, var)

    def write(self, data):
        data = bytes(data)
        if len(data) == 10 and data[3] == 0x25 and data[4] in (0xB4, 0xB6) and data[2] in (0x10, 0x20):
            self.writes.append(data)
            ax = "X" if data[2] == 0x10 else "Y"
            var = "LPOSMIN" if data[4] == 0xB4 else "LPOSMAX"
            raw = data[5:9]
            val = int.from_bytes(raw[2:4] + raw[0:2], "big", signed=True)
            if self.corrupt == (ax, var):
                val += 1
            self.v[(ax, var)] = val
            self._pending = b"O"
            return len(data)
        return super().write(data)

    def limit_writes(self):
        return [w for w in self.writes if len(w) == 10 and w[3] == 0x25 and w[4] in (0xB4, 0xB6)]


def go(tmp_path, fake=None, lim=SYN, **kw):
    fake = fake or FakeLim()
    args = {"send": True, "confirm": "LOAD XY CALIBRATED LIMITS ON COM3", "on_for_2_min": True, "stage_clear": True,
            "calibration": cal_file(tmp_path, lim)}
    args.update(kw)
    res = g7.run(find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0, sleep=lambda s: None,
                 **args)
    return res, fake


def test_builder_only_xy_limits():
    f = g7.build_limit_write(1, "LPOSMAX", 50000)
    assert f.hex(" ") == "08 00 10 25 b6 c3 50 00 00 06" and sum(f[:-1]) & 0xFF == f[-1]
    for bad in ((4, "LPOSMIN", 0), (3, "LPOSMAX", 0), (1, "CPOSCMD", 0), (1, "ESTATE", 0)):
        with pytest.raises(g7.MotorGate7Refused):
            g7.build_limit_write(*bad)
    assert REVIEWED_MAINTENANCE["motor.xy_load_calibrated_limits"]["commands"] == frozenset({0x25B4, 0x25B6})


@pytest.mark.parametrize("lim, why", [
    ({"X": (-1000, 200000), "Y": SYN["Y"]}, "span"),
    ({"X": (-10000, -10000 + round(119.1 * CPM)), "Y": SYN["Y"]}, "lMin"),
    ({"X": (500, 500 + round(119.1 * CPM)), "Y": SYN["Y"]}, "lMin"),
    ({"X": SYN["X"], "Y": (-1500, 40000)}, "Y"),
])
def test_bad_calibration_refused_before_opening(tmp_path, lim, why):
    fake = FakeLim()
    res, f = go(tmp_path, fake, lim=lim)
    assert res.outcome == "calibration_refused" and why in res.message and f.writes == []


def test_missing_calibration(tmp_path):
    res = g7.run(send=True, scans=tmp_path, find_ports=lambda: [PORT])
    assert res.outcome == "refused" and "no parsed calibration" in res.message


def test_dry_run_shows_four_writes_and_sends_nothing(tmp_path):
    res, fake = go(tmp_path, send=False, on_for_2_min=False, stage_clear=False, confirm="")
    assert res.outcome == "dry_run" and fake.writes == [] and len(res.exchanges) == 4


def test_pass_writes_four_and_reads_back(tmp_path):
    res, fake = go(tmp_path)
    assert res.outcome == "pass", (res.message, res.final)
    assert fake.limit_writes() == [w[4] for w in g7.plan_writes(SYN)]
    assert fake.v[("X", "LPOSMIN")] == SYN["X"][0] and fake.v[("Y", "LPOSMAX")] == SYN["Y"][1]
    assert all(res.final["checks"].values())
    # nothing but limit writes was sent
    assert [w for w in fake.writes if w not in (b"\xff",) and w[3:5] not in (b"\xb0\x04", b"\xb0\x05", b"\xb0\x00")
            and w[3] != 0xB0] == fake.limit_writes()


def test_readback_mismatch_stops_writing(tmp_path):
    res, fake = go(tmp_path, FakeLim(corrupt=("X", "LPOSMAX")))
    assert res.outcome == "write_failed" and len(fake.limit_writes()) == 2


def test_already_loaded_writes_nothing(tmp_path):
    res, fake = go(tmp_path, FakeLim(start=SYN))
    assert res.outcome == "already_loaded" and fake.limit_writes() == []


def test_unexpected_current_limits_refused(tmp_path):
    res, fake = go(tmp_path, FakeLim(start={"X": (0, 40000)}))
    assert res.outcome == "baseline_refused" and fake.limit_writes() == []


def test_axis_away_from_home_refused(tmp_path):
    fake = FakeLim()
    fake.v[("Y", "APOS")] = fake.v[("Y", "CPOS")] = 2268
    res, f = go(tmp_path, fake)
    assert res.outcome == "baseline_refused" and "Y not at home" in res.message and f.limit_writes() == []


def test_link_refuses_other_writes(tmp_path):
    link = g4._Link(FakeLim(), g4.MotorGate4Result("x", "", "", ""), lambda: 0.0, lambda s: None,
                    allowed_writes={w[4] for w in g7.plan_writes(SYN)}, allowed_reads=g7.ALLOWED_READS)
    with pytest.raises(g4.MotorGate4Refused):
        link.write("move", g4.M3_ESTATE_MOVE)
