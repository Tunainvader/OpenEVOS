"""Read-only Technosoft telemetry backend (ADR-0023). Simulated drive chain only; no real hardware."""

from __future__ import annotations

import threading
import time

import pytest

from openevos.backends.technosoft import protocol as tp
from openevos.backends.technosoft import telemetry as tm
from openevos.backends.technosoft.transport import SimulatedDriveChain, TransportError
from openevos.core.models import Activity, BackendKind, Connection, FirmwareMode, Health
from openevos.maintenance import (
    motor_gate2 as g2,  # tests may cross-check against the reviewed gate frames
)


def test_frames_match_the_verified_gate_frames():
    gate = {(r.axis, r.var): r.frame for r in g2.READS}
    for (ax, var), frame in gate.items():
        if var in tp.VARS[ax]:
            assert tp.VARS[ax][var].frame == frame, (ax, var)
    assert tp.VARS["X"]["SRH"].frame.hex(" ") == "08 00 10 b0 04 00 11 09 0f f5"  # vendor IsMoving


def test_drift_monitor_reads_are_the_reads_gate_9_made_on_the_unit():
    """ADR-0040 adds X/Y status reads to the app; every one but LCOMMERRPOS was read on the unit by Gate 9."""
    from openevos.maintenance import motor_gate9_drive_status as g9
    reviewed = {n: (16, a) for n, a in g9.STATUS_VARS}
    reviewed.update({n: (16 if t == "INT" else 32, a) for n, t, a in g9.PROGRAM_VARS})
    for ax in ("X", "Y"):
        for name in ("SRL", "MER", "ISTATUSFLAGS", "BDOAXISOFF", "IIQMAX", "IIQMIN", "IQ"):
            v = tp.VARS[ax][name]
            assert (v.bits, v.address) == reviewed[name], (ax, name)
            assert v.frame[3:5] == bytes([0xB0, 0x04])  # a 16-bit read, nothing else
        assert tp.VARS[ax]["LCOMMERRPOS"].bits == 32 and tp.VARS[ax]["LCOMMERRPOS"].address == 0x03BC
    assert "SRL" not in tp.VARS["Z"]  # X/Y program variables only
    # F has its own program's ISTATUSFLAGS at another address (ADR-0051: Gates 11/12 read it)
    assert tp.VARS["F"]["ISTATUSFLAGS"].address == 0x03D2
    assert tp.signed16(0xFFFF) == -1 and tp.signed16(0x7FFF) == 32767


class _SlowLineChain(SimulatedDriveChain):
    """Each read takes as long as on the instrument's line (~15 ms), so the fast log's timing is tested for real."""

    def write(self, data):
        if data != bytes([tp.SYNC_BYTE]):
            time.sleep(0.015)
        return super().write(data)


def test_fast_log_keeps_about_20_hz_when_reads_take_real_time():
    """ADR-0040's ~20 Hz on a line where each read takes time. Run on COM3 (2026-11-16), the first build gave ~10 Hz:
    its 50 ms period was counted from the end of each sample."""
    from openevos.backends.technosoft.stage_monitor import StageMonitor
    chain = _SlowLineChain(allow_motion=True)  # a motion session writes its exit frames on stop
    monitor = StageMonitor()
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=1.0, motion=True, monitor=monitor, fast_log=True)
    svc.start()
    try:
        assert _wait(lambda: monitor.sample_count > 0)
        n0, t0 = monitor.sample_count, time.monotonic()
        time.sleep(3.0)  # spans full slow polls (~40 slow reads each) as well as idle time
        rate = (monitor.sample_count - n0) / (time.monotonic() - t0)
    finally:
        svc.stop()
    assert 15 <= rate <= 21, rate


def test_nothing_but_reads_can_be_sent():
    for bad in (bytes.fromhex("06 00 10 21 B3 00 06 F0"),  # stop request
                bytes.fromhex("08 00 10 25 D4 08 DC 00 00 F5"),  # move target
                bytes.fromhex("04 00 10 00 02 16"),  # AXISOFF
                bytes.fromhex("06 00 10 08 20 00 04 42")):  # baud change
        with pytest.raises(tp.ProtocolRefused):
            tp.assert_allowed(bad)
    chain = SimulatedDriveChain()
    with pytest.raises(tp.ProtocolRefused):
        chain.write(bytes.fromhex("06 00 10 21 B3 00 06 F0"))
    with pytest.raises(tp.ProtocolRefused):
        tp.build_read(7, 16, 0x03EA)


def test_parse_checks_and_vendor_word_order():
    chain = SimulatedDriveChain({("Z", "LPOSMIN"): -1775, ("X", "APOS"): 2268})
    link = tm.DriveLink(chain)
    assert link.open() == 115200
    assert link.read(tp.VARS["Z"]["LPOSMIN"]) == -1775
    assert link.read(tp.VARS["X"]["APOS"]) == 2268
    with pytest.raises(ValueError):
        tp.parse_read_reply(b"O" + bytes(12), 1, 0x03EA, 16)


class _LateSyncChain(SimulatedDriveChain):
    """The first FF's 0D arrives late, together with the second's: two 0D pending (seen on the instrument)."""

    def __init__(self):
        super().__init__()
        self.late = True

    def write(self, data):
        leftover = self._pending  # a real serial line appends: unread bytes stay in front of new ones
        if data == bytes([tp.SYNC_BYTE]) and self.late:
            n = super().write(data)
            self._pending = leftover  # no answer within the read window...
            self.late = False
            self._owed = True
            return n
        n = super().write(data)
        if data == bytes([tp.SYNC_BYTE]) and getattr(self, "_owed", False):
            self._pending = b"\x0d\x0d"  # ...then both answers arrive
            self._owed = False
        self._pending = leftover + self._pending
        return n


def test_a_late_sync_reply_cannot_prefix_the_next_read():
    chain = _LateSyncChain()
    chain.values[("X", "ESTATEV")] = 0
    link = tm.DriveLink(chain)
    assert link.read(tp.VARS["X"]["ESTATEV"]) == 0  # no "first byte 0x0D" failure


class _StraySyncChain(SimulatedDriveChain):
    """A single-attempt sync whose 0D is followed by another late 0D in front of the reply."""

    def write(self, data):
        n = super().write(data)
        if data != bytes([tp.SYNC_BYTE]):
            self._pending = b"\x0d" + self._pending
        return n


def test_a_stray_sync_byte_before_a_reply_is_dropped():
    chain = _StraySyncChain()
    chain.values[("X", "APOS")] = 2268
    assert tm.DriveLink(chain).read(tp.VARS["X"]["APOS"]) == 2268


def test_observation_mapping():
    chain = SimulatedDriveChain({("X", "APOS"): 2268, ("Y", "SRH"): 0x013F, ("Z", "APOS"): 5000})
    values = tm.TelemetryService(lambda: chain, "SIM").poll_once(tm.DriveLink(chain))
    stage = tm.observation(tm.DEVICES[0], values)
    assert stage["activity"] is Activity.MOVING  # Y's motion-complete bit clear
    assert stage["position"].unit == "um" and stage["position"].values[0] == pytest.approx(1000.1, abs=0.1)
    assert stage["attributes"]["homed"] is True and stage["attributes"]["limits_mm"][0][1] == pytest.approx(22.05, 0.01)
    focus = tm.observation(tm.DEVICES[1], values)
    assert focus["activity"] is Activity.IDLE and focus["position"].values[0] == pytest.approx(610.0)
    turret = tm.observation(tm.DEVICES[3], values)
    # ADR-0053: the turret's position is its 5-position sensor (its APOS stays 0 while it turns)
    assert turret["position"].unit == "position" and turret["position"].values == (1.0,)
    assert "homed" not in turret["attributes"] and turret["attributes"]["objective_position"] == 1
    assert stage["firmware_mode"] is FirmwareMode.APPLICATION


class _Ctx:
    def __init__(self):
        self.failed = None

    def fail(self, message, error_code=None):
        self.failed = (message, error_code)


def test_backends_are_physical_read_only():
    svc = tm.TelemetryService(lambda: SimulatedDriveChain(), "SIM")
    for b in svc.backends:
        caps = b.capabilities()
        assert b.backend_kind is BackendKind.PHYSICAL and caps.supported_commands == frozenset()
        ctx = _Ctx()
        b.start(object(), ctx)
        assert ctx.failed[1] == "read_only"


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_service_reports_into_a_reporter_and_stops():
    chain = SimulatedDriveChain({("X", "APOS"): 1134})
    svc = tm.TelemetryService(lambda: chain, "SIM", poll_interval_s=0.02)
    seen = []
    svc.backends[0].attach(lambda **kw: seen.append(kw) or True)
    svc.start()
    assert _wait(lambda: any(k.get("position") is not None for k in seen))
    svc.stop()
    seqs = [k["seq"] for k in seen]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seen[-1]["connection"] is Connection.DISCONNECTED
    assert all(w == bytes([tp.SYNC_BYTE]) or w in tp.ALLOWED_REQUESTS for w in chain.writes)


def test_unavailable_port_reports_disconnected():
    def nope():
        raise TransportError("no port")

    svc = tm.TelemetryService(nope, "COM9", poll_interval_s=0.02)
    seen = []
    svc.backends[1].attach(lambda **kw: seen.append(kw) or True)
    svc.start()
    assert _wait(lambda: any(k.get("connection") is Connection.DISCONNECTED for k in seen))
    svc.stop()
    assert "no port" in svc.last_error


def test_instrument_with_simulated_telemetry():
    from openevos.core.instrument import InstrumentConfig, build_instrument

    cfg = InstrumentConfig(backend_options={"seed": 1, "time_scale": 0.0},
                           telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02}})
    inst = build_instrument(cfg)
    try:
        def ready():
            s = inst.gateway.core_snapshot().get("motor.stage")
            return s is not None and s.observed_position is not None and s.connection is Connection.CONNECTED

        assert _wait(ready)
        snaps = inst.gateway.core_snapshot()
        assert {"motor.stage", "motor.focus", "motor.cube_carriage", "motor.objective_turret",
                "motor.phase_wheel"} <= set(snaps)
        st = snaps["motor.stage"]
        assert st.health is Health.NORMAL and st.firmware_mode is FirmwareMode.APPLICATION
        assert st.attr("read_only") is True and st.attr("homed") is True
        assert "xy" in snaps  # the simulated optics are still there
    finally:
        inst.close()
    assert not any(t.is_alive() for t in threading.enumerate() if t.name == "technosoft-telemetry")


def test_unknown_telemetry_source_refused():
    from openevos.core.instrument import BackendUnavailable, InstrumentConfig, build_instrument

    with pytest.raises(BackendUnavailable):
        build_instrument(InstrumentConfig(backend_options={"time_scale": 0.0}, telemetry={"nope": {}}))


def test_a_waiting_move_or_stop_interrupts_the_status_poll():
    chain = SimulatedDriveChain()
    svc = tm.TelemetryService(lambda: chain, "SIM", motion=True)
    link = tm.DriveLink(chain)
    assert svc.poll_once(link, interruptible=True) is not None
    svc._jobs.put(object())
    n = len(chain.writes)
    assert svc.poll_once(link, interruptible=True) is None and len(chain.writes) == n  # nothing read
    svc._jobs.get_nowait()
    svc._stop_request.set()
    assert svc.poll_once(link, interruptible=True) is None
    assert svc.poll_once(link) is not None  # the plain poll is unchanged


def test_reads_sync_once_and_resync_only_after_a_bad_reply():
    chain = SimulatedDriveChain({("X", "APOS"): 2268})
    link = tm.DriveLink(chain)
    for _ in range(5):
        assert link.read(tp.VARS["X"]["APOS"]) == 2268
    assert chain.writes.count(bytes([tp.SYNC_BYTE])) == 1  # one sync for five reads

    class _Glitch(SimulatedDriveChain):
        bad = 1

        def write(self, data):
            n = super().write(data)
            if data not in (bytes([tp.SYNC_BYTE]),) and self.bad:
                self.bad -= 1
                self._pending = b"O" + bytes(12)  # corrupt reply once
            return n

    g = _Glitch({("X", "APOS"): 2268})
    link = tm.DriveLink(g)
    assert link.read(tp.VARS["X"]["APOS"]) == 2268  # retried after a resync
    assert g.writes.count(bytes([tp.SYNC_BYTE])) == 2
