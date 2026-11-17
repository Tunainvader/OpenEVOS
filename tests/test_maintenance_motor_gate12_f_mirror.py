"""Motor Gate 12 (ADR-0041): F carriage to the mirror / back to slot 1, against a fake F drive. No real hardware."""

from __future__ import annotations

from pathlib import Path

from test_maintenance_motor_gate4_x_move import PORT
from test_maintenance_motor_gate11_f_cube import FakeF

from openevos.maintenance import motor_gate12_f_mirror as g12
from openevos.safety.firmware import REVIEWED_MAINTENANCE

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "evidence" / "fl1_v1.6_motor_gate12_f_mirror_spec.md"


class FakeFMirror(FakeF):
    """Gate 11's fake F drive plus STATE_17_GOTO_MIRR (moves to its stored LMIRROR1POS)."""

    def __init__(self, *, mirror=128000, limits=(0, 128000), **kw):
        super().__init__(**kw)
        self.v[("F", "LMIRROR1POS")] = mirror
        self.v[("F", "LPOSMIN")], self.v[("F", "LPOSMAX")] = limits

    def write(self, data):
        n = super().write(data)
        if bytes(data) == g12.GOTO_MIRR_FRAME:
            self.target = self.v[("F", "LMIRROR1POS")]
            self.v[("F", "ISTATUSFLAGS")] = 0x1000  # converged flag cleared at move start
        return n


def go(fake, to="mirror", **kw):
    phrase = "MOVE F TO MIRROR ON COM3" if to == "mirror" else "RETURN F TO CUBE 1 ON COM3"
    args = {"send": True, "confirm": phrase, "on_for_2_min": True, "nobody_looking": True}
    args.update(kw)
    return g12.run(to=to, find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0,
                   sleep=lambda s: None, **args)


def gate_writes(fake):
    return [w for w in fake.writes if w in g12.ALLOWED_WRITES]


def test_frames_registry_and_spec():
    assert g12.GOTO_MIRR_FRAME.hex(" ") == "06 00 50 21 b5 00 11 3d"
    assert g12.ALLOWED_WRITES == {g12.GOTO_MIRR_FRAME, g12.g11.slot_frame(1), g12.g11.GOTO_FRAME, g12.g11.STOP_FRAME}
    assert all(sum(f[:-1]) & 0xFF == f[-1] for f in g12.ALLOWED_WRITES)
    e = REVIEWED_MAINTENANCE["motor.f_goto_mirror"]
    assert e["adr"] == "ADR-0041" and e["module"] == "maintenance/motor_gate12_f_mirror.py"
    assert (ROOT / "docs" / "decisions" / "ADR-0041-motor-gate12-f-mirror.md").is_file()
    text = SPEC.read_text(encoding="utf-8")
    for f in g12.ALLOWED_WRITES:
        assert f.hex(" ").upper() in text


def test_dry_run_and_refusals_open_nothing():
    opened = []
    for to in ("mirror", "slot1"):
        res = g12.run(to=to, find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
        assert res.outcome == "dry_run" and opened == []
        assert g12.confirm_phrase("COM3", to) in res.message
    assert [e.request_hex for e in g12.run(find_ports=lambda: [PORT]).exchanges] == [g12.GOTO_MIRR_FRAME.hex(" ")]
    good = {"send": True, "on_for_2_min": True, "nobody_looking": True, "confirm": "MOVE F TO MIRROR ON COM3"}
    for kw in ({"nobody_looking": False}, {"on_for_2_min": False}, {"confirm": "MOVE F TO CUBE 1 ON COM3"},
               {"confirm": "RETURN F TO CUBE 1 ON COM3"}):
        res = g12.run(to="mirror", **{**good, **kw}, find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
        assert res.outcome == "refused" and opened == []
    assert g12.run(to="cube5", find_ports=lambda: [PORT]).outcome == "refused"
    assert g12.run(find_ports=list).outcome == "refused"


def test_moves_to_the_mirror_with_one_state_request():
    fake = FakeFMirror()
    res = go(fake)
    assert res.outcome == "pass", res.message
    assert res.final["APOS"] == 128000 and res.legs["goto"]["target"] == 128000
    assert gate_writes(fake) == [g12.GOTO_MIRR_FRAME]
    assert {"ESTATEV", "APOS", "ISTATUSFLAGS", "MER", "SRH", "SRL"} <= set(res.final)


def test_returns_to_slot_1_with_gate11_frames():
    fake = FakeFMirror()
    fake.v[("F", "APOS")] = fake.v[("F", "CPOS")] = 128000
    res = go(fake, to="slot1")
    assert res.outcome == "pass", res.message
    assert res.final["APOS"] == 0
    assert gate_writes(fake) == [g12.SLOT1_FRAME, g12.GOTO_CUBE_FRAME]


def test_unexpected_drive_values_refuse_with_nothing_written():
    for fake in (FakeFMirror(mirror=130000), FakeFMirror(limits=(0, 131500)),
                 FakeFMirror(cube=(0, 34000, 70000, 102000))):
        res = go(fake)
        assert res.outcome == "baseline_refused" and not gate_writes(fake), res.message
    fake = FakeFMirror()
    fake.v[("F", "MER")] = 0x0040  # LSP limit switch
    res = go(fake)
    assert res.outcome == "baseline_refused" and "MER" in res.message and not gate_writes(fake)


def test_wrong_way_motion_sends_the_f_stop():
    fake = FakeFMirror(wrong_way=True, step=500)
    fake.v[("F", "APOS")] = fake.v[("F", "CPOS")] = 34000
    res = go(fake)
    assert res.outcome == "fault_stopped" and "moved away" in res.fault
    assert gate_writes(fake) == [g12.GOTO_MIRR_FRAME, g12.STOP_FRAME]


def test_timeout_sends_the_f_stop():
    fake = FakeFMirror(step=1)
    clock = iter(range(10_000))
    res = g12.run(to="mirror", send=True, confirm="MOVE F TO MIRROR ON COM3", on_for_2_min=True, nobody_looking=True,
                  find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: float(next(clock)),
                  sleep=lambda s: None)
    assert res.outcome == "fault_stopped" and "in time" in res.fault
    assert gate_writes(fake)[-1] == g12.STOP_FRAME
