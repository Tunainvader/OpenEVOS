"""Read-only XInput gamepad reader (Windows' xinput1_4.dll via ctypes). Kept out of `ui/`, which may not use FFI
(tests/test_phase2_import_guard.py); `ui/gamepad.py` maps its readings to Capture page actions."""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

# XINPUT_GAMEPAD button bits
BUTTONS = {
    "up": 0x0001, "down": 0x0002, "left": 0x0004, "right": 0x0008, "start": 0x0010, "back": 0x0020,
    "ls": 0x0040, "rs": 0x0080, "lb": 0x0100, "rb": 0x0200, "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
}


@dataclass(frozen=True)
class PadState:
    """One reading, sticks in -1..1 (up and right positive), triggers 0..1."""

    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    lt: float = 0.0
    rt: float = 0.0
    buttons: frozenset[str] = frozenset()


def _stick(v: int) -> float:
    return max(-1.0, v / 32767.0)


class _XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XInputGamepad)]


class XInputSource:
    """Reads the first connected XInput pad. `read()` is None with no pad (or off Windows)."""

    def __init__(self) -> None:
        self._dll = None
        self.user: int | None = None
        if sys.platform == "win32":
            for name in ("xinput1_4", "xinput9_1_0"):
                try:
                    self._dll = ctypes.WinDLL(name)  # type: ignore[attr-defined]
                    break
                except OSError:
                    continue

    @property
    def available(self) -> bool:
        return self._dll is not None

    def read(self) -> PadState | None:
        if self._dll is None:
            return None
        users = [self.user] if self.user is not None else range(4)
        for u in users:
            st = _XInputState()
            if self._dll.XInputGetState(u, ctypes.byref(st)) == 0:
                self.user = u
                g = st.Gamepad
                return PadState(_stick(g.sThumbLX), _stick(g.sThumbLY), _stick(g.sThumbRX), _stick(g.sThumbRY),
                                g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0,
                                frozenset(k for k, bit in BUTTONS.items() if g.wButtons & bit))
        self.user = None
        return None
