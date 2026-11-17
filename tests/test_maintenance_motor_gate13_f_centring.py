"""Motor Gate 13 (ADR-0042): F centring steps short of the mirror, against a fake F drive. No real hardware."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_maintenance_motor_gate4_x_move import PORT
from test_maintenance_motor_gate12_f_mirror import FakeFMirror

from openevos.maintenance import motor_gate13_f_centring as g13
from openevos.safety.firmware import REVIEWED_MAINTENANCE

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "evidence" / "fl1_v1.6_motor_gate13_f_centring_spec.md"
PHRASE = "STEP F SHORT OF MIRROR ON COM3"


class FakeFSteps(FakeFMirror):
    """Gate 12's fake plus LTARGETPOS (32-bit) + ESTATE=2 (GOTO_POS, clamped like the drive). Starts at the mirror."""

    def __init__(self, *, at=128000, mer_after_moves=None, **kw):
        super().__init__(**kw)
        self.v[("F", "APOS")] = self.v[("F", "CPOS")] = at
        self.ltarget, self.moves, self.mer_after_moves = 0, 0, mer_after_moves

    def write(self, data):
        n = super().write(data)
        data = bytes(data)
        if data[:5] == bytes([0x08, 0x00, 0x50, 0x25, 0xB7]):
            self.ltarget = int.from_bytes(data[7:9] + data[5:7], "big", signed=True)
        elif data == g13.GOTO_POS_FRAME:
            lo, hi = self.v[("F", "LPOSMIN")], self.v[("F", "LPOSMAX")]
            self.target = max(lo, min(hi, self.ltarget))
            self.v[("F", "ISTATUSFLAGS")] = 0x1000
        if data in (g13.GOTO_POS_FRAME, g13.GOTO_MIRR_FRAME):
            self.moves += 1
            if self.mer_after_moves is not None and self.moves >= self.mer_after_moves:
                self.v[("F", "MER")] = 0x0040
        return n


def go(fake, **kw):
    args = {"send": True, "confirm": PHRASE, "on_for_2_min": True, "nobody_looking": True}
    args.update(kw)
    return g13.run(find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0, sleep=lambda s: None,
                   utcnow=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc), **args)


def gate_writes(fake):
    return [w for w in fake.writes if w in g13.ALLOWED_WRITES]


def test_frames_registry_and_spec():
    assert g13.GOTO_POS_FRAME.hex(" ") == "06 00 50 21 b5 00 02 2e"  # = PearlScope's FAxisMoveToPos frame
    assert g13.target_frame(124000).hex(" ") == "08 00 50 25 b7 e4 60 00 01 79"
    assert len(g13.ALLOWED_WRITES) == len(g13.TARGETS) + 3
    assert all(sum(f[:-1]) & 0xFF == f[-1] for f in g13.ALLOWED_WRITES)
    assert all(102000 + 10000 <= p < 128000 for p in g13.TARGETS)
    with pytest.raises(g13.g4.MotorGate4Refused):
        g13.target_frame(130000)
    e = REVIEWED_MAINTENANCE["motor.f_centring_steps"]
    assert e["adr"] == "ADR-0042" and e["module"] == "maintenance/motor_gate13_f_centring.py"
    assert (ROOT / "docs" / "decisions" / "ADR-0042-motor-gate13-f-centring.md").is_file()
    text = SPEC.read_text(encoding="utf-8")
    for f in g13.ALLOWED_WRITES:
        assert f.hex(" ").upper() in text


def test_dry_run_and_refusals_open_nothing():
    opened = []
    res = g13.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == [] and PHRASE in res.message
    assert len(res.exchanges) == 2 * len(g13.TARGETS) + 1
    good = {"send": True, "on_for_2_min": True, "nobody_looking": True, "confirm": PHRASE}
    for kw in ({"nobody_looking": False}, {"on_for_2_min": False}, {"confirm": "MOVE F TO MIRROR ON COM3"},
               {"dwell_s": 30.0}, {"dwell_s": 0.0}):
        res = g13.run(**{**good, **kw}, find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
        assert res.outcome == "refused" and opened == []


def test_steps_through_the_fixed_targets_and_returns():
    fake = FakeFSteps()
    res = go(fake)
    assert res.outcome == "pass", res.message
    assert [res.legs[f"P{i}"]["after_dwell"]["APOS"] for i in range(1, 5)] == list(g13.TARGETS)
    assert res.final["APOS"] == 128000
    expected = [w for p in g13.TARGETS for w in (g13.target_frame(p), g13.GOTO_POS_FRAME)] + [g13.GOTO_MIRR_FRAME]
    assert gate_writes(fake) == expected
    assert all("converged_utc" in leg and "dwell_end_utc" in leg for leg in res.legs.values())


def test_refuses_unless_at_the_mirror_and_reviewed():
    for fake in (FakeFSteps(at=0), FakeFSteps(mirror=130000), FakeFSteps(limits=(0, 118000))):
        res = go(fake)
        assert res.outcome == "baseline_refused" and not gate_writes(fake), res.message


def test_mer_after_a_dwell_stops_f_where_it_is():
    fake = FakeFSteps(mer_after_moves=1)
    res = go(fake)
    assert res.outcome == "fault_stopped" and "MER" in res.fault and "NOT at the mirror" in res.message
    assert gate_writes(fake) == [g13.target_frame(124000), g13.GOTO_POS_FRAME, g13.STOP_FRAME]


def test_wrong_way_motion_sends_the_f_stop():
    fake = FakeFSteps(wrong_way=True, step=500)
    res = go(fake)
    assert res.outcome == "fault_stopped" and "moved away" in res.fault
    assert gate_writes(fake)[-1] == g13.STOP_FRAME
