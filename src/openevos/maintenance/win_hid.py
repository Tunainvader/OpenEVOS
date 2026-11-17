"""Minimal Windows HID transport for reviewed maintenance reads (ADR-0011).

Implements exactly what docs/evidence/fl1_v1.6_led_board_static_analysis.md §6 prescribes:

1. Find the device interface through SetupAPI (`GUID_DEVINTERFACE_HID`), by VID/PID.
2. `describe()` opens the interface with **zero access rights** (no read, no write) and reads
   only what the HID class driver already holds: attributes (VID/PID/version) and report sizes.
   Nothing is sent to the device.
3. `open_for_exchange()` opens it with GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ|WRITE and
   FILE_FLAG_OVERLAPPED (one handle; the vendor used two).
4. `exchange_once()`: `HidD_FlushQueue`, then one `WriteFile` and one `ReadFile`, each limited
   to `timeout_s` (default 0.5 s). On a timeout: `CancelIoEx`, close, report. Never retries.

Nothing here knows any command bytes; the caller (`led_bootloader`) decides what is sent.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

if sys.platform != "win32":  # pragma: no cover - importable elsewhere for tests, unusable
    raise ImportError("openevos.maintenance.win_hid needs Windows")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_setup = ctypes.WinDLL("setupapi", use_last_error=True)
_hid = ctypes.WinDLL("hid", use_last_error=True)

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
ERROR_IO_PENDING = 997
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
HIDP_STATUS_SUCCESS = 0x00110000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_size_t)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT), ("ProductID", wintypes.USHORT),
                ("VersionNumber", wintypes.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                ("InputReportByteLength", wintypes.USHORT), ("OutputReportByteLength", wintypes.USHORT),
                ("FeatureReportByteLength", wintypes.USHORT), ("Reserved", wintypes.USHORT * 17),
                ("NumberLinkCollectionNodes", wintypes.USHORT), ("NumberInputButtonCaps", wintypes.USHORT),
                ("NumberInputValueCaps", wintypes.USHORT), ("NumberInputDataIndices", wintypes.USHORT),
                ("NumberOutputButtonCaps", wintypes.USHORT), ("NumberOutputValueCaps", wintypes.USHORT),
                ("NumberOutputDataIndices", wintypes.USHORT), ("NumberFeatureButtonCaps", wintypes.USHORT),
                ("NumberFeatureValueCaps", wintypes.USHORT), ("NumberFeatureDataIndices", wintypes.USHORT)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


_setup.SetupDiGetClassDevsW.restype = wintypes.HANDLE
_setup.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
_setup.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
                                               wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
_setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                    ctypes.c_void_p, wintypes.DWORD,
                                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_setup.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                           ctypes.POINTER(OVERLAPPED)]
_k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                          ctypes.POINTER(OVERLAPPED)]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                     wintypes.BOOL]
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
_hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
_hid.HidD_FlushQueue.argtypes = [wintypes.HANDLE]
_hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
_hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
_hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
_hid.HidP_GetCaps.restype = ctypes.c_long


class HidError(RuntimeError):
    pass


class HidTimeout(HidError):
    pass


@dataclass(frozen=True)
class HidDescription:
    path: str
    vendor_id: int
    product_id: int
    version: int
    input_report_len: int
    output_report_len: int


def _err(what: str) -> HidError:
    return HidError(f"{what} failed (Windows error {ctypes.get_last_error()})")


def find_interfaces(vid: int, pid: int) -> list[str]:
    """Device-interface paths of present HID devices whose path names this VID/PID. Read-only."""
    guid = GUID()
    _hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = _setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE_VALUE:
        raise _err("SetupDiGetClassDevsW")
    needle = f"vid_{vid:04x}&pid_{pid:04x}"
    out: list[str] = []
    try:
        i = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not _setup.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i, ctypes.byref(ifd)):
                break
            i += 1
            need = wintypes.DWORD(0)
            _setup.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            if need.value < 8:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not _setup.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if needle in path.lower():
                out.append(path)
    finally:
        _setup.SetupDiDestroyDeviceInfoList(hdev)
    return out


def describe(path: str) -> HidDescription:
    """Attributes and report sizes from the HID class driver. Opens with zero access; sends nothing."""
    h = _k32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise _err("CreateFileW (zero access)")
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if not _hid.HidD_GetAttributes(h, ctypes.byref(attrs)):
            raise _err("HidD_GetAttributes")
        pp = ctypes.c_void_p()
        if not _hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            raise _err("HidD_GetPreparsedData")
        try:
            caps = HIDP_CAPS()
            if _hid.HidP_GetCaps(pp, ctypes.byref(caps)) & 0xFFFFFFFF != HIDP_STATUS_SUCCESS:
                raise HidError("HidP_GetCaps failed")
        finally:
            _hid.HidD_FreePreparsedData(pp)
        return HidDescription(path, attrs.VendorID, attrs.ProductID, attrs.VersionNumber,
                              caps.InputReportByteLength, caps.OutputReportByteLength)
    finally:
        _k32.CloseHandle(h)


class WinHidChannel:
    """One overlapped handle for one request/reply exchange. Use as a context manager."""

    def __init__(self, path: str):
        self.path = path
        self._h = _k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                   OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self._h == INVALID_HANDLE_VALUE:
            raise _err("CreateFileW (read/write, overlapped)")

    def __enter__(self) -> WinHidChannel:  # noqa: PYI034 - typing.Self needs Python 3.11
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._h not in (None, INVALID_HANDLE_VALUE):
            _k32.CloseHandle(self._h)
            self._h = None

    def flush_input(self) -> None:
        if not _hid.HidD_FlushQueue(self._h):
            raise _err("HidD_FlushQueue")

    def _io(self, fn, buf, length: int, timeout_s: float, what: str) -> int:
        ev = _k32.CreateEventW(None, True, False, None)
        if not ev:
            raise _err("CreateEventW")
        ov = OVERLAPPED()
        ov.hEvent = ev
        done = wintypes.DWORD(0)
        try:
            ok = fn(self._h, buf, length, None, ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                raise _err(what)
            if _k32.WaitForSingleObject(ev, max(1, int(timeout_s * 1000))) != WAIT_OBJECT_0:
                _k32.CancelIoEx(self._h, ctypes.byref(ov))
                _k32.GetOverlappedResult(self._h, ctypes.byref(ov), ctypes.byref(done), True)  # let cancel settle
                raise HidTimeout(f"{what}: no completion within {timeout_s * 1000:.0f} ms")
            if not _k32.GetOverlappedResult(self._h, ctypes.byref(ov), ctypes.byref(done), False):
                raise _err(f"{what} (GetOverlappedResult)")
            return done.value
        finally:
            _k32.CloseHandle(ev)

    def write(self, report: bytes, timeout_s: float) -> int:
        buf = ctypes.create_string_buffer(bytes(report), len(report))
        return self._io(_k32.WriteFile, buf, len(report), timeout_s, "WriteFile")

    def read(self, length: int, timeout_s: float) -> bytes:
        buf = ctypes.create_string_buffer(length)
        n = self._io(_k32.ReadFile, buf, length, timeout_s, "ReadFile")
        return buf.raw[:n]
