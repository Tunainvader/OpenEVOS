"""Minimal Win32 serial port for Motor Gate 1 (ADR-0016, spec revision 2).

Copies the vendor's `MotorController64.dll` `FUN_180041560` step by step:
* `CreateFileW("\\\\.\\COMn", GENERIC_READ|GENERIC_WRITE, share 0, OPEN_EXISTING)`; nothing else on open;
* a zeroed DCB with `DCBlength = 0x1C`, filled by `GetCommState`, so the driver's flags (DTR/RTS
  control) are KEPT, then only BaudRate, ByteSize = 8, Parity = none and StopBits = 2 are changed;
* `GetCommTimeouts`, then ReadIntervalTimeout 1000, ReadTotalTimeoutMultiplier 10, ReadTotalTimeoutConstant 10.

Revision 1 used a zero-filled DCB (DTR/RTS disabled). That misread the vendor, and its one run got no
reply (spec §11). `inspect()` reads the current state without changing anything.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("openevos.maintenance.win_serial needs Windows")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PURGE_RXCLEAR = 0x0008
TWOSTOPBITS = 2
VENDOR_BAUDS = (115200, 9600)  # the only rates configure() accepts (vendor's pass order)


class DCB(ctypes.Structure):
    _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("wReserved", wintypes.WORD), ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                ("ByteSize", ctypes.c_ubyte), ("Parity", ctypes.c_ubyte), ("StopBits", ctypes.c_ubyte),
                ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char), ("wReserved1", wintypes.WORD)]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [("ReadIntervalTimeout", wintypes.DWORD), ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                ("ReadTotalTimeoutConstant", wintypes.DWORD), ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                ("WriteTotalTimeoutConstant", wintypes.DWORD)]


assert ctypes.sizeof(DCB) == 0x1C  # the vendor sets DCBlength = 0x1C

_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
_k32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
_k32.GetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
_k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
_k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                           ctypes.c_void_p]
_k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                          ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

# DCB bit-field layout (winbase.h), in declaration order: (name, width)
_FLAG_FIELDS = (("fBinary", 1), ("fParity", 1), ("fOutxCtsFlow", 1), ("fOutxDsrFlow", 1), ("fDtrControl", 2),
                ("fDsrSensitivity", 1), ("fTXContinueOnXoff", 1), ("fOutX", 1), ("fInX", 1), ("fErrorChar", 1),
                ("fNull", 1), ("fRtsControl", 2), ("fAbortOnError", 1))


class SerialError(RuntimeError):
    pass


def decode_flags(flags: int) -> dict[str, int]:
    out, shift = {}, 0
    for name, width in _FLAG_FIELDS:
        out[name] = (flags >> shift) & ((1 << width) - 1)
        shift += width
    return out


def describe_dcb(dcb: DCB) -> dict:
    return {"raw_hex": bytes(dcb).hex(" "), "BaudRate": dcb.BaudRate, "ByteSize": dcb.ByteSize,
            "Parity": dcb.Parity, "StopBits": dcb.StopBits, "flags": decode_flags(dcb.flags),
            "XonLim": dcb.XonLim, "XoffLim": dcb.XoffLim}


def vendor_line_settings(read_back: DCB, baud: int) -> DCB:
    """The vendor's change: copy the read-back DCB and set only the four line fields."""
    if baud not in VENDOR_BAUDS:
        raise SerialError(f"baud {baud} is not one of the vendor's pass rates {VENDOR_BAUDS}")
    dcb = DCB.from_buffer_copy(bytes(read_back))
    dcb.BaudRate = baud
    dcb.ByteSize = 8
    dcb.Parity = 0
    dcb.StopBits = TWOSTOPBITS
    return dcb


def vendor_timeouts(read_back: COMMTIMEOUTS) -> COMMTIMEOUTS:
    to = COMMTIMEOUTS.from_buffer_copy(bytes(read_back))
    to.ReadIntervalTimeout, to.ReadTotalTimeoutMultiplier, to.ReadTotalTimeoutConstant = 1000, 10, 10
    return to


class WinSerialPort:
    """Opening changes nothing; `inspect()` only reads; `configure()` applies the vendor's change."""

    def __init__(self, port_name: str):
        if not port_name.upper().startswith("COM"):
            raise SerialError(f"not a COM port name: {port_name!r}")
        self.name = port_name
        h = _k32.CreateFileW(f"\\\\.\\{port_name}", GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE:
            raise SerialError(f"cannot open {port_name} exclusively (Windows error {ctypes.get_last_error()}); "
                              "is another program using it?")
        self._h = h

    def __enter__(self) -> WinSerialPort:  # noqa: PYI034 - typing.Self needs Python 3.11
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _read_back(self) -> tuple[DCB, COMMTIMEOUTS]:
        dcb = DCB()  # zeroed, like the vendor's memset
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not _k32.GetCommState(self._h, ctypes.byref(dcb)):
            raise SerialError(f"GetCommState failed ({ctypes.get_last_error()})")
        to = COMMTIMEOUTS()
        if not _k32.GetCommTimeouts(self._h, ctypes.byref(to)):
            raise SerialError(f"GetCommTimeouts failed ({ctypes.get_last_error()})")
        return dcb, to

    def inspect(self) -> dict:
        dcb, to = self._read_back()
        return {"dcb": describe_dcb(dcb),
                "timeouts": {f: getattr(to, f) for f, _t in COMMTIMEOUTS._fields_}}

    def configure(self, baud: int) -> None:
        dcb, to = self._read_back()
        new = vendor_line_settings(dcb, baud)
        if not _k32.SetCommState(self._h, ctypes.byref(new)):
            raise SerialError(f"SetCommState failed ({ctypes.get_last_error()})")
        new_to = vendor_timeouts(to)
        if not _k32.SetCommTimeouts(self._h, ctypes.byref(new_to)):
            raise SerialError(f"SetCommTimeouts failed ({ctypes.get_last_error()})")

    def purge_rx(self) -> None:
        _k32.PurgeComm(self._h, PURGE_RXCLEAR)

    def write(self, data: bytes) -> int:
        n = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        if not _k32.WriteFile(self._h, buf, len(data), ctypes.byref(n), None):
            raise SerialError(f"WriteFile failed ({ctypes.get_last_error()})")
        return n.value

    def read(self, max_bytes: int) -> bytes:
        """Returns what arrives within the configured timeouts (possibly nothing)."""
        n = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(max_bytes)
        if not _k32.ReadFile(self._h, buf, max_bytes, ctypes.byref(n), None):
            raise SerialError(f"ReadFile failed ({ctypes.get_last_error()})")
        return buf.raw[:n.value]

    def close(self) -> None:
        if getattr(self, "_h", None) not in (None, INVALID_HANDLE_VALUE):
            _k32.CloseHandle(self._h)
            self._h = None
