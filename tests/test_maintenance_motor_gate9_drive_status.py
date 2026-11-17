"""Motor Gate 9 (ADR-0030): read-only drive status registers. Fake drive chain only; no real hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_maintenance_motor_gate4_x_move import FF, NORMAL, PORT, id_reply, reply

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate8_full_range as g8
from openevos.maintenance import motor_gate9_drive_status as g9
from openevos.safety.firmware import REVIEWED_L3_READS

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate9_drive_status_spec.md"


class FakeStatus:
    """Answers the ID query and Gate 9's status reads; anything else gets no reply and is recorded."""

    def __init__(self, values=None):
        self.writes, self._pending, self.closed = [], b"", False
        self.by_frame = {r.frame: r for reads in g9.STATUS.values() for r in reads.values()}
        self.values = values or {}

    def inspect(self):
        return {"dcb": {"flags": dict(NORMAL)}, "timeouts": {}}

    def configure(self, baud):
        pass

    def purge_rx(self):
        self._pending = b""

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = id_reply()
        elif data in self.by_frame:
            r = self.by_frame[data]
            v = self.values.get((r.axis, r.var), 0)
            if r.vtype == "INT":
                raw = (v & 0xFFFF).to_bytes(2, "big")
            else:
                b = v.to_bytes(4, "big", signed=True)
                raw = b[2:4] + b[0:2]  # vendor order: low word first
            self._pending = reply(r.axis_id, r.address, raw)
        else:
            self._pending = b""
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.closed = True


def go(fake, **kw):
    args = {"send": True, "confirm": "READ DRIVE STATUS ON COM3", "on_for_2_min": True}
    args.update(kw)
    return g9.run(find_ports=lambda: [PORT], open_port=lambda d: fake, monotonic=lambda: 0.0,
                  sleep=lambda s: None, **args)


def test_frames_addresses_and_registry():
    assert len(g9.STATUS_FRAMES) == 9 * 4 + 2 * len(g9.PROGRAM_VARS) + len(g9.F_PROGRAM_VARS)
    assert {"LCUBE1POS", "LCUBE4POS", "ISTATUSFLAGS"} <= set(g9.STATUS["F"]) and g9.STATUS["F"]["MER"].axis_id == 5
    assert "IMAXPROT" not in g9.STATUS["Z"]  # the program-variable addresses are X/Y only
    x_mer = g9.STATUS["X"]["MER"].frame
    assert x_mer.hex(" ") == "08 00 10 b0 04 00 11 08 fc " + f"{sum(x_mer[:-1]) & 0xFF:02x}"
    assert g9.STATUS["Y"]["SRH"].frame == g4.axis_reads("Y", 2)["SRH"].frame  # same frame as the verified SRH read
    assert g9.STATUS["Y"]["LTARGETPOSV"].frame[3:5] == bytes([0xB0, 0x05])
    for f in g9.STATUS_FRAMES:
        assert f[3] == 0xB0 and f[4] in (0x04, 0x05) and sum(f[:-1]) & 0xFF == f[-1]
    e = REVIEWED_L3_READS["motor.gate9_status_read"]
    assert e["adr"] == "ADR-0030" and e["commands"] == frozenset({0xB000, 0xB004, 0xB005})
    text = SPEC.read_text(encoding="utf-8")
    for var, addr in g9.STATUS_VARS:
        assert f"`{var}`" in text and f"0x{addr:04X}" in text
    for var, _t, addr in g9.PROGRAM_VARS:
        assert f"`{var}`" in text and f"0x{addr:04X}" in text


def test_dry_run_and_refusals_open_nothing():
    opened = []
    res = g9.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and opened == [] and len(res.exchanges) == len(g9.STATUS_FRAMES)
    for kw in ({"on_for_2_min": False}, {"confirm": "READ MOTOR LIMITS ON COM3"}):
        args = {"send": True, "confirm": "READ DRIVE STATUS ON COM3", "on_for_2_min": True, **kw}
        res = g9.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d), **args)
        assert res.outcome == "refused" and opened == []


def test_reads_only_and_decodes():
    fake = FakeStatus({("Y", "MER"): 1 << 6, ("Y", "SRH"): 0x033F, ("Y", "POSERR"): -12, ("Y", "SRL"): 1 << 15})
    res = go(fake)
    assert res.outcome == "complete", res.message
    assert all(w == FF or w == mg1.ID_QUERY or w in g9.STATUS_FRAMES for w in fake.writes)
    y = res.baseline["status"]["Y"]["decoded"]
    assert y["MER"] == "0x0040" and y["MER_set"] == ["LSP limit switch active"]
    assert "target reached / not moving" in y["SRH_set"] and y["POSERR"] == -12
    assert "axis on (power stage enabled)" in y["SRL_set"]
    assert "LSP limit switch active" in res.message and fake.closed
    assert y["IMAXPROT"] == 0 and "IMAXPROT" not in res.baseline["status"]["Z"]["decoded"]


def test_peak_current_against_the_protection_limit():
    fake = FakeStatus({("Y", "IIQMAX"): 9000, ("Y", "IIQMIN"): -17500, ("Y", "IMAXPROT"): 17117,
                       ("Y", "ISTATUSFLAGS"): 0x0001, ("Y", "LTARGETPOSV"): 166571,
                       ("X", "IIQMAX"): 8000, ("X", "IIQMIN"): -8000, ("X", "IMAXPROT"): 17117})
    res = go(fake)
    y, x = res.baseline["status"]["Y"]["decoded"], res.baseline["status"]["X"]["decoded"]
    assert y["peak_current_reached_limit"] is True and y["LTARGETPOSV"] == 166571 and y["ISTATUSFLAGS"] == "0x0001"
    assert x["peak_current_reached_limit"] is False
    assert "Y:" in res.message and "(LIMIT REACHED)" in res.message.split("Y:")[1]


def test_gate8_allows_the_status_reads_but_no_new_writes():
    link = g4._Link(FakeStatus(), g4.MotorGate4Result("x", "", "", ""), lambda: 0.0, lambda s: None,
                    allowed_writes=frozenset(), allowed_reads=g9.STATUS_FRAMES)
    snap = g9.snapshot(link)
    assert set(snap) == {"X", "Y"} and snap["X"]["decoded"]["MER_set"] == []
    with pytest.raises(g4.MotorGate4Refused):
        link.read(g4.XR["APOS"])  # not in this link's allowlist
    assert "g9.STATUS_FRAMES" in Path(g8.__file__).read_text(encoding="utf-8")
