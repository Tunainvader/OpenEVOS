"""One app per instrument (instance_guard). The guard's lock file sits in %LOCALAPPDATA%. A copy started from inside a
packaged app (a terminal in the Claude desktop app) sees that folder through the app's private copy, so two copies
never saw each other's lock (2026-11-23). On Windows a named kernel section now backs the lock file. Temporary files
only; the section name is the test session's own (conftest), never the running app's."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from openevos.ui import instance_guard
from openevos.ui.instance_guard import InstanceGuard

windows = pytest.mark.skipif(sys.platform != "win32", reason="named section: Windows only")

# another process holding the section; it exits without detaching, as a crashed app would
HOLDER = textwrap.dedent("""
    import os, sys
    from PySide6.QtCore import QSharedMemory
    s = QSharedMemory()
    s.setNativeKey(sys.argv[1])
    print("held" if s.create(1) else f"not held ({s.errorString()})", flush=True)
    sys.stdin.read()
    os._exit(0)
""")


@windows
def test_a_second_copy_is_refused_whatever_its_settings_file(tmp_path):
    first = InstanceGuard(tmp_path / "a" / "ui_settings.json")
    other = InstanceGuard(tmp_path / "b" / "ui_settings.json")  # its own lock file: only the section sees the first
    assert first.acquire()
    try:
        assert not other.acquire()
        assert not (tmp_path / "b" / instance_guard.LOCK_FILE).exists()  # refused before it made a lock file
    finally:
        first.release()
    assert other.acquire()
    other.release()


@windows
def test_the_section_is_seen_from_another_process_and_never_goes_stale(tmp_path):
    guard = InstanceGuard(tmp_path / "ui_settings.json")
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, instance_guard.SECTION_NAME], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert not guard.acquire()
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)
    assert guard.acquire()  # the holder exited without detaching: the kernel dropped the section
    guard.release()


def test_without_a_section_the_lock_file_still_guards(tmp_path, monkeypatch):
    from PySide6 import QtCore

    real = QtCore.QSharedMemory

    class Unavailable:  # a section that cannot be made for a reason other than another copy
        SharedMemoryError = real.SharedMemoryError

        def setNativeKey(self, key):
            pass

        def create(self, size):
            return False

        def error(self):
            return real.SharedMemoryError.UnknownError

        def errorString(self):
            return "unknown error"

    monkeypatch.setattr(QtCore, "QSharedMemory", Unavailable)
    first = InstanceGuard(tmp_path / "ui_settings.json", use_section=True)
    second = InstanceGuard(tmp_path / "ui_settings.json", use_section=True)
    assert first.acquire()
    assert first.note is not None and "the lock file still guards" in first.note
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_release_is_safe_twice_and_before_acquire(tmp_path):
    guard = InstanceGuard(tmp_path / "ui_settings.json")
    guard.release()
    assert guard.acquire()
    guard.release()
    guard.release()
    assert guard.acquire()  # free again
    guard.release()
