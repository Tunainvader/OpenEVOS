"""Motor Gate 10 (ADR-0032): speed presets, measured on a fake X drive. No real hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_maintenance_motor_gate4_x_move import PORT, FakeX

from openevos.backends.technosoft import protocol as tp
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate10_speed as g10
from openevos.safety.firmware import REVIEWED_MAINTENANCE

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate10_speed_spec.md"


class Clock:
    """Fake time: each SRH poll of a move advances time by the distance/speed of the drive's CSPDCMD."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def go(fake, **kw):
    clock = Clock()
    args = {"send": True, "confirm": "MEASURE X SPEEDS ON COM3", "on_for_2_min": True, "stage_empty_watching": True}
    args.update(kw)

    def sleep(s):
        clock.t += s

    real_write = fake.write

    def write(data):
        n = real_write(data)
        if bytes(data) == g4.M3_ESTATE_MOVE:  # a move takes distance / speed of the current CSPDCMD
            cspd = fake.v[("X", "CSPDCMD")] / 65536
            mm_s = cspd / tp.SPEED_SAMPLE_S / tp.COUNTS_PER_MM["X"]
            clock.t += g10.DIST_MM / mm_s
        return n

    fake.write = write
    return g10.run(find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=clock, sleep=sleep, **args)


def test_frames_closed_set_and_registry():
    assert g10.SPEED["fast"].hex(" ") == "08 00 10 25 d6 00 00 00 0f 22"  # the power-up 15.0
    assert [round(tp.speed_raw(p) / 65536, 4) for p in ("slow", "medium", "fast")] == [1.8142, 7.2567, 15.0]
    assert all(tp.is_motion_write(tp.speed_write(a, p)) for a in "XY" for p in tp.SPEED_PRESETS_MM_S)
    assert not tp.is_motion_write(bytes.fromhex("08 00 10 25 D6 00 00 00 20 2B"))  # 32.0: refused
    with pytest.raises(tp.ProtocolRefused):
        tp.speed_write("Z", "slow")
    e = REVIEWED_MAINTENANCE["motor.x_speed_presets"]
    assert e["adr"] == "ADR-0032" and e["module"] == "maintenance/motor_gate10_speed.py"
    text = SPEC.read_text(encoding="utf-8")
    for f in g10.SPEED.values():
        assert f.hex(" ").upper() in text


def test_dry_run_and_refusal():
    opened = []
    res = g10.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == []
    res = g10.run(send=True, confirm="TRAVERSE XY FULL RANGE ON COM3", on_for_2_min=True, stage_empty_watching=True,
                  find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "refused" and opened == []


def test_measures_each_preset_and_restores_the_power_up_speed():
    fake = FakeX(move_polls=2)
    res = go(fake)
    assert res.outcome == "pass", (res.message, res.final)
    sp = res.baseline["speeds"]
    assert set(sp) == {"slow", "medium", "fast"}
    assert 0.95 <= res.baseline["fit"]["speed_scale"] <= 1.05
    assert fake.v[("X", "CSPDCMD")] == tp.POWER_UP_SPEED_RAW
    assert [w for w in fake.writes if w in g10.ALLOWED_WRITES][-1] == g10.SPEED["fast"]  # the last write restores 15.0


def test_fit_recovers_speed_scale_and_overhead_from_the_real_run():
    # leg times recorded on the instrument, 2026-11-13 (motor-gate10-20261113T165520)
    real = {"slow": {"preset_mm_s": 1.0, "leg_s": {"out": 12.843, "home": 12.828}},
            "medium": {"preset_mm_s": 4.0, "leg_s": {"out": 5.39, "home": 5.375}},
            "fast": {"preset_mm_s": 8.268, "leg_s": {"out": 4.125, "home": 4.156}}}
    fit = g10.analyse(real)
    assert fit["problems"] == [] and 0.98 < fit["speed_scale"] < 1.03 and 2.7 < fit["overhead_s"] < 3.0
    too_fast = {p: {**r, "leg_s": {k: (v - 2.87) / 1.3 + 2.87 for k, v in r["leg_s"].items()}} for p, r in real.items()}
    assert any("scale" in p for p in g10.analyse(too_fast)["problems"])  # 30 % faster than set: refused


def test_fault_still_restores_the_power_up_speed():
    fake = FakeX(overshoot=40, band=8)
    res = go(fake)
    assert res.outcome == "fault_stopped"
    assert [w for w in fake.writes if w in g10.ALLOWED_WRITES][-1] == g10.SPEED["fast"]
