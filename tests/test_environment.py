"""Regression tests for the first real Windows run (2026-11-12).

On Windows, ``dict(os.environ)`` has UPPERCASE keys, so ``.get("ProgramFiles")`` returned None
and the default EVOS/camera-SDK search folders silently vanished. These tests feed the
collectors an environment shaped exactly like that and check that (a) the folders are found and
(b) a search that could not cover its default folders says so instead of reading as complete.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeRunner, make_pe

from openevos.inspector.collectors import (
    CaseInsensitiveEnv,
    CollectorContext,
    CollectorSkipped,
    ScanResults,
    ps_value,
)
from openevos.inspector.collectors.camera_sdks import CameraSdksCollector
from openevos.inspector.collectors.drivers import SignedDriversCollector
from openevos.inspector.collectors.evos_artifacts import EvosArtifactsCollector
from openevos.inspector.collectors.keywords import keyword_hits
from openevos.inspector.evidence import EvidenceRegister


def _ctx(environ: dict[str, str]) -> CollectorContext:
    return CollectorContext(
        scan_id="t", register=EvidenceRegister(scan_id="t"), runner=FakeRunner(),
        is_windows=True, include_default_roots=True, environ=environ,
    )


def test_case_insensitive_env():
    env = CaseInsensitiveEnv({"PROGRAMFILES": r"C:\Program Files"})
    assert env.get("ProgramFiles") == env["programfiles"] == r"C:\Program Files"
    assert "ProgramFiles" in env and "ProgramData" not in env
    assert env.get("ProgramData") is None


def test_context_wraps_plain_dicts_and_defaults_to_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENEVOS_TEST_DIR", str(tmp_path))
    ctx = CollectorContext(scan_id="t", register=EvidenceRegister(scan_id="t"))
    assert ctx.environ.get("openevos_test_dir") == str(tmp_path)
    assert isinstance(_ctx({"ProgramFiles": "x"}).environ, CaseInsensitiveEnv)


def test_uppercase_environment_still_finds_default_folders(tmp_path: Path):
    pf = tmp_path / "Program Files"
    app = pf / "Invitrogen" / "EVOS FL Auto"
    app.mkdir(parents=True)
    (app / "EvosApp.exe").write_bytes(make_pe())
    drive = tmp_path / "drive"
    drive.mkdir()
    ctx = _ctx({"PROGRAMFILES": str(pf), "SYSTEMDRIVE": str(drive)})

    results = ScanResults()
    warning = EvosArtifactsCollector().collect(ctx, results)

    assert [Path(a.path).name for a in results.artifacts] == ["EvosApp.exe"]
    # The other default folders were not resolvable here, so the run must be partial, by name.
    assert warning == (
        "default search folder(s) not resolved: ProgramFiles(x86), ProgramData, LOCALAPPDATA, APPDATA"
    )


def test_openevos_itself_is_not_an_evos_remnant(tmp_path: Path):
    assert keyword_hits("openevos") == keyword_hits("OpenEVOS-inspector-v0.1.zip") == []
    assert keyword_hits("EVOS FL Auto") == keyword_hits("EvosApp.exe") == ["evos"]
    local = tmp_path / "AppData" / "Local"
    pkg = local / "Programs" / "Python312" / "Lib" / "site-packages" / "openevos" / "inspector"
    (pkg / "collectors").mkdir(parents=True)
    (pkg / "collectors" / "evos_artifacts.py").write_text("x")  # own source, EVOS in the file name
    (pkg / "evos_fl_auto_profile.json").write_text("{}")
    (local / "work" / "openevos-scans" / "20261112-amafd1000-connected").mkdir(parents=True)
    (local / "work" / "openevos-scans" / "20261112-amafd1000-connected" / "device_inventory.json").write_text("{}")
    (local / "Invitrogen").mkdir()
    (local / "Invitrogen" / "settings.xml").write_text("<x/>")
    ctx = _ctx({"LOCALAPPDATA": str(local), "SYSTEMDRIVE": str(tmp_path / "drive")})

    results = ScanResults()
    EvosArtifactsCollector().collect(ctx, results)
    assert [Path(a.path).name for a in results.artifacts] == ["settings.xml"]


class _DriverRowsRunner(FakeRunner):
    """signed_drivers rows as Windows PowerShell 5.1 really emits them."""

    def __call__(self, key, **kwargs):
        if key == "signed_drivers":
            return [
                {"DeviceID": "USB\\VID_1234&PID_5678\\1", "DeviceName": "Synthetic", "DriverProviderName": "Acme",
                 "DriverVersion": "1.0", "DriverDate": {}, "InfName": "oem9.inf", "IsSigned": True,
                 "Signer": None, "DeviceClass": "IMAGE", "HardWareID": "USB\\VID_1234&PID_5678"},
                {"DeviceID": "PCI\\SYNTHETIC\\BAD", "DeviceName": {"unexpected": 1}, "DriverDate": "2020-01-01"},
            ]
        return super().__call__(key, **kwargs)


def test_empty_object_dates_and_bad_rows_do_not_fail_the_driver_scan():
    ctx = _ctx({})
    ctx.runner = _DriverRowsRunner()
    results = ScanResults()
    warning = SignedDriversCollector().collect(ctx, results)
    assert [(d.device_id, d.date) for d in results.drivers] == [("USB\\VID_1234&PID_5678\\1", None)]
    assert results.drivers[0].evidence_ids
    assert warning and warning.startswith("1 driver row(s) could not be parsed")


def test_ps_value():
    assert ps_value({}) is None and ps_value([]) is None
    assert ps_value("2020-01-01") == "2020-01-01" and ps_value(0) == 0 and ps_value(False) is False


def test_no_resolvable_default_folder_is_skipped_with_true_reason(tmp_path: Path):
    ctx = _ctx({"SYSTEMDRIVE": str(tmp_path)})
    with pytest.raises(CollectorSkipped, match="no default search folder could be resolved"):
        EvosArtifactsCollector().collect(ctx, ScanResults())


def test_camera_sdk_search_uses_uppercase_env_and_flags_missing_folders(tmp_path: Path):
    pf = tmp_path / "Program Files"
    mm = pf / "Micro-Manager-2.0"
    mm.mkdir(parents=True)
    (mm / "mmgr_dal_DemoCamera.dll").write_bytes(make_pe(0x8664, 0x20B))
    ctx = _ctx({"PROGRAMFILES": str(pf), "SYSTEMROOT": str(tmp_path / "nowhere" / "Windows")})

    results = ScanResults()
    warning = CameraSdksCollector().collect(ctx, results)

    assert [m.adapters for m in results.micromanager] == [["DemoCamera"]]
    assert warning == "default search folder(s) not resolved: ProgramFiles(x86)"
