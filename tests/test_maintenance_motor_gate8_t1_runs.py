"""Motor Gate 8 rev 6 (ADR-0045): T1 Runs C and A against a fake drive chain that moves X on a trapezoid profile
from the CSPDCMD/CACCCMD written (the Probable 0.8 ms scale). Synthetic calibration values only; no hardware."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest
from test_maintenance_motor_gate7_xy_limits import PORT, SYN, cal_file

from openevos.maintenance import motor_gate1 as mg1
from openevos.maintenance import motor_gate4_x_move as g4
from openevos.maintenance import motor_gate8_full_range as g8
from openevos.maintenance import motor_gate8_t1_runs as t1
from openevos.maintenance import motor_gate10_speed as g10
from openevos.maintenance import motor_gate15_y_axisoff as g15
from openevos.safety.firmware import REVIEWED_MAINTENANCE

SPEC = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "fl1_v1.6_motor_gate8_rev6_t1_runs_spec.md"
FF = b"\xff"
READ_S = 0.016
NORMAL = {"fBinary": 1, "fParity": 0, "fOutxCtsFlow": 0, "fOutxDsrFlow": 0, "fDtrControl": 1, "fDsrSensitivity": 0,
          "fTXContinueOnXoff": 0, "fOutX": 0, "fInX": 0, "fErrorChar": 0, "fNull": 0, "fRtsControl": 1,
          "fAbortOnError": 0}
X_FAR = g8.plan_targets(SYN)["X"]


def _reply(r, value):
    b = value.to_bytes(4, "big", signed=True)
    raw = (value & 0xFFFF).to_bytes(2, "big") if r.bits == 16 else b[2:4] + b[0:2]
    body = bytes([0x00, 0x11, 0xB4, 0x04 if r.bits == 16 else 0x05, 0x00, r.axis_id << 4,
                  r.address >> 8, r.address & 0xFF]) + raw
    framed = bytes([len(body)]) + body
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def _id_reply():
    framed = bytes([6, 0x00, 0x01, 0xB0, 0x00, 0x00, 0x10])
    return b"O" + framed + bytes([sum(framed) & 0xFF])


def _val(frame):  # 32-bit write value, vendor order
    return int.from_bytes(frame[7:9] + frame[5:7], "big", signed=True)


class FakeChain:
    """Homed, idle, Gate 7 limits loaded; X moves on a trapezoid at the written speed and acceleration."""

    def __init__(self, *, limits=SYN, accel_scale=1.0, y_never_off=False, y_on_after=None, y_drift_per_leg=0,
                 y_jump_at_leg=None, x_overshoot=0):
        self.t = 0.0
        self.writes, self._pending = [], b""
        self.by_frame = {r.frame: r for r in t1.READS_ALL}
        self.v = {(r.axis, r.var): {"ESTATEV": 0, "IINITONCEDONE": 12345, "SRH": 0x033F, "LCONVERGEBAND": 5,
                                    "CSPDCMD": 0x000F0000, "CACCCMD": 0x10000, "CACCSTOP": 0x10000}.get(r.var, 0)
                  for r in self.by_frame.values()}
        for ax in ("X", "Y"):
            self.v[(ax, "LPOSMIN")], self.v[(ax, "LPOSMAX")] = limits[ax]
            self.v |= {(ax, "SRL"): 0xC400, (ax, "ISTATUSFLAGS"): 0xD000, (ax, "IQ"): 120, (ax, "HOMEDONE"): 1,
                       (ax, "LHOMEINDEXDELTA"): 1234}
        self.accel_scale, self.y_never_off, self.y_on_after = accel_scale, y_never_off, y_on_after
        self.y_drift_per_leg, self.y_jump_at_leg, self.x_overshoot = y_drift_per_leg, y_jump_at_leg, x_overshoot
        self.move = None  # (t0, start, target, v, a)
        self.legs = 0
        self.y_off, self.y_srl_reads = False, 0

    # clock seams
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

    def _advance(self):
        if self.move is None:
            return
        t0, start, target, v, a = self.move
        d, s = abs(target - start), (1 if target > start else -1)
        ta = v / a
        if v * ta >= d:  # triangular
            ta, tc = math.sqrt(d / a), 0.0
        else:
            tc = (d - v * ta) / v
        tt, el = 2 * ta + tc, self.t - t0
        if el >= tt:
            pos, done = d, True
        elif el < ta:
            pos, done = 0.5 * a * el * el, False
        elif el < ta + tc:
            pos, done = 0.5 * a * ta * ta + v * (el - ta), False
        else:
            r = tt - el
            pos, done = d - 0.5 * a * r * r, False
        p = start + s * round(pos)
        self.v[("X", "APOS")] = p + (s * self.x_overshoot if done else 0)
        self.v[("X", "CPOS")] = p
        self.v[("X", "IQ")] = 400 if (el < ta or el > ta + tc) and not done else 60
        if done:
            self.v[("X", "APOS")] = target + s * self.x_overshoot
            self.v[("X", "SRH")] = 0x033F
            self.move = None
            self.legs += 1
            self.v[("Y", "APOS")] += self.y_drift_per_leg
            if self.y_jump_at_leg is not None and self.legs == self.y_jump_at_leg:
                self.v[("Y", "APOS")] += 150

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        self.t += READ_S
        if data == FF:
            self._pending = b"\x0d"
            return 1
        if data == mg1.ID_QUERY:
            self._pending = _id_reply()
            return len(data)
        r = self.by_frame.get(data)
        if r is not None:
            self._advance()
            key = (r.axis, r.var)
            if key == ("Y", "SRL") and self.y_off:
                self.y_srl_reads += 1
                on = self.y_on_after is not None and self.y_srl_reads > self.y_on_after
                self.v[key] = 0xC400 if on else 0x4400
            self._pending = _reply(r, self.v[key])
            return len(data)
        self._pending = b"O"  # a write
        if data == g15.W1_Y_AXISOFF:
            self.y_off = not self.y_never_off
            self.v[("Y", "IQ")] = 0
        elif data[2:5] == bytes([0x10, 0x25, 0xD6]):
            self.v[("X", "CSPDCMD")] = _val(data)
        elif data[2:5] == bytes([0x10, 0x25, 0xD8]):
            self.v[("X", "CACCCMD")] = _val(data)
        elif data[2:5] == bytes([0x10, 0x25, 0xD4]):
            self.v[("X", "CPOSCMD")] = _val(data)
        elif data == g4.M3_ESTATE_MOVE:
            v = self.v[("X", "CSPDCMD")] / 65536 / 0.0008
            a = self.v[("X", "CACCCMD")] / 65536 / 0.0008 ** 2 * self.accel_scale
            self.move = (self.t, self.v[("X", "APOS")], self.v[("X", "CPOSCMD")], v, a)
            self.v[("X", "ESTATEV")], self.v[("X", "SRH")] = 13, 0x013F
        elif data == g4.STOP[-1][1]:  # X ESTATE = 6: stop at rest
            self.move = None
            self.v[("X", "CPOS")] = self.v[("X", "APOS")]
            self.v[("X", "ESTATEV")], self.v[("X", "SRH")] = 0, 0x033F
        return len(data)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def write_frames(self):
        return [w for w in self.writes if w != FF and w != mg1.ID_QUERY and w not in self.by_frame]


OK = {"send": True, "on_for_2_min": True, "power_cycled_gate7_no_move": True, "x_travel_clear": True,
      "watching_main_switch_second": True}


def go(tmp_path, chain=None, run="C", **kw):
    c = chain or FakeChain()
    opened = []
    args = OK | {"confirm": f"RUN T1 {run} X ONLY Y OFF ON COM3"} | kw
    res = t1.run(run_name=run, calibration=cal_file(tmp_path), find_ports=lambda: [PORT],
                 open_port=lambda d: opened.append(d) or c, monotonic=c.monotonic, sleep=c.sleep, **args)
    return res, c, opened


@pytest.fixture
def short_runs(monkeypatch):
    """Two cycles per run keep the simulated time (and the test) short; the per-cycle path is the same."""
    monkeypatch.setitem(t1.RUNS, "C", t1.Run("C", "slow", 190, 1, 2.0))
    monkeypatch.setitem(t1.RUNS, "A", t1.Run("A", "fast", 1573, 2, 10.0))


# ---------------------------------------------------------------------------------- frames
def test_new_acceleration_frames_are_the_specs_and_nothing_else_is_built():
    text = SPEC.read_text(encoding="utf-8")
    for raw, hexs in ((190, "08 00 10 25 D8 00 BE 00 00 D3"), (1573, "08 00 10 25 D8 06 25 00 00 40"),
                      (0x10000, "08 00 10 25 D8 00 00 00 01 16")):
        assert t1.cacc_frame(raw) == bytes.fromhex(hexs) and f"`{hexs}`" in text
    for bad in (1, 0x20000, 5000):
        with pytest.raises(g4.MotorGate4Refused):
            t1.cacc_frame(bad)
    assert t1.SPEED == {"slow": g10.SPEED["slow"], "fast": g10.SPEED["fast"]}  # Gate 10's verified frames
    w = t1.allowed_writes(X_FAR, 4535)
    for bad in ("04 00 20 01 02 27", "04 00 10 00 02 16", "06 00 20 21 B3 00 08 02", g10.SPEED["medium"].hex(" ")):
        assert bytes.fromhex(bad) not in w  # no AXISON, no X AXISOFF, no Y move, no other speed
    assert not any(f[2] == 0x20 for f in w if f != g15.W1_Y_AXISOFF)  # Y gets the AXISOFF and nothing else


def test_registered_as_reviewed_maintenance():
    e = REVIEWED_MAINTENANCE["motor.x_t1_runs_y_off"]
    assert e["module"] == "maintenance/motor_gate8_t1_runs.py" and e["adr"] == "ADR-0045"
    assert {0x0002, 0x25D6, 0x25D8, 0x25D4, 0x21B3} <= e["commands"]


# ---------------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("kw", [{"on_for_2_min": False}, {"power_cycled_gate7_no_move": False},
                                {"x_travel_clear": False}, {"watching_main_switch_second": False},
                                {"confirm": "RUN T1 A X ONLY Y OFF ON COM3"}])
def test_refusals_open_nothing(tmp_path, kw):
    res, c, opened = go(tmp_path, **kw)
    assert res.outcome == "refused" and opened == [] and c.writes == []


def test_dry_run_lists_the_writes_and_opens_nothing(tmp_path):
    res, c, opened = go(tmp_path, send=False)
    assert res.outcome == "dry_run" and opened == [] and c.writes == []
    assert res.exchanges[0].request_hex == "04 00 20 00 02 26" and res.plan["expected_minutes"] > 14


def test_power_up_limits_are_refused_run_gate7_first(tmp_path):
    res, c, _ = go(tmp_path, FakeChain(limits={"X": (0, 50000), "Y": (0, 50000)}))
    assert res.outcome == "baseline_refused" and "Gate 7" in res.message and c.write_frames() == []


def test_y_that_does_not_switch_off_stops_before_any_x_write(tmp_path):
    res, c, _ = go(tmp_path, FakeChain(y_never_off=True))
    assert res.outcome == "y_not_off" and c.write_frames() == [g15.W1_Y_AXISOFF]


# ---------------------------------------------------------------------------------- the runs
def test_run_c_logs_y_drift_through_the_cycles_and_restores_x(tmp_path, short_runs):
    res, c, _ = go(tmp_path, FakeChain(y_drift_per_leg=-3))
    assert res.outcome == "complete", (res.message, res.checks)
    w = c.write_frames()
    assert w[:3] == [g15.W1_Y_AXISOFF, g10.SPEED["slow"], t1.cacc_frame(190)]  # Y off first, then X speed/accel
    assert w[-2:] == [g10.SPEED["fast"], t1.cacc_frame(0x10000)]  # restored last
    assert w.count(g15.W1_Y_AXISOFF) == 1
    assert 0.3 <= res.ramp["ramp_s"] <= 1.0 and res.off_after_ms <= 100
    assert res.legs["c1 home"]["y_total_from_baseline"] == -12  # 4 legs x -3: logged, not stopped
    assert 15 <= res.sample_rate_hz <= 25
    assert res.home_data["Y"]["LHOMEINDEXDELTA"] == 1234 and "power-cycle" in res.message


def test_run_a_uses_the_power_up_speed_and_its_own_acceleration(tmp_path, short_runs):
    res, c, _ = go(tmp_path, run="A")
    assert res.outcome == "complete", res.message
    w = c.write_frames()
    assert g10.SPEED["slow"] not in w and t1.cacc_frame(1573) in w  # fast is already the power-up value
    assert 0.3 <= res.ramp["ramp_s"] <= 1.0 and len([k for k in res.legs if k.startswith("c")]) == 4


def test_a_wrong_acceleration_scale_fails_the_ramp_check_before_any_long_move(tmp_path, short_runs):
    res, c, _ = go(tmp_path, FakeChain(accel_scale=0.1))  # ramps 10x longer than expected
    assert res.outcome == "ramp_check_failed" and not any(k.startswith("c") for k in res.legs)
    assert g8._w(1, X_FAR) not in c.write_frames() and res.restore == {"CSPDCMD": g4.RAW_15_0, "CACCCMD": g4.RAW_1_0}


def test_y_back_on_stops_x_and_restores(tmp_path, short_runs):
    res, c, _ = go(tmp_path, FakeChain(y_on_after=30))
    assert res.outcome == "stopped_y_on" and "came back on" in res.fault
    assert any(f == g4.STOP[-1][1] for f in c.write_frames()[-4:]) or res.final.get("after_fault")
    assert res.restore == {"CSPDCMD": g4.RAW_15_0, "CACCCMD": g4.RAW_1_0}


def test_y_far_from_baseline_stops_the_run(tmp_path, short_runs):
    res, _, _ = go(tmp_path, FakeChain(y_jump_at_leg=3))
    assert res.outcome == "stopped_y_far" and "counts from" in res.fault


def test_landing_outside_the_band_is_a_fault_stop(tmp_path, short_runs):
    res, _, _ = go(tmp_path, FakeChain(x_overshoot=20))
    assert res.outcome == "fault_stopped" and "band" in res.fault


def test_cli_writes_json_and_a_csv_the_correlation_script_reads(tmp_path, short_runs):
    c = FakeChain()
    rc = t1.main(["--run", "C", "--send", "--instrument-on-2min", "--power-cycled-gate7-no-move", "--x-travel-clear",
                  "--watching-main-switch-second-person", "--confirm", "RUN T1 C X ONLY Y OFF ON COM3",
                  "--calibration", str(cal_file(tmp_path)), "--evidence-dir", str(tmp_path)],
                 find_ports=lambda: [PORT], open_port=lambda d: c, monotonic=c.monotonic, sleep=c.sleep)
    assert rc == 0 and len(list(tmp_path.glob("motor-gate8-*-t1c-complete.json"))) == 1
    (path,) = tmp_path.glob("motor-gate8-*-t1c.csv")
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == t1.CSV_COLUMNS and len(rows) > 100
    assert max(int(r["x_apos"]) for r in rows) == X_FAR
