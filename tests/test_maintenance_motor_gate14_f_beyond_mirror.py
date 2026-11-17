"""Motor Gate 14 (ADR-0043): F past 128,000 via stepped RAM LPOSMAX, against a fake F drive. No real hardware."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_maintenance_motor_gate4_x_move import PORT
from test_maintenance_motor_gate13_f_centring import FakeFSteps

from openevos.maintenance import motor_gate14_f_beyond_mirror as g14
from openevos.safety.firmware import REVIEWED_MAINTENANCE

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "evidence" / "fl1_v1.6_motor_gate14_f_beyond_mirror_spec.md"


class FakeFLimit(FakeFSteps):
    """Gate 13's fake plus LPOSMAX writes, IIQMAX/IIQMIN peaks, an optional hard stop and an optional drive ISR."""

    def __init__(self, *, hard_stop=None, peaks=None, isr_at=None, **kw):
        super().__init__(**kw)
        self.by_frame.update({r.frame: r for r in g14.EXTRA_READS.values()})
        self.v[("F", "IIQMAX")], self.v[("F", "IIQMIN")] = 0, 0
        self.hard_stop, self.peaks, self.isr_at = hard_stop, peaks or {}, isr_at

    def write(self, data):
        data = bytes(data)
        n = super().write(data)
        if data[:5] == bytes([0x08, 0x00, 0x50, 0x25, 0xEC]):
            self.v[("F", "LPOSMAX")] = int.from_bytes(data[7:9] + data[5:7], "big", signed=True)
        if data in (g14.GOTO_POS_FRAME, g14.GOTO_MIRR_FRAME) and self.target is not None:
            p = self.peaks.get(self.target, 100)
            self.v[("F", "IIQMAX")], self.v[("F", "IIQMIN")] = p, -int(p * 0.8)
            if self.isr_at == self.target:
                self.v[("F", "ISTATUSFLAGS")] |= 0x0001
        if self.hard_stop is not None and self.v[("F", "APOS")] > self.hard_stop:
            self.v[("F", "APOS")] = self.v[("F", "CPOS")] = self.hard_stop
        return n


def go(fake, up_to=130000, monotonic=lambda: 0.0, **kw):
    args = {"send": True, "confirm": f"RAISE F LIMIT TO {up_to} ON COM3", "on_for_2_min": True,
            "nobody_looking": True}
    args.update(kw)
    return g14.run(up_to=up_to, find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=monotonic,
                   sleep=lambda s: None, utcnow=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc), **args)


def gate_writes(fake):
    return [w for w in fake.writes if w in g14.ALLOWED_WRITES]


def test_frames_registry_and_spec():
    assert g14.limit_frame(129000).hex(" ") == "08 00 50 25 ec f7 e8 00 01 49"
    assert g14.RESTORE_FRAME.hex(" ") == "08 00 50 25 ec f4 00 00 01 5e"
    assert max(g14.STEPS) == 134000 and all(b - a == 1000 for a, b in itertools.pairwise((128000, *g14.STEPS)))
    assert all(sum(f[:-1]) & 0xFF == f[-1] for f in g14.ALLOWED_WRITES)
    for bad in (135000, 128500):
        with pytest.raises(g14.g4.MotorGate4Refused):
            g14.limit_frame(bad)
    e = REVIEWED_MAINTENANCE["motor.f_beyond_mirror_steps"]
    assert e["adr"] == "ADR-0043" and e["module"] == "maintenance/motor_gate14_f_beyond_mirror.py"
    assert (ROOT / "docs" / "decisions" / "ADR-0043-motor-gate14-f-beyond-mirror.md").is_file()
    text = SPEC.read_text(encoding="utf-8")
    for f in g14.ALLOWED_WRITES:
        assert f.hex(" ").upper() in text


def test_dry_run_and_refusals_open_nothing():
    opened = []
    res = g14.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == [] and "RAISE F LIMIT TO 130000 ON COM3" in res.message
    assert res.exchanges[-1].request_hex == g14.RESTORE_FRAME.hex(" ")
    good = {"send": True, "on_for_2_min": True, "nobody_looking": True, "confirm": "RAISE F LIMIT TO 130000 ON COM3"}
    for kw in ({"nobody_looking": False}, {"confirm": "RAISE F LIMIT TO 134000 ON COM3"}, {"up_to": 135000},
               {"dwell_s": 20.0}):
        res = g14.run(**{**good, **kw}, find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
        assert res.outcome == "refused" and opened == []


def test_steps_up_and_restores():
    fake = FakeFLimit()
    res = go(fake, up_to=132000)
    assert res.outcome == "pass", res.message
    assert [res.legs[f"S{v}"]["after_dwell"]["APOS"] for v in (129000, 130000, 131000, 132000)] == \
        [129000, 130000, 131000, 132000]
    assert res.final["APOS"] == 128000 and res.final["LPOSMAX"] == 128000 and res.legs["restore"]["LPOSMAX"] == 128000
    w = gate_writes(fake)
    assert w[:3] == [g14.REF_FRAME, g14.GOTO_POS_FRAME, g14.GOTO_MIRR_FRAME]
    assert w[3:6] == [g14.limit_frame(129000), g14.target_frame(129000), g14.GOTO_POS_FRAME]
    assert w[-2:] == [g14.GOTO_MIRR_FRAME, g14.RESTORE_FRAME]
    assert g14.limit_frame(133000) not in w


def test_hard_stop_times_out_stops_and_restores():
    fake = FakeFLimit(hard_stop=130500)
    clock = itertools.count()
    res = go(fake, up_to=131000, monotonic=lambda: next(clock) * 0.01)
    assert res.outcome == "fault_stopped" and "S131000" in res.fault and "in time" in res.fault
    w = gate_writes(fake)
    assert w[-2:] == [g14.STOP_FRAME, g14.RESTORE_FRAME] and fake.v[("F", "LPOSMAX")] == 128000
    assert "LPOSMAX restored to 128000" in res.message


def test_high_peak_current_stops_and_restores():
    fake = FakeFLimit(peaks={130000: 500})
    res = go(fake, up_to=131000)
    assert res.outcome == "fault_stopped" and "possible contact" in res.fault
    assert gate_writes(fake)[-2:] == [g14.STOP_FRAME, g14.RESTORE_FRAME]
    assert g14.limit_frame(131000) not in gate_writes(fake)


def test_drive_isr_flag_stops_even_with_mer_clear():
    fake = FakeFLimit(isr_at=129000)
    res = go(fake)
    assert res.outcome == "fault_stopped" and "protection fired" in res.fault
    assert fake.v[("F", "MER")] == 0 and gate_writes(fake)[-2:] == [g14.STOP_FRAME, g14.RESTORE_FRAME]


def test_refuses_unless_at_the_mirror_with_reviewed_limits():
    for fake in (FakeFLimit(at=0), FakeFLimit(limits=(0, 130000))):
        res = go(fake)
        assert res.outcome == "baseline_refused" and not gate_writes(fake), res.message
    fake = FakeFLimit()
    fake.v[("F", "ISTATUSFLAGS")] = 0xD100
    res = go(fake)
    assert res.outcome == "baseline_refused" and "protection flag" in res.message
