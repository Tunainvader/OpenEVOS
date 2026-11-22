"""One OpenEVOS app per instrument: a second copy would fight the first for COM3 and the LED board (2026-11-18: two
copies started 6 s apart after a power cycle).

Two guards, taken before anything touches the hardware and held for the life of the app:
* a lock file next to the settings file (QLockFile; it records the holder's PID, on every platform);
* on Windows, a named kernel object in the logon session's namespace: a one-byte Qt shared-memory section
  (QSharedMemory with a native key, so no FFI in the UI package).

The lock file alone missed a copy started from inside a packaged (MSIX) app, such as a terminal in the Claude desktop
app. New files under %LOCALAPPDATA% land in that app's private copy, so each copy found only its own lock file
(2026-11-23). Kernel object names are shared across that boundary (checked both ways that day), and the named
section does not depend on --settings either. The kernel drops it when its holder exits, crashed or not, so it never
goes stale.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QLockFile, QSharedMemory

SECTION_NAME = "Local\\OpenEVOS-instrument"
LOCK_FILE = "openevos-instrument.lock"


class InstanceGuard:
    """Both guards for one copy of the app. `acquire()` is True when no other copy runs on the instrument."""

    def __init__(self, settings_path: Path, *, use_section: bool | None = None, section_name: str | None = None):
        self.lock_path = settings_path.with_name(LOCK_FILE)
        self.use_section = sys.platform == "win32" if use_section is None else use_section
        self.section_name = section_name or SECTION_NAME  # read here, so tests can give a session its own name
        self.note: str | None = None  # set when the named section could not be made; the lock file still guards
        self._section: QSharedMemory | None = None
        self._lock: QLockFile | None = None

    def acquire(self, timeout_ms: int = 300) -> bool:
        from PySide6.QtCore import QLockFile, QSharedMemory  # at use: importing this module must not load Qt

        if self.use_section:
            section = QSharedMemory()
            section.setNativeKey(self.section_name)
            if section.create(1):
                self._section = section
            elif section.error() in (QSharedMemory.SharedMemoryError.AlreadyExists,
                                     QSharedMemory.SharedMemoryError.PermissionDenied):  # e.g. an elevated copy
                return False
            else:
                self.note = f"instance section unavailable ({section.errorString()}); the lock file still guards"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.lock_path))
        if not lock.tryLock(timeout_ms):
            self.release()
            return False
        self._lock = lock
        return True

    def release(self) -> None:
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
        if self._section is not None:
            self._section.detach()
            self._section = None
