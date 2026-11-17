"""Motor Gate 18 (ADR-0052): first Z move, against a fake chain whose Z follows STATE_8 / STATE_6. No hardware."""

from __future__ import annotations

from test_maintenance_motor_gate7_xy_limits import PORT
from test_maintenance_motor_gate8_t1_runs import FF, _val
from test_maintenance_motor_gate17_z_o_reads import Chain

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate18_z_move as g18
from openevos.safety.firmware import REVIEWED_MAINTENANCE


class ZChain(Chain):
    """Z moves at once to its (clamped) target on ESTATE 8; ESTATE 6 stops it at rest. `stuck`: Z never completes."""

    def __init__(self, overrides=None, stuck=False, idle_stop_moves=0):
        super().__init__(overrides)
        self.by_frame |= {r.frame: r for r in g4.ESTATEV_ALL}
        self.v |= {(r.axis, r.var): 0 for r in g4.ESTATEV_ALL if (r.axis, r.var) not in self.v}
        self.stuck, self.idle_stop_moves = stuck, idle_stop_moves

    def write(self, data):
        data = bytes(data)
        if data == FF or data == mg1.ID_QUERY or data in self.by_frame:
            return super().write(data)
        self.writes.append(data)
        self._pending = b"O"
        v = self.v
        if data[3:5] == bytes([0x25, 0xB3]):
            v[("Z", "CPOSCMD")] = _val(data)
        elif data == g18.Z3_ESTATE_MOVE:
            target = min(max(v[("Z", "CPOSCMD")], v[("Z", "LPOSMIN")]), v[("Z", "LPOSMAX")])
            v[("Z", "ESTATEV")] = 13
            if self.stuck:
                v[("Z", "SRH")] = 0x013F
            else:
                v[("Z", "CPOS")] = v[("Z", "APOS")] = v[("Z", "LTARGETPOSV")] = target
                v[("Z", "SRH")] = 0x033F
        elif data == g18.Z4_ESTATE_STOP:
            v[("Z", "APOS")] += self.idle_stop_moves
            v[("Z", "CPOS")] = v[("Z", "APOS")]
            v[("Z", "ESTATEV")], v[("Z", "SRH")] = 0, 0x033F
        return len(data)


PHRASE = g18.confirm_phrase(PORT.device)


def go(chain, **kw):
    args = {"send": True, "on_for_2_min": True, "stage_empty_watching": True, "confirm": PHRASE} | kw
    return g18.run(find_ports=lambda: [PORT], open_port=lambda d: chain, monotonic=chain.monotonic,
                   sleep=chain.sleep, **args)


def _z_writes(c):
    return [w for w in c.writes if w != FF and w != mg1.ID_QUERY and w not in c.by_frame]


def test_frames_are_the_cfg_frames_and_nothing_else_can_be_built():
    assert g18.Z3_ESTATE_MOVE == bytes.fromhex("06 00 40 21 B1 00 08 20")
    assert g18.Z4_ESTATE_STOP == bytes.fromhex("06 00 40 21 B1 00 06 1E")
    # 8200 = 0x2008, low word first; checksum = byte sum mod 256 (same rule as Gate 4's verified X frames)
    assert g18.Z1_TARGET_OUT == bytes.fromhex("08 00 40 25 B3 20 08 00 00 48")
    assert g18.Z2_TARGET_HOME == bytes.fromhex("08 00 40 25 B3 00 00 00 00 20")
    assert all(f[-1] == sum(f[:-1]) & 0xFF for f in g18.ALLOWED_WRITES)
    for bad in (1, -8200, 8201, 67000):
        try:
            g18.target_frame(bad)
        except g4.MotorGate4Refused:
            continue
        raise AssertionError(f"target {bad} was built")
    entry = REVIEWED_MAINTENANCE["motor.z_move_1mm_and_back"]
    assert entry["module"] == "maintenance/motor_gate18_z_move.py"
    assert {int.from_bytes(f[3:5], "big") for f in g18.ALLOWED_WRITES} == entry["commands"]
    assert all(f[2] == 0x40 for f in g18.ALLOWED_WRITES)  # axis 4 only


def test_dry_run_and_missing_confirmations_open_nothing():
    opened = []
    res = g18.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == []
    c = ZChain()
    for kw in ({"on_for_2_min": False}, {"stage_empty_watching": False}, {"confirm": "MOVE Z ON COM3"}):
        assert go(c, **kw).outcome == "refused"
    assert c.writes == []


def test_pass_idle_stop_then_out_and_back():
    c = ZChain()
    res = go(c)
    assert res.outcome == "pass", res.message
    assert _z_writes(c) == [g18.Z4_ESTATE_STOP, g18.Z1_TARGET_OUT, g18.Z3_ESTATE_MOVE, g18.Z2_TARGET_HOME,
                            g18.Z3_ESTATE_MOVE, g18.Z4_ESTATE_STOP]
    assert res.legs["out"]["APOS"] == 8200 and res.legs["home"]["APOS"] == 0
    assert res.final["checks"]["z_still_on"] and c.v[("Z", "APOS")] == 0


def test_five_mm_option_uses_its_own_frame_and_phrase():
    assert g18.Z5_TARGET_OUT == bytes.fromhex("08 00 40 25 B3 A0 28 00 00 E8")  # 41000 = 0xA028
    c = ZChain()
    assert go(c, mm=5).outcome == "refused" and c.writes == []  # the 1 mm phrase does not run 5 mm
    res = go(c, mm=5, confirm=g18.confirm_phrase(PORT.device, 5))
    assert res.outcome == "pass", res.message
    assert res.legs["out"]["APOS"] == 41000 and g18.Z5_TARGET_OUT in _z_writes(c)
    assert g18.Z1_TARGET_OUT not in _z_writes(c) and c.v[("Z", "APOS")] == 0
    assert go(ZChain(), mm=3).outcome == "refused"


def test_baseline_refusals_write_nothing():
    for over, why in (({("Z", "APOS"): 40, ("Z", "CPOS"): 40}, "not at home"),
                      ({("Z", "CSPDCMD"): 20 * 65536}, "power-up"),
                      ({("Z", "MER"): 0x0100}, "MER"),
                      ({("Z", "SRL"): 0x4400}, "power stage off"),
                      ({("Z", "LPOSMAX"): 8000}, "do not contain")):
        c = ZChain(over)
        res = go(c)
        assert res.outcome == "baseline_refused" and why in res.message, (over, res.message)
        assert _z_writes(c) == []


def test_idle_stop_that_moves_z_is_a_fault():
    c = ZChain(idle_stop_moves=30)
    res = go(c)
    assert res.outcome == "fault_stopped" and "idle stop changed Z" in res.fault
    assert _z_writes(c) == [g18.Z4_ESTATE_STOP, g18.Z4_ESTATE_STOP]  # the idle stop, then the fault stop only


def test_z_that_never_completes_is_stopped():
    c = ZChain(stuck=True)
    res = go(c)
    assert res.outcome == "fault_stopped" and _z_writes(c)[-1] == g18.Z4_ESTATE_STOP
    assert g18.Z2_TARGET_HOME not in _z_writes(c)
