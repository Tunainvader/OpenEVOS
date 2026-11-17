"""Motor Gate 17 (ADR-0052): read-only Z and O variables, against a fake drive chain. No hardware."""

from __future__ import annotations

from test_maintenance_motor_gate7_xy_limits import PORT
from test_maintenance_motor_gate8_t1_runs import FF, NORMAL, READ_S, _id_reply, _reply

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate17_z_o_reads as g17

HEALTHY = {
    "Z": {"HOMEDONE": 1, "ESTATEV": 0, "CPOSCMD": 0, "CSPDCMD": 10 * 65536, "CACCCMD": 16384, "CACCSTOP": 65536,
          "LPOSMIN": -1780, "LPOSMAX": 67000, "IIQMAX": 300, "IIQMIN": -200, "IPENDINGMOVES": 0, "LCONVERGEBAND": 5,
          "BDOAXISOFF": 0, "LTARGETPOSV": 0, "IVERSION": 21, "LHOMEINDEXDELTA": 1780, "ISTATUSFLAGS": 0x8000,
          "IINITONCEDONE": 12345, "APOS": 0, "CPOS": 0, "SRL": 0xC400, "SRH": 0x033F, "MER": 0},
    "O": {"ESTATEV": 0, "INUMTURRETPOSITIONS": 5, "IHF": 0, "ICURRENTPOSHF": 2, "ITARGETPOS": 2, "IMOVEPOSITIVE": 1,
          "LGOPASTAMOUNT": 9600, "LDEBOUNCEJUMP": 1000, "LTICKSPERGENEVAREV": 76800, "LTICKSPERTURRETREV": 384000,
          "IPENDINGMOVES": 0, "LSTUCKDELTA": 76800, "ISTATUSFLAGS": 0, "IVERSION": 7, "APOS": 0, "CPOS": 0,
          "SRL": 0x4400, "SRH": 0x033F, "MER": 0},
}


class Chain:
    def __init__(self, overrides=None):
        self.t = 0.0
        self.writes, self._pending = [], b""
        self.by_frame = {r.frame: r for rs in g17.READS.values() for r in rs.values()}
        self.by_frame |= {r.frame: r for r in g4.READINESS}
        self.v = {(ax, k): v for ax, d in HEALTHY.items() for k, v in d.items()}
        self.v |= {(r.axis, r.var): 12345 if r.var == "IINITONCEDONE" else 0 for r in g4.READINESS}
        self.v |= overrides or {}
        self.reads = 0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def inspect(self):
        return {"dcb": {"flags": dict(NORMAL)}, "timeouts": {}}

    def configure(self, baud):
        pass

    def purge_rx(self):
        self._pending = b""

    def close(self):
        pass

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        self.t += READ_S
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = _id_reply()
        else:
            r = self.by_frame[data]  # anything else is not a read: KeyError fails the test
            self.reads += 1
            self._pending = _reply(r, self.v[(r.axis, r.var)])
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out


PHRASE = g17.confirm_phrase(PORT.device)


def go(chain, **kw):
    args = {"send": True, "on_for_2_min": True, "confirm": PHRASE} | kw
    return g17.run(find_ports=lambda: [PORT], open_port=lambda d: chain, monotonic=chain.monotonic,
                   sleep=chain.sleep, **args)


def test_only_reads_exist_and_addresses_are_the_cfg_ones():
    for rs in g17.READS.values():
        for r in rs.values():
            assert r.frame[3:5] in (b"\xb0\x04", b"\xb0\x05")  # 16/32-bit reads only
            assert r.frame[2] == r.axis_id << 4
    assert g17.READS["Z"]["LCONVERGEBAND"].address == 0x03D3 and g17.READS["Z"]["CPOSCMD"].bits == 32
    assert g17.READS["O"]["ITARGETPOS"].address == 0x03CC and g17.READS["O"]["ITARGETPOS"].bits == 16
    assert g17.READS["O"]["ICURRENTPOSHF"].address == 0x03CB


def test_dry_run_and_refusals_open_nothing():
    opened = []
    res = g17.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == [] and len(res.exchanges) == sum(len(r) for r in g17.READS.values())
    c = Chain()
    assert go(c, on_for_2_min=False).outcome == "refused" and c.writes == []
    assert go(c, confirm="READ Z ON COM3").outcome == "refused" and c.writes == []


def test_healthy_unit_passes_with_reads_only():
    c = Chain()
    res = go(c)
    assert res.outcome == "pass", res.message
    assert "O at position 2" in res.message
    assert len(res.final["samples"]["Z"]) == g17.REPEATS
    assert all(w == FF or w == mg1.ID_QUERY or w in c.by_frame for w in c.writes)  # nothing but reads
    assert any("objective position 2" in n for n in res.final["notes"])


def test_constant_mismatch_and_unknown_turret_position_fail():
    res = go(Chain({("Z", "LCONVERGEBAND"): 10, ("O", "ICURRENTPOSHF"): 0}))
    assert res.outcome == "fail"
    assert "Z LCONVERGEBAND = 10, decoded 5" in res.message and "O position sensor reads 0" in res.message


def test_z_limit_must_match_its_homing_result():
    res = go(Chain({("Z", "LPOSMIN"): -1700}))
    assert res.outcome == "fail" and "-LHOMEINDEXDELTA" in res.message


def test_o_sensor_code_must_decode_to_its_position():
    res = go(Chain({("O", "IHF"): 4}))  # 4 decodes to position 1, the drive says 2
    assert res.outcome == "fail" and "does not decode" in res.message


def test_turret_power_on_at_rest_is_a_note_not_a_failure():
    res = go(Chain({("O", "SRL"): 0xC400}))
    assert res.outcome == "pass" and any("power stage ON" in n for n in res.final["notes"])
