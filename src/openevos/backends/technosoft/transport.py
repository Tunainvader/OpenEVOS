"""Serial transports for read-only motor telemetry (ADR-0023).

* `WinSerialPort`: opens the drives' FTDI port exactly like the vendor and Motor Gate 1 rev 2
  (exclusive open; read back the DCB and refuse unless DTR/RTS control are ENABLE with no flow control;
  change only baud/8/N/2; vendor timeouts). `write()` passes only allowlisted read requests.
* `SimulatedDriveChain`: an in-memory drive chain answering the same requests, for tests and demos.

Port discovery uses only the Windows registry's SERIALCOMM map and the FTDI enumerator key (read-only).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Protocol

from openevos.backends.technosoft import protocol as tp


class Port(Protocol):
    name: str

    def configure(self, baud: int) -> None: ...
    def purge_rx(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, n: int) -> bytes: ...
    def close(self) -> None: ...


class TransportError(RuntimeError):
    pass


def check_write(data: bytes, allow_motion: bool, extra: frozenset = frozenset()) -> None:
    """Last gate before the wire: telemetry reads always; the reviewed X/Y motion writes only when the
    session enabled motor control (ADR-0024); `extra` = exact frames the stage service registered for one
    operation (the validated Gate 7 limit writes, ADR-0056), and only with motor control. Anything else raises."""
    if data in tp.ALLOWED_REQUESTS:
        return
    if allow_motion and tp.is_motion_write(data):
        return
    if allow_motion and data in extra:
        return
    raise tp.ProtocolRefused(f"request {data.hex(' ')} is not allowed on this port")


# ---------------------------------------------------------------------------------- Windows
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _DCB(ctypes.Structure):
        _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("wReserved", wintypes.WORD), ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                    ("ByteSize", ctypes.c_ubyte), ("Parity", ctypes.c_ubyte), ("StopBits", ctypes.c_ubyte),
                    ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                    ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char), ("wReserved1", wintypes.WORD)]

    class _TIMEOUTS(ctypes.Structure):
        _fields_ = [("ReadIntervalTimeout", wintypes.DWORD), ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                    ("ReadTotalTimeoutConstant", wintypes.DWORD), ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                    ("WriteTotalTimeoutConstant", wintypes.DWORD)]

    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    for _f, _a in (("GetCommState", ctypes.POINTER(_DCB)), ("SetCommState", ctypes.POINTER(_DCB)),
                   ("GetCommTimeouts", ctypes.POINTER(_TIMEOUTS)), ("SetCommTimeouts", ctypes.POINTER(_TIMEOUTS))):
        getattr(_k32, _f).argtypes = [wintypes.HANDLE, _a]
    _k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                               ctypes.c_void_p]
    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                              ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _INVALID = ctypes.c_void_p(-1).value


def ftdi_com_ports() -> list[str]:
    """COM names of present FTDI 0403:6001 serial ports (registry, read-only)."""
    if sys.platform != "win32":
        return []
    import winreg

    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\FTDIBUS") as root:
            i = 0
            while True:
                try:
                    dev = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if "VID_0403+PID_6001" not in dev.upper():
                    continue
                with winreg.OpenKey(root, dev) as dk:
                    j = 0
                    while True:
                        try:
                            inst = winreg.EnumKey(dk, j)
                        except OSError:
                            break
                        j += 1
                        try:
                            with winreg.OpenKey(dk, inst + r"\Device Parameters") as pk:
                                names.append(winreg.QueryValueEx(pk, "PortName")[0])
                        except OSError:
                            pass
    except OSError:
        return []
    present = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as sk:
            k = 0
            while True:
                try:
                    present.add(winreg.EnumValue(sk, k)[1])
                except OSError:
                    break
                k += 1
    except OSError:
        pass
    return sorted(n for n in set(names) if n in present)


class WinSerialPort:
    """Vendor-identical port setup (Motor Gate 1 rev 2); only allowlisted reads can be written."""

    def __init__(self, name: str, *, allow_motion: bool = False):
        self.allow_motion = allow_motion
        self.extra_writes: frozenset = frozenset()  # set by the stage service for one operation only
        if sys.platform != "win32":
            raise TransportError("the Technosoft serial transport needs Windows")
        if not name.upper().startswith("COM"):
            raise TransportError(f"not a COM port: {name!r}")
        self.name = name
        h = _k32.CreateFileW("\\\\.\\" + name, 0xC0000000, 0, None, 3, 0, None)
        if h == _INVALID:
            raise TransportError(f"cannot open {name} exclusively (error {ctypes.get_last_error()}); "
                                 "is PearlScope or a maintenance tool using it?")
        self._h = h
        try:
            dcb = self._dcb()
            flags = dcb.flags
            dtr, rts = (flags >> 4) & 3, (flags >> 12) & 3
            flow = flags & (0b1100 | (1 << 6) | (1 << 8) | (1 << 9))
            if dtr != 1 or rts != 1 or flow:
                raise TransportError(f"{name} driver state not normal (DTR {dtr}, RTS {rts}, flow 0x{flow:X}); "
                                     "power-cycle the instrument")
        except Exception:
            self.close()
            raise

    def _dcb(self):
        dcb = _DCB()
        dcb.DCBlength = ctypes.sizeof(_DCB)
        if not _k32.GetCommState(self._h, ctypes.byref(dcb)):
            raise TransportError("GetCommState failed")
        return dcb

    def configure(self, baud: int) -> None:
        if baud not in (115200, 9600):
            raise TransportError("only the vendor rates")
        dcb = self._dcb()
        dcb.BaudRate, dcb.ByteSize, dcb.Parity, dcb.StopBits = baud, 8, 0, 2
        if not _k32.SetCommState(self._h, ctypes.byref(dcb)):
            raise TransportError("SetCommState failed")
        to = _TIMEOUTS()
        _k32.GetCommTimeouts(self._h, ctypes.byref(to))
        to.ReadIntervalTimeout, to.ReadTotalTimeoutMultiplier, to.ReadTotalTimeoutConstant = 1000, 10, 10
        # Bounded writes (2026-11-13): with no write timeout a stalled USB link blocked a write forever, so the
        # app could not exit and COM3 stayed held even after the process was killed. 10 bytes take ~1 ms.
        to.WriteTotalTimeoutMultiplier, to.WriteTotalTimeoutConstant = 10, 250
        if not _k32.SetCommTimeouts(self._h, ctypes.byref(to)):
            raise TransportError("SetCommTimeouts failed")

    def purge_rx(self) -> None:
        _k32.PurgeComm(self._h, 0x0008)

    def write(self, data: bytes) -> int:
        check_write(bytes(data), self.allow_motion, self.extra_writes)
        n = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        if not _k32.WriteFile(self._h, buf, len(data), ctypes.byref(n), None):
            raise TransportError("WriteFile failed")
        if n.value != len(data):
            raise TransportError(f"write timed out ({n.value} of {len(data)} bytes sent)")
        return n.value

    def read(self, n: int) -> bytes:
        got = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(n)
        if not _k32.ReadFile(self._h, buf, n, ctypes.byref(got), None):
            raise TransportError("ReadFile failed")
        return buf.raw[:got.value]

    def close(self) -> None:
        if getattr(self, "_h", None) not in (None, _INVALID):
            # abort anything still pending in the driver (PURGE_TXABORT | PURGE_RXABORT | TXCLEAR | RXCLEAR) so the
            # handle - and COM3 - is really released
            _k32.PurgeComm(self._h, 0x000F)
            _k32.CloseHandle(self._h)
            self._h = None


# ---------------------------------------------------------------------------------- simulation
class SimulatedDriveChain:
    """In-memory six-drive chain answering telemetry reads. `values[(axis, var)]` is mutable."""

    name = "SIM"

    def __init__(self, values: dict | None = None, *, allow_motion: bool = False, moves_complete: bool = True):
        self.allow_motion = allow_motion
        self.moves_complete = moves_complete  # False: a move never finishes (timeout tests)
        self.speed_counts_s: float | None = None  # None: moves complete at once (tests)
        self._ramps: dict[str, tuple[float, int, int]] = {}
        self.drift_on_move: dict[str, int] = {}
        self.wrong_way: set[str] = set()  # axis -> counts its encoder shifts when the OTHER axis moves
        self._lock = threading.Lock()
        self.values: dict[tuple[str, str], int] = {}
        for ax, vs in tp.VARS.items():
            for name in vs:
                # SRL / ISTATUSFLAGS: what Gate 9 read on a healthy X and Y (power stage on, settled)
                self.values[(ax, name)] = {"IINITONCEDONE": tp.INIT_DONE_MARKER, "SRH": 0x033F,
                                           "LPOSMAX": 50000, "CSPDCMD": tp.POWER_UP_SPEED_RAW,
                                           "SRL": 0xC400, "ISTATUSFLAGS": 0xD000}.get(name, 0)
        # F carriage: the stored positions, limits and band the drive held on 2026-11-13 (Gates 9/11/12), at slot 1
        self.values.update({("F", "LPOSMIN"): 0, ("F", "LPOSMAX"): 128000, ("F", "LCONVERGEBAND"): 5,
                            ("F", "LMIRROR1POS"): tp.F_EXPECTED_POS["mirror"], ("F", "ISTATUSFLAGS"): 0x4000,
                            **{("F", f"LCUBE{n}POS"): tp.F_EXPECTED_POS[f"slot{n}"] for n in tp.F_SLOTS}})
        self.f_moves_complete = True  # False: F never converges (timeout tests)
        # Z (Gate 17 values) and the objective turret: at position 1 (4X), switched off at rest
        self.values.update({("Z", "LPOSMIN"): -1779, ("Z", "LPOSMAX"): 67000, ("Z", "LCONVERGEBAND"): 5,
                            ("O", "ICURRENTPOSHF"): 1, ("O", "IHF"): 4, ("O", "ITARGETPOS"): 1, ("O", "SRL"): 0x4400,
                            ("O", "ISTATUSFLAGS"): 0x1000})
        # X/Y software re-home (ADR-0058, ESTATE 19): finishes after `home_reads` reads; `home_fault` "overcurrent"
        # ends it interrupted (ISTATUSFLAGS bits 0+3, HOMEDONE 0), "stuck" never ends, "ignore" never starts
        self.home_reads = 3
        self.home_fault: str | None = None
        #: tests: these axes end their homing this many counts short of 0, command 0 (Y since 2026-11-19: 14-31)
        self.home_standoff: dict[str, int] = {}
        self._homing: dict[str, int] = {}
        for ax in tp.MOTION_AXES:
            self.values[(ax, "HOMEDONE")] = 1
        #: tests: knock these still axes by n counts when the next move of another axis finishes (once)
        self.knock: dict[str, int] = {}
        #: tests (ADR-0061): the next moves of an axis end these many counts off their target (signed, one per move)
        #: with motion complete; they settle onto the target after `land_settle_reads` reads (None: they stay, as Y
        #: did at 65.6 mm on 2026-11-21). A stop at rest ends the settling where the axis stands.
        self.land_off: dict[str, list[int]] = {}
        self.land_settle_reads: int | None = None
        self._landing: dict[str, list[int]] = {}  # axis -> [reads left, target]
        self.o_steps_per_read = 0  # 0: a turret change completes at once; n: one position per n reads (tests)
        self.o_stuck = False
        self._o_turn: list | None = None  # [reads left, start]
        self.values.update(values or {})
        self._by_frame = {v.frame: v for vs in tp.VARS.values() for v in vs.values()}
        self._pending = b""
        self.writes: list[bytes] = []
        self.closed = False
        self.extra_writes: frozenset = frozenset()

    def configure(self, baud: int) -> None:
        pass

    def purge_rx(self) -> None:
        self._pending = b""

    def _motion(self, data: bytes) -> None:
        """Model the drive program: target write, move request (clamped to limits), controlled stop."""
        if data[2] == tp.F_ID << 4:
            self._f_motion(data)
            return
        if data[2] in (tp.Z_ID << 4, tp.O_ID << 4):
            self._zo_motion(data)
            return
        axis = "X" if data[2] == 0x10 else "Y"
        if data == tp.XY_HOME_FRAMES[axis]:  # STATE_19_HOME_DOUBLE (Gate 20)
            if self.home_fault != "ignore":
                vals = self.values
                vals[(axis, "ESTATEV")], vals[(axis, "SRH")] = tp.HOME_STATE, 0x013F
                vals[(axis, "HOMEDONE")] &= ~1
                vals[(axis, "ISTATUSFLAGS")] &= 0x1FFE
                vals[(axis, "SRL")] |= 0x8000  # HOMETOHARDSTOP: AXISON
                self._ramps.pop(axis, None)
                self._homing[axis] = self.home_reads
        elif data[3:5] in (bytes([0x25, 0xB4]), bytes([0x25, 0xB6])):  # LPOSMIN / LPOSMAX (Gate 7)
            v = data[5:9]
            name = "LPOSMIN" if data[4] == 0xB4 else "LPOSMAX"
            self.values[(axis, name)] = int.from_bytes(v[2:4] + v[0:2], "big", signed=True)
        elif data[3:5] == bytes([0x25, 0xD4]):
            v = data[5:9]
            self.values[(axis, "CPOSCMD")] = int.from_bytes(v[2:4] + v[0:2], "big", signed=True)
        elif data[3:5] == bytes([0x25, 0xD6]):  # CSPDCMD (ADR-0032)
            v = data[5:9]
            self.values[(axis, "CSPDCMD")] = int.from_bytes(v[2:4] + v[0:2], "big", signed=True)
        elif data == tp.move_request(axis):
            lo, hi = self.values[(axis, "LPOSMIN")], self.values[(axis, "LPOSMAX")]
            target = min(max(self.values.get((axis, "CPOSCMD"), 0), lo), hi)
            self.values[(axis, "CPOS")] = target
            self.values[(axis, "ESTATEV")] = 13
            if axis in self.wrong_way:  # tests: the axis runs away from its target (Y, 2026-11-13)
                start = self.values[(axis, "APOS")]
                target = start - (target - start) - 500
            for other, counts in self.drift_on_move.items():  # tests: the idle axis' encoder creeps (2026-11-13)
                if other != axis:
                    self.values[(other, "APOS")] += counts
            for other in [a for a in self.knock if a != axis]:  # once: Y's knock while X passes ~33-41 mm (2026-11-18)
                self.values[(other, "APOS")] += self.knock.pop(other)  # the encoder only: the drive's CPOS stays put
            if self.moves_complete and self.speed_counts_s:  # travel at a real speed (manual-control demos)
                self._ramps[axis] = (time.monotonic(), self.values[(axis, "APOS")], target)
                self.values[(axis, "SRH")] = 0x013F
            elif self.moves_complete:
                off = self.land_off[axis].pop(0) if self.land_off.get(axis) else 0
                self.values[(axis, "APOS")] = target + off
                if off and self.land_settle_reads is not None:
                    self._landing[axis] = [self.land_settle_reads, target]
                self.values[(axis, "SRH")] = 0x033F
            else:
                self.values[(axis, "SRH")] = 0x013F
        elif data == tp.stop_frames(axis)[2]:
            self._advance()
            self._ramps.pop(axis, None)
            self._landing.pop(axis, None)
            self.values[(axis, "CPOS")] = self.values[(axis, "APOS")]
            self.values[(axis, "ESTATEV")] = 0
            self.values[(axis, "SRH")] = 0x033F
        self._pending = b"O"

    def _zo_motion(self, data: bytes) -> None:
        """Z: CPOSCMD, ESTATE 8 (move at once, clamped), ESTATE 6 (stop at rest). O: ITARGETPOS, GOTO_POS (positions
        decrease as it turns, as on the unit), AXISOFF."""
        v = self.values
        if data[2] == tp.Z_ID << 4 and data[3:5] == bytes([0x25, 0xB3]):
            w = data[5:9]
            v[("Z", "CPOSCMD")] = int.from_bytes(w[2:4] + w[0:2], "big", signed=True)
        elif data == tp.Z_MOVE_FRAME:
            t = min(max(v.get(("Z", "CPOSCMD"), 0), v[("Z", "LPOSMIN")]), v[("Z", "LPOSMAX")])
            v[("Z", "CPOS")] = v[("Z", "APOS")] = t
            v[("Z", "ESTATEV")], v[("Z", "SRH")] = 13, 0x033F
        elif data == tp.Z_STOP_FRAME:
            v[("Z", "CPOS")] = v[("Z", "APOS")]
            v[("Z", "ESTATEV")], v[("Z", "SRH")] = 0, 0x033F
        elif data[2] == tp.O_ID << 4 and data[3:5] == bytes([0x21, 0xCC]):
            v[("O", "ITARGETPOS")] = int.from_bytes(data[5:7], "big")
        elif data == tp.O_GOTO_FRAME:
            v[("O", "ESTATEV")], v[("O", "IPENDINGMOVES")], v[("O", "SRL")] = 3, 1, 0xC400
            self._o_turn = [self.o_steps_per_read, v[("O", "ICURRENTPOSHF")]]
            self._o_step()
        elif data == tp.O_AXISOFF_FRAME:
            self._o_turn = None
            v[("O", "SRL")] = 0x4400
        self._pending = b"O"

    def _o_step(self) -> None:
        """Advance a turret change: one position per `o_steps_per_read` reads (0 = all at once)."""
        if self._o_turn is None:
            return
        v = self.values
        if self.o_stuck:
            v[("O", "ISTATUSFLAGS")] |= 2
            v[("O", "SRL")], v[("O", "ESTATEV")] = 0x4400, 0
            self._o_turn = None
            return
        hf = {1: 4, 2: 0, 3: 2, 4: 10, 5: 12}
        while True:
            if self._o_turn[0] > 0:
                self._o_turn[0] -= 1
                return
            pos = v[("O", "ICURRENTPOSHF")]
            if pos == v[("O", "ITARGETPOS")]:
                v[("O", "ESTATEV")], v[("O", "IPENDINGMOVES")], v[("O", "SRL")] = 0, 0, 0x4400
                self._o_turn = None
                return
            pos = (pos - 2) % 5 + 1
            v[("O", "ICURRENTPOSHF")], v[("O", "IHF")] = pos, hf[pos]
            self._o_turn[0] = self.o_steps_per_read
            if self.o_steps_per_read:
                return

    def _f_motion(self, data: bytes) -> None:
        """F's program (FAxis.sw): LTARGETPOS = slot, GOTO_CUBE -> stored LCUBEnPOS; GOTO_MIRR -> LMIRROR1POS; STOP."""
        v = self.values
        if data[3:5] == bytes([0x25, 0xB7]):
            w = data[5:9]
            v[("F", "LTARGETPOS")] = int.from_bytes(w[2:4] + w[0:2], "big", signed=True)
        elif data in (tp.F_GOTO_CUBE_FRAME, tp.F_GOTO_MIRROR_FRAME):
            target = (v[("F", "LMIRROR1POS")] if data == tp.F_GOTO_MIRROR_FRAME
                      else v[("F", f"LCUBE{v.get(('F', 'LTARGETPOS'), 1)}POS")])
            v[("F", "CPOS")] = target
            if self.f_moves_complete:
                v[("F", "APOS")] = target
                v[("F", "ISTATUSFLAGS")] = 0x4000
            else:
                v[("F", "ESTATEV")] = 2
                v[("F", "ISTATUSFLAGS")] = 0
        elif data == tp.F_STOP_FRAME:
            v[("F", "CPOS")] = v[("F", "APOS")]
            v[("F", "ESTATEV")] = 0
        self._pending = b"O"

    def write(self, data: bytes) -> int:
        check_write(bytes(data), self.allow_motion, self.extra_writes)
        with self._lock:
            self.writes.append(bytes(data))
            if data not in tp.ALLOWED_REQUESTS:
                self._motion(bytes(data))
            elif data == bytes([tp.SYNC_BYTE]):
                self._pending = bytes([tp.SYNC_REPLY])
            elif data == tp.ID_QUERY:  # the real gateway's reply, byte for byte (Motor Gate 1 record)
                self._pending = bytes.fromhex("4F 0A 00 01 B4 04 00 10 00 00 79 80 CC")
            else:
                self._advance()
                self._land_step()
                self._o_step()
                self._home_step()
                v = self._by_frame[bytes(data)]
                val = self.values[(v.axis, v.name)]
                if v.bits == 16:
                    raw = (val & 0xFFFF).to_bytes(2, "big")
                else:
                    b = int(val).to_bytes(4, "big", signed=True)
                    raw = b[2:4] + b[0:2]
                body = bytes([0x00, 0x11, 0xB4, 0x04 if v.bits == 16 else 0x05, 0x00, v.axis_id << 4,
                              v.address >> 8, v.address & 0xFF]) + raw
                framed = bytes([len(body)]) + body
                self._pending = b"O" + framed + bytes([tp.checksum(framed)])
        return len(data)

    def _land_step(self) -> None:
        """Advance a landing that ended off its target (tests, ADR-0061): onto the target after its reads."""
        for axis in list(self._landing):
            left, target = self._landing[axis]
            if left > 0:
                self._landing[axis][0] = left - 1
                continue
            del self._landing[axis]
            self.values[(axis, "APOS")] = target

    def _home_step(self) -> None:
        """Advance X/Y homing one read: the axis travels towards its hard stop, then ends at 0 (or interrupted)."""
        for axis in list(self._homing):
            if self.home_fault == "stuck":
                continue
            v = self.values
            if self._homing[axis] > 0:
                self._homing[axis] -= 1
                v[(axis, "APOS")] = v[(axis, "APOS")] // 2
                continue
            del self._homing[axis]
            if self.home_fault == "overcurrent":
                v[(axis, "ISTATUSFLAGS")] |= 0x0009
                v[(axis, "SRL")] &= ~0x8000
            else:
                v[(axis, "APOS")], v[(axis, "CPOS")] = self.home_standoff.get(axis, 0), 0
                v[(axis, "HOMEDONE")] |= 1
            v[(axis, "ESTATEV")], v[(axis, "SRH")] = 0, 0x033F

    def _advance(self) -> None:
        """Move ramping axes along at speed_counts_s; finish them at their target."""
        for axis, (t0, start, target) in list(self._ramps.items()):
            scale = self.values[(axis, "CSPDCMD")] / tp.POWER_UP_SPEED_RAW  # the drive's speed setting
            travelled = (time.monotonic() - t0) * (self.speed_counts_s or 0) * scale
            if travelled >= abs(target - start):
                self.values[(axis, "APOS")] = target
                self.values[(axis, "SRH")] = 0x033F
                del self._ramps[axis]
            else:
                self.values[(axis, "APOS")] = start + round(travelled) * (1 if target > start else -1)

    def read(self, n: int) -> bytes:
        with self._lock:
            out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self) -> None:
        self.closed = True
