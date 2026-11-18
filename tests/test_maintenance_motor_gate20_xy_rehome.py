"""Motor Gate 20 (ADR-0058): X/Y software re-home with ESTATE 19, against a fake chain that follows
STATE_19_HOME_DOUBLE (XAxis.sw). No hardware."""

from __future__ import annotations

import json

import pytest
from test_maintenance_motor_gate7_xy_limits import PORT
from test_maintenance_motor_gate8_t1_runs import FF, NORMAL, READ_S, _id_reply, _reply

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate20_xy_rehome as g20
from openevos.safety.firmware import REVIEWED_MAINTENANCE

CAL = {"X": (-1000, 269000), "Y": (-1500, 175600)}


class HomeChain:
    """Homed, idle, calibrated limits; ESTATE 19 makes the axis home over `home_polls` reads of its ESTATEV.
    `fault`: None | "overcurrent" (ends with bits 0+3, HOMEDONE 0) | "stuck" (never finishes) | "ignore" (no start)."""

    def __init__(self, overrides=None, home_polls=4, fault=None):
        self.t, self.writes, self._pending = 0.0, [], b""
        self.by_frame = {r.frame: r for rs in g20.READS.values() for r in rs.values()}
        self.by_frame |= {r.frame: r for r in (*g4.READINESS, *g4.ESTATEV_ALL)}
        self.by_frame |= {g20.ZR[n].frame: g20.ZR[n] for n in ("APOS", "CPOS", "ESTATEV")}
        self.v = {(r.axis, r.var): 0 for r in self.by_frame.values()}
        for r in g4.READINESS:
            if r.var == "IINITONCEDONE":
                self.v[(r.axis, r.var)] = 12345
        for ax, (lo, hi) in CAL.items():
            self.v |= {(ax, "IINITONCEDONE"): 12345, (ax, "HOMEDONE"): 1, (ax, "ISTATUSFLAGS"): 0x1000,
                       (ax, "SRL"): 0xC400, (ax, "SRH"): 0x033F, (ax, "LPOSMIN"): lo, (ax, "LPOSMAX"): hi,
                       (ax, "CSPDCMD"): 15 * 65536, (ax, "APOS"): 40000, (ax, "CPOS"): 40000,
                       (ax, "LHOMEINDEXDELTA"): 1234}
        self.v |= overrides or {}
        self.home_polls, self.fault = home_polls, fault
        self.homing: dict[str, int] = {}

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

    def _step(self, ax):
        left = self.homing.get(ax)
        if left is None:
            return
        if self.fault == "stuck":
            return
        if left > 0:
            self.homing[ax] = left - 1
            self.v[(ax, "APOS")] = self.v[(ax, "APOS")] // 2
            return
        del self.homing[ax]
        v = self.v
        if self.fault == "overcurrent":
            v[(ax, "ISTATUSFLAGS")] |= 0x0009
        else:
            v[(ax, "APOS")] = v[(ax, "CPOS")] = 0
            v[(ax, "HOMEDONE")] |= 1
            v[(ax, "SRH")] = 0x033F
        v[(ax, "ESTATEV")] = 0

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        self.t += READ_S
        if data == FF:
            self._pending = b"\x0d"
        elif data == mg1.ID_QUERY:
            self._pending = _id_reply()
        elif data in self.by_frame:
            r = self.by_frame[data]
            if r.var == "ESTATEV":
                self._step(r.axis)
            self._pending = _reply(r, self.v[(r.axis, r.var)])
        else:
            assert data in g20.ALLOWED_WRITES, data.hex(" ")  # anything else fails the test
            ax = "X" if data == g20.H1_X_HOME else "Y"
            self._pending = b"O"
            if self.fault != "ignore":
                v = self.v
                v[(ax, "ESTATEV")] = 19
                v[(ax, "HOMEDONE")] &= ~1
                v[(ax, "ISTATUSFLAGS")] &= 0x1FFE
                v[(ax, "SRH")] = 0x013F
                v[(ax, "SRL")] = 0xC400
                self.homing[ax] = self.home_polls
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out


PHRASE = g20.confirm_phrase(PORT.device)


def go(chain, **kw):
    args = {"send": True, "on_for_2_min": True, "stage_clear_watching": True, "confirm": PHRASE} | kw
    return g20.run(find_ports=lambda: [PORT], open_port=lambda d: chain, monotonic=chain.monotonic,
                   sleep=chain.sleep, **args)


def _sent(c):
    return [w for w in c.writes if w in g20.ALLOWED_WRITES]


def test_frames_are_the_vendor_bytes_and_registered():
    assert g20.H1_X_HOME == bytes.fromhex("06 00 10 21 B3 00 13 FD")
    assert g20.H2_Y_HOME == bytes.fromhex("06 00 20 21 B3 00 13 0D")
    entry = REVIEWED_MAINTENANCE["motor.xy_rehome"]
    assert entry["module"] == "maintenance/motor_gate20_xy_rehome.py"
    assert {int.from_bytes(f[3:5], "big") for f in g20.ALLOWED_WRITES} == entry["commands"]
    assert {f[2] for f in g20.ALLOWED_WRITES} == {0x10, 0x20}
    with pytest.raises(g4.MotorGate4Refused):
        g20.home_frame("Z")


def test_home_frames_are_not_app_motion_writes():
    from openevos.backends.technosoft import protocol as tp
    from openevos.backends.technosoft import transport as tr

    for f in g20.ALLOWED_WRITES:
        assert not tp.is_motion_write(f)
        with pytest.raises(tp.ProtocolRefused):
            tr.check_write(f, allow_motion=True)


def test_dry_run_and_missing_confirmations_open_nothing():
    opened = []
    res = g20.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d))
    assert res.outcome == "dry_run" and [e.request_hex for e in res.exchanges] == [
        g20.H1_X_HOME.hex(" "), g20.H2_Y_HOME.hex(" ")]
    for kw in ({"on_for_2_min": False}, {"stage_clear_watching": False}, {"confirm": "REHOME X AND Y ON COM9"},
               {"axes": ("Y", "X")}, {"axes": ("Z",)}):
        args = {"send": True, "on_for_2_min": True, "stage_clear_watching": True, "confirm": PHRASE} | kw
        res = g20.run(find_ports=lambda: [PORT], open_port=lambda d: opened.append(d), **args)
        assert res.outcome == "refused"
    assert opened == []


def test_pass_homes_x_then_y_and_sends_only_the_two_frames():
    c = HomeChain()
    res = go(c)
    assert res.outcome == "pass", res.message
    assert _sent(c) == [g20.H1_X_HOME, g20.H2_Y_HOME]
    # Y's home is sent only after X's final reads
    x_done = max(i for i, w in enumerate(c.writes) if w == g20.READS["X"]["LHOMEINDEXDELTA"].frame)
    assert c.writes.index(g20.H2_Y_HOME) > x_done
    assert all(res.final[ax]["checks"].values() for ax in "XY")
    assert res.legs["X"]["started_s"] is not None and res.legs["Y"]["done_s"] > 0
    json.loads(res.to_json())


def test_single_axis():
    c = HomeChain()
    res = go(c, axes=("Y",), confirm=g20.confirm_phrase(PORT.device, ("Y",)))
    assert res.outcome == "pass" and _sent(c) == [g20.H2_Y_HOME]


def test_overcurrent_tripped_axis_off_is_allowed_and_cleared():
    # the 2026-11-18 Y lock: power stage off, ISTATUSFLAGS 0x1001; MER 0 -> may home (the drive does AXISON itself)
    c = HomeChain({("Y", "SRL"): 0x0400, ("Y", "ISTATUSFLAGS"): 0x1001, ("Y", "IIQMIN"): -17715})
    res = go(c)
    assert res.outcome == "pass", res.message


@pytest.mark.parametrize(("over", "why"), [
    ({("Y", "MER"): 0x0100}, "MER"),
    ({("X", "SRH"): 0x833F}, "SRH fault"),
    ({("Z", "APOS"): 20000}, "Z not at home"),
])
def test_baseline_refusals_send_nothing(over, why):
    c = HomeChain(over)
    res = go(c)
    assert res.outcome == "baseline_refused" and why in res.message
    assert _sent(c) == []


def test_not_idle_sends_nothing():
    c = HomeChain({("Y", "ESTATEV"): 13})
    assert go(c).outcome == "not_idle" and _sent(c) == []


def test_overcurrent_during_home_stops_after_x_and_never_homes_y():
    c = HomeChain(fault="overcurrent")
    res = go(c)
    assert res.outcome == "fault_stopped" and "HOMEDONE" in res.message
    assert _sent(c) == [g20.H1_X_HOME]


def test_stuck_home_times_out_with_nothing_more_sent():
    c = HomeChain(fault="stuck")
    res = go(c)
    assert res.outcome == "fault_stopped" and "still homing" in res.message
    assert _sent(c) == [g20.H1_X_HOME]


def test_request_ignored_is_reported():
    c = HomeChain(fault="ignore")
    res = go(c)
    assert res.outcome == "fault_stopped" and "did not start" in res.message
    assert _sent(c) == [g20.H1_X_HOME]


def test_limits_changed_fails():
    class Reset(HomeChain):
        def _step(self, ax):
            ending = self.homing.get(ax) == 0
            super()._step(ax)
            if ending:  # as if VAR_INIT had run
                self.v[(ax, "LPOSMAX")] = 50000

    c = Reset()
    res = go(c)
    assert res.outcome == "fail" and "X limits_unchanged" in res.message
    assert _sent(c) == [g20.H1_X_HOME]


def test_main_writes_evidence_for_every_run(tmp_path, capsys):
    rc = g20.main(["--evidence-dir", str(tmp_path)], find_ports=lambda: [PORT])
    assert rc == 0 and len(list(tmp_path.glob("motor-gate20-*-dry_run.json"))) == 1
    rc = g20.main(["--send", "--evidence-dir", str(tmp_path)], find_ports=lambda: [PORT])
    assert rc == 1 and len(list(tmp_path.glob("motor-gate20-*-refused.json"))) == 1
