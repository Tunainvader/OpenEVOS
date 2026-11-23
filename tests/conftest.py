"""Shared fixtures: a fake read-only runner fed by recorded/synthetic Windows query output.

All VID/PIDs for "new" devices (1234:5678, ABCD:0001) are synthetic and imply nothing about the
real EVOS hardware.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from core_helpers import manual_core  # noqa: F401  (shared Phase 2.1 core fixture)

# Qt's offscreen platform has no system font lookup on Windows: without this, UI tests lay out with
# fallback metrics (wider widgets, e.g. the narrow-rail test cannot shrink the window to 1200 px).
if sys.platform == "win32":
    _fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if _fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_fonts))

from openevos.inspector.collectors import CollectorContext
from openevos.inspector.collectors.registry import DictRegistryReader
from openevos.inspector.evidence import EvidenceRegister

FIX = Path(__file__).parent / "fixtures" / "windows"


@pytest.fixture(autouse=True)
def _own_instance_section(monkeypatch):
    """main() with real-hardware arguments takes the app's one-copy guard, whose Windows named section is per logon
    session. Tests get their own name: never refused while the instrument app is open on this PC, never blocking it."""
    from openevos.ui import instance_guard

    monkeypatch.setattr(instance_guard, "SECTION_NAME", f"Local\\OpenEVOS-instrument-test-{os.getpid()}")


@pytest.fixture(autouse=True, scope="module")
def _drop_closed_windows():
    """Delete the closed, parentless top-level windows earlier test modules left behind (2026-11-24: 118 of them by
    the focus-pad tests; every stylesheet change re-styled them all, and a theme switch in the full suite took
    minutes, so the run looked hung). At a module's start every fixture of the modules before it has ended; a window
    still showing, or one with a parent (a menu, a popup), is left alone."""
    if "PySide6.QtWidgets" in sys.modules:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            for w in app.topLevelWidgets():
                if w.parent() is None and not w.isVisible():
                    w.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    yield


def load(name: str):
    return json.loads((FIX / name).read_text("utf-8"))


class FakeRunner:
    """Stands in for safe_exec.run. Records every query key it is asked for."""

    def __init__(self, snapshot: str = "baseline"):
        self.snapshot = snapshot
        self.calls: list[str] = []

    def __call__(self, key: str, **kwargs):
        self.calls.append(key)
        if key == "pnp_devices":
            devs = load("pnp_devices_baseline.json")
            if self.snapshot == "microscope":
                devs += load("pnp_devices_microscope_extra.json")
            return devs
        if key == "pnp_device_properties":
            return load("pnp_device_properties.json")
        if key == "signed_drivers":
            return load("signed_drivers.json")
        if key == "system_info":
            return load("system_info.json")
        if key == "pnp_events":
            assert isinstance(kwargs.get("hours"), int)
            return load("pnp_events.json")
        if key == "authenticode":
            return [{"Path": p, "Status": "NotSigned", "Signer": None, "Thumbprint": None}
                    for p in kwargs.get("stdin_obj", [])]
        raise AssertionError(f"unexpected query {key}")


def fake_ports(snapshot: str = "baseline"):
    ports = [SimpleNamespace(device="COM3", name="COM3", description="USB Serial Port (COM3)",
                             hwid="FTDIBUS\\VID_0403+PID_6001+A12345BCA\\0000", vid=0x0403, pid=0x6001,
                             serial_number="A12345BCA", location=None, manufacturer="FTDI",
                             product=None, interface=None)]
    if snapshot == "microscope":
        ports.append(SimpleNamespace(device="COM7", name="COM7", description="Synthetic USB Serial (COM7)",
                                     hwid="USB VID:PID=ABCD:0001 SER=SN0001TEST", vid=0xABCD, pid=0x0001,
                                     serial_number="SN0001TEST", location="1-4.2", manufacturer=None,
                                     product=None, interface=None))
    return lambda: ports


UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def fake_registry() -> DictRegistryReader:
    return DictRegistryReader({
        "HKLM64": {
            UNINSTALL: {"__subkeys__": ["{VC2010}"]},
            UNINSTALL + r"\{VC2010}": {"DisplayName": "Microsoft Visual C++ 2010  x86 Redistributable",
                                       "DisplayVersion": "10.0.40219", "Publisher": "Microsoft Corporation"},
            "SOFTWARE": {"__subkeys__": ["Microsoft", "Classes"]},
            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full": {"Release": 528372},
        },
        "HKLM32": {
            UNINSTALL: {"__subkeys__": ["{EVOS}"]},
            UNINSTALL + r"\{EVOS}": {"DisplayName": "Synthetic EVOS Test Suite", "DisplayVersion": "0.0.1",
                                     "Publisher": "Life Technologies (synthetic)",
                                     "InstallLocation": r"C:\Program Files (x86)\EVOS"},
            "SOFTWARE": {"__subkeys__": ["Life Technologies", "Wow6432Node"]},
            r"SOFTWARE\Life Technologies": {"__subkeys__": ["EVOS"]},
        },
        "HKCU": {"Software": {"__subkeys__": []}},
    })


@pytest.fixture
def make_ctx(tmp_path):
    def _make(snapshot: str = "baseline", artifact_roots: list[Path] | None = None):
        ctx = CollectorContext(
            scan_id="t", register=EvidenceRegister(scan_id="t"), runner=FakeRunner(snapshot),
            is_windows=True, artifact_roots=artifact_roots or [], include_default_roots=False,
            events_hours=24, registry_reader=fake_registry(), serial_lister=fake_ports(snapshot),
            environ={"SystemRoot": str(tmp_path / "nowhere" / "Windows")},
        )
        return ctx
    return _make


# ---------------------------------------------------------------------------- synthetic PE
def make_pe(machine: int = 0x14C, magic: int = 0x10B, clr_flags: int | None = None) -> bytes:
    """Build a minimal PE image header sufficient for parse_pe (not runnable)."""
    e_lfanew = 0x80
    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    buf[e_lfanew:e_lfanew + 4] = b"PE\0\0"
    coff = e_lfanew + 4
    opt_size = 224 if magic == 0x10B else 240
    struct.pack_into("<HH", buf, coff, machine, 1)
    struct.pack_into("<H", buf, coff + 16, opt_size)
    opt = coff + 20
    struct.pack_into("<H", buf, opt, magic)
    dd_base = opt + (96 if magic == 0x10B else 112)
    struct.pack_into("<I", buf, dd_base - 4, 16)
    sec = opt + opt_size
    buf[sec:sec + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", buf, sec + 8, 0x200, 0x1000, 0x200, 0x200)
    if clr_flags is not None:
        struct.pack_into("<II", buf, dd_base + 14 * 8, 0x1010, 72)
        struct.pack_into("<I", buf, 0x210, 72)
        struct.pack_into("<I", buf, 0x210 + 16, clr_flags)
    return bytes(buf)


INF_TEXT = """; synthetic driver package for tests
[Version]
Signature="$Windows NT$"
Class=Image
ClassGuid={6bdd1fc6-810f-11d0-bec7-08002be2092f}
Provider=%MFG%
DriverVer=03/01/2013,1.0.0.0 ; trailing comment
CatalogFile=synth.cat

[Manufacturer]
%MFG%=Models,NTamd64

[Models.NTamd64]
%DevDesc%=Inst, USB\\VID_1234&PID_5678&MI_00
%DevDesc2%=Inst, USB\\VID_1234&PID_9999

[Strings]
MFG="Life Technologies"
DevDesc="Synthetic EVOS Camera Interface"
DevDesc2="Synthetic Other"
"""


@pytest.fixture
def fake_image(tmp_path) -> Path:
    return build_fake_image(tmp_path / "img")


def build_fake_image(root: Path) -> Path:
    """A directory shaped like a mounted copy of an old Windows system volume."""
    (root / "Windows" / "INF").mkdir(parents=True)
    (root / "Windows" / "System32").mkdir(parents=True)
    (root / "Windows" / "INF" / "oem12.inf").write_bytes(b"\xff\xfe" + INF_TEXT.encode("utf-16-le"))
    (root / "Windows" / "INF" / "usb.inf").write_text("[Version]\nProvider=Microsoft\nUSB\\VID_1234&PID_5678\n")
    (root / "Windows" / "System32" / "ftd2xx.dll").write_bytes(make_pe(0x8664, 0x20B))
    evos = root / "Program Files (x86)" / "EVOS FL Auto"
    evos.mkdir(parents=True)
    (evos / "EvosApp.exe").write_bytes(make_pe(clr_flags=0x1 | 0x2))  # .NET, 32BITREQUIRED
    (evos / "CamBridge.dll").write_bytes(make_pe(0x14C))  # native x86
    (evos / "FlyCapture2.dll").write_bytes(make_pe(0x14C))
    (evos / "calibration.xml").write_text("<cal/>")
    mm = root / "Program Files" / "Micro-Manager-2.0"
    mm.mkdir(parents=True)
    (mm / "mmgr_dal_DemoCamera.dll").write_bytes(make_pe(0x8664, 0x20B))
    (mm / "mmgr_dal_PointGrey.dll").write_bytes(make_pe(0x8664, 0x20B))
    (root / "Users" / "jdoe" / "Documents").mkdir(parents=True)
    (root / "Users" / "jdoe" / "Documents" / "unrelated.txt").write_text("x")
    # A file under Windows/ that must NOT be walked by the artifact collector:
    (root / "Windows" / "evos_should_be_skipped.log").write_text("x")
    return root
