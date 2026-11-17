"""Motor Gate 19 rev 2 (ADR-0052): turret change against a fake O drive that turns one position per STEP_S, the numbers
DECREASING (as on the unit, run 1: 1 -> 5). No hardware."""

from __future__ import annotations

from test_maintenance_motor_gate7_xy_limits import PORT
from test_maintenance_motor_gate8_t1_runs import FF, _reply
from test_maintenance_motor_gate18_z_move import ZChain

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate17_z_o_reads as g17
from openevos.maintenance import motor_gate19_o_turret as g19
from openevos.safety.firmware import REVIEWED_MAINTENANCE

HF_FOR = {pos: hf for hf, pos in g17.O_HF_TABLE.items()}
STEP_S = 1.0
CENTRE = {("X", "APOS"): round(59.5 * g19.COUNTS_PER_MM_XY), ("Y", "APOS"): round(39.0 * g19.COUNTS_PER_MM_XY)}


class OChain(ZChain):
    """O at position 1 (Gate 17), switched off; stage centred. GOTO_POS turns one position per STEP_S of fake time,
    numbers decreasing. `between_last`: the turret starts between positions, having last passed this one."""

    def __init__(self, overrides=None, stuck_after=None, overshoot=False, z_drift=0, between_last=None,
                 interrupt_after=None):
        base = {("O", "ICURRENTPOSHF"): 1, ("O", "IHF"): 4, ("O", "ITARGETPOS"): 1, **CENTRE}
        if between_last is not None:
            base |= {("O", "ICURRENTPOSHF"): 0, ("O", "IHF"): 14}
        super().__init__(base | (overrides or {}))
        self.by_frame |= {r.frame: r for r in g19.XYR.values()}
        self.stuck_after, self.overshoot, self.z_drift = stuck_after, overshoot, z_drift
        self.between_last, self.interrupt_after = between_last, interrupt_after
        self.turn = None  # (t0, start)
        self.polls = 0

    def _advance(self):
        if self.turn is None:
            return
        t0, start = self.turn
        v = self.v
        steps = int((self.t - t0) / STEP_S)
        target = v[("O", "ITARGETPOS")]
        need = (start - target) % 5 + (1 if self.overshoot else 0)
        if self.stuck_after is not None and steps >= self.stuck_after:
            v[("O", "ISTATUSFLAGS")] |= 2
            v[("O", "SRL")], v[("O", "ESTATEV")] = 0x4400, 0
            self.turn = None
            return
        steps = min(steps, need)
        if steps:
            pos = (start - 1 - steps) % 5 + 1
            v[("O", "ICURRENTPOSHF")], v[("O", "IHF")] = pos, HF_FOR[pos]
        v[("Z", "APOS")] += self.z_drift
        if steps == need:
            v[("O", "ESTATEV")], v[("O", "IPENDINGMOVES")], v[("O", "SRL")] = 0, 0, 0x4400
            self.turn = None

    def write(self, data):
        data = bytes(data)
        if data in self.by_frame:
            r = self.by_frame[data]
            if self.turn is not None and r.var == "ESTATEV" and r.axis == "O":
                self.polls += 1
                if self.interrupt_after is not None and self.polls > self.interrupt_after:
                    self.interrupt_after = None
                    raise KeyboardInterrupt
            self._advance()
            self.writes.append(data)
            self.t += 0.016
            self._pending = _reply(r, self.v[(r.axis, r.var)])
            return len(data)
        if data == FF or data == mg1.ID_QUERY or data[2] != 0x60:
            return super().write(data)
        self.writes.append(data)
        self._pending = b"O"
        if data[3:5] == bytes([0x21, 0xCC]):
            self.v[("O", "ITARGETPOS")] = int.from_bytes(data[5:7], "big")
        elif data == g19.O_GOTO:
            start = self.v[("O", "ICURRENTPOSHF")] or self.between_last
            self.turn = (self.t, start)
            self.v[("O", "ESTATEV")], self.v[("O", "IPENDINGMOVES")], self.v[("O", "SRL")] = 3, 1, 0xC400
        elif data == g19.O_AXISOFF:
            self.turn = None
            self.v[("O", "SRL")] = 0x4400
        return len(data)


def go(chain, n=5, **kw):
    args = {"send": True, "on_for_2_min": True, "stage_empty_z_down": True,
            "confirm": g19.confirm_phrase(PORT.device, n)} | kw
    return g19.run(n=n, find_ports=lambda: [PORT], open_port=lambda d: chain, monotonic=chain.monotonic,
                   sleep=chain.sleep, **args)


def _o_writes(c):
    return [w for w in c.writes if w != FF and w != mg1.ID_QUERY and w not in c.by_frame]


def test_frames_direction_and_registry():
    assert g19.target_frame(1) == bytes.fromhex("06 00 60 21 CC 00 01 54")
    assert g19.O_GOTO == bytes.fromhex("06 00 60 21 B1 00 03 3B")
    assert all(f[-1] == sum(f[:-1]) & 0xFF and f[2] == 0x60 for f in g19.ALLOWED_WRITES)
    for bad in (0, 6, -1):
        try:
            g19.target_frame(bad)
        except g4.MotorGate4Refused:
            continue
        raise AssertionError(bad)
    entry = REVIEWED_MAINTENANCE["motor.o_turret_goto_position"]
    ops = {int.from_bytes(f[3:5], "big") for f in g19.ALLOWED_WRITES if len(f) > 6} | {0x0002}
    assert ops == entry["commands"] and entry["module"] == "maintenance/motor_gate19_o_turret.py"
    # numbers decrease as the turret turns (run 1 on the unit: 1 -> 5)
    assert g19.path_from(1, 5) == [5] and g19.path_from(1, 2) == [5, 4, 3, 2] and g19.path_from(5, 4) == [4]


def test_dry_run_and_refusals_open_nothing():
    opened = []
    assert g19.run(n=2, find_ports=lambda: [PORT], open_port=lambda d: opened.append(d)).outcome == "dry_run"
    assert opened == []
    c = OChain()
    for kw in ({"on_for_2_min": False}, {"stage_empty_z_down": False}, {"confirm": "TURN TURRET ON COM3"}):
        assert go(c, **kw).outcome == "refused"
    assert g19.run(n=7, find_ports=lambda: [PORT]).outcome == "refused" and c.writes == []


def test_one_step_passes():
    c = OChain()
    res = go(c, n=5)
    assert res.outcome == "pass", res.message
    assert _o_writes(c) == [g19.target_frame(5), g19.O_GOTO]
    assert c.v[("O", "ICURRENTPOSHF")] == 5 and "60X" in res.message


def test_long_way_passes_through_the_others():
    c = OChain()
    res = go(c, n=2)
    assert res.outcome == "pass", res.message
    assert res.legs["turn"]["path"] == [5, 4, 3, 2]


def test_from_between_finishes_the_step_it_started():
    c = OChain(between_last=5)  # run 1 left it past 5, heading for 4
    res = go(c, n=4, from_between=5)
    assert res.outcome == "pass", res.message
    assert res.legs["turn"]["path"] == [4] and c.v[("O", "ICURRENTPOSHF")] == 4
    res = go(OChain(between_last=5), n=4)  # without --from-between: refused, nothing written
    assert res.outcome == "baseline_refused" and "--from-between" in res.message


def test_refusals_write_nothing():
    for over, why in (({("Z", "APOS"): 8200, ("Z", "CPOS"): 8200}, "Z not retracted"),
                      ({("X", "APOS"): 0, ("Y", "APOS"): 0}, "centre the stage"),
                      ({("O", "ICURRENTPOSHF"): 0}, "position unknown"),
                      ({("O", "SRL"): 0xC400}, "power stage on"),
                      ({("O", "ISTATUSFLAGS"): 0x1002}, "stuck flag")):
        c = OChain(over)
        res = go(c)
        assert res.outcome == "baseline_refused" and why in res.message, (over, res.message)
        assert _o_writes(c) == []
    res = go(OChain(), n=1)
    assert res.outcome == "baseline_refused" and "already at position 1" in res.message


def test_faults_send_axisoff_once():
    for kw, why in (({"stuck_after": 0}, "stuck"), ({"overshoot": True}, "not next on its path"),
                    ({"z_drift": 10}, "Z moved")):
        c = OChain(**kw)
        res = go(c, n=5)
        assert res.outcome == "fault_stopped" and why in res.fault, (kw, res.fault)
        assert _o_writes(c)[-1] == g19.O_AXISOFF and _o_writes(c).count(g19.O_AXISOFF) == 1


def test_ctrl_c_cuts_the_turret_power():
    c = OChain(interrupt_after=1)
    res = go(c, n=2)
    assert res.outcome == "fault_stopped" and "Ctrl+C" in res.fault
    assert _o_writes(c)[-1] == g19.O_AXISOFF
