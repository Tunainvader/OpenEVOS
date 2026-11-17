"""ADR-0009: OpenEVOS never flashes; maintenance-only transitions are refused; bootloader quarantine.

These tests turn the firmware policy from a statement into an enforced property:
* every command path refuses firmware operations with an explicit reason;
* a device reporting BOOTLOADER (or a physical device with UNKNOWN firmware) accepts nothing;
* no source file under core/hal/backends/safety/ui defines or calls a firmware operation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from core_helpers import ScriptedBackend, stage_move

from openevos.core.models import (
    Activity,
    BackendKind,
    CommandOrigin,
    CommandStatus,
    Connection,
    DeviceKind,
    DeviceSnapshot,
    FirmwareMode,
    FirmwareOperationRefused,
    Health,
    MoveStageAbsolute,
    parse_command,
)
from openevos.safety.firmware import (
    FirmwareModeRefusal,
    is_banned_firmware_operation,
    is_maintenance_only_operation,
    operation_refusal,
)

BANNED = [
    "led.download_hex", "DownloadHex", "DownloadBootloaderHex", "firmware.flash", "flash_firmware",
    "UpdateFirmware", "led.update_firmware", "cn.reflash", "ResetToBootloader", "enter_bootloader",
    "erase_flash", "VerifyHex", "SetUpdatedBootloaderVersion", "led.flash", "camera.firmware", "dfu.write",
]
MAINTENANCE = ["StartApp", "led.start_app", "jump_to_application", "reboot", "cn.reset_device"]
# HW_DATA writes would overwrite the board's stored calibration (fl1_v1.6_led_board_static_analysis.md)
BANNED += ["ToPicFlash", "led.write_hw_data", "SaveHwDataToFlash", "write_eeprom", "write_calibration"]
# Technosoft drives: writing a .sw program/setup image into a drive's EEPROM (TML_lib.dll)
BANNED += ["TS_DownloadSwFile", "motor.download_sw", "TS_SaveSetup", "motor.write_setup"]
# Exact names from the vendor type libraries (IGen3LedBoard / IGen3LedBoardBootloader)
BANNED += ["SetHardwareSpecificData", "SetConsecutiveDoubleHardwareSpecificData", "SetPersistentData",
           "SetPersistentData2", "SetPersistentData2Vector", "SetHardwareIniDataStored", "ResetToBootloader",
           "DownloadBootloaderHex", "SetUpdatedBootloaderVersion", "SetEraseRange", "SetCN5TestEnabled",
           "SetRunCN5TestUnconditionally"]
# Their read twins are not writes, but are L3: refused until a reviewed protocol + operator approval exist.
VENDOR_READS = ["GetHardwareSpecificData", "GetPersistentData", "GetPersistentData2", "GetPersistentDataChecksum",
                "GetPersistentDataSection2Checksum", "GetHardwareIniDataStored", "GetApplicationFirmwareVersion",
                "GetFirmwareVersion", "GetEraseRange", "GetCN5TestState", "led.get_application_firmware_version"]


@pytest.mark.parametrize("name", VENDOR_READS)
def test_vendor_read_methods_are_l3_not_flash(name):
    from openevos.safety.firmware import is_reviewed_read_operation

    assert not is_banned_firmware_operation(name)
    assert not is_maintenance_only_operation(name)
    assert is_reviewed_read_operation(name)
    assert "L3" in operation_refusal(name)
ALLOWED = ["stage.move_absolute", "focus.move_absolute", "turret.set_position", "illumination.set",
           "camera.set_exposure", "xy.move_relative", "led.set_intensity", "incubator.set_enabled"]


# ------------------------------------------------------------------ name classification
@pytest.mark.parametrize("name", BANNED)
def test_banned_firmware_operations(name):
    assert is_banned_firmware_operation(name)
    assert "never flashes" in operation_refusal(name)


@pytest.mark.parametrize("name", MAINTENANCE)
def test_maintenance_only_operations(name):
    assert not is_banned_firmware_operation(name)
    assert is_maintenance_only_operation(name)
    assert "maintenance workflow" in operation_refusal(name)


@pytest.mark.parametrize("name", ALLOWED)
def test_ordinary_operations_are_not_firmware(name):
    assert operation_refusal(name) is None


# ------------------------------------------------------------------ new command core
@pytest.mark.parametrize("kind", BANNED + MAINTENANCE)
def test_parse_command_refuses_firmware_kinds_with_reason(kind):
    with pytest.raises(FirmwareOperationRefused, match="ADR-0009"):
        parse_command({"kind": kind, "device_id": "led", "origin": "ui"})


def _snapshot(mode: FirmwareMode, backend: BackendKind) -> DeviceSnapshot:
    return DeviceSnapshot(device_id="d", kind=DeviceKind.ILLUMINATION, backend_kind=backend,
                          connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE,
                          firmware_mode=mode)


def test_quarantine_rules():
    assert "BOOTLOADER" in FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.BOOTLOADER, BackendKind.SIMULATED))[0]
    assert FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.BOOTLOADER, BackendKind.PHYSICAL))
    assert "RECOVERY" in FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.RECOVERY, BackendKind.SIMULATED))[0]
    assert FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.RECOVERY, BackendKind.PHYSICAL))
    assert "fail closed" in FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.UNKNOWN, BackendKind.PHYSICAL))[0]
    assert FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.APPLICATION, BackendKind.PHYSICAL)) == []
    assert FirmwareModeRefusal.reasons(_snapshot(FirmwareMode.UNKNOWN, BackendKind.SIMULATED)) == []


def test_bootloader_device_is_quarantined_end_to_end(manual_core):
    """A device that reports BOOTLOADER: the router rejects, and the backend never sees the command."""
    core, *_ = manual_core
    backend = ScriptedBackend("quarantined", DeviceKind.XY_STAGE)
    core.devices.register(backend, _caps_like(core, "quarantined"))
    core.devices.connect("quarantined")
    backend.report(firmware_mode=FirmwareMode.BOOTLOADER, reason="enumerated as bootloader")
    assert core.store.get("quarantined").firmware_mode is FirmwareMode.BOOTLOADER

    ticket = core.router.submit(_move("quarantined"))
    assert ticket.record.status is CommandStatus.REJECTED
    assert "BOOTLOADER" in (ticket.record.message or "")
    assert backend.started == []

    # Leaving bootloader is an observation from the device, never something OpenEVOS commands.
    backend.report(firmware_mode=FirmwareMode.APPLICATION, reason="application running")
    ok = core.router.submit(_move("quarantined"))
    assert ok.record.status is not CommandStatus.REJECTED, ok.record.message


def _move(device_id: str) -> MoveStageAbsolute:
    template = stage_move(10.0, 10.0)
    return MoveStageAbsolute(device_id=device_id, x=template.x, y=template.y, unit=template.unit,
                             frame=template.frame, origin=CommandOrigin.TEST, timeout_s=30.0)


def test_store_tracks_firmware_mode_with_versions(manual_core):
    core, *_ = manual_core
    backend = ScriptedBackend("fw", DeviceKind.XY_STAGE)
    core.devices.register(backend, _caps_like(core, "fw"))
    before = core.store.get("fw")
    assert before.firmware_mode is FirmwareMode.UNKNOWN
    backend.report(firmware_mode=FirmwareMode.BOOTLOADER)
    after = core.store.get("fw")
    assert after.firmware_mode is FirmwareMode.BOOTLOADER and after.version == before.version + 1


def _caps_like(core, device_id):
    """The simulated stage's (SIMULATED-source) capability record, re-addressed to another device."""
    return core.devices.get("sim-stage").capabilities.model_copy(update={"device_id": device_id})


# ------------------------------------------------------------------ UI command port (ADR-0010)
def test_command_port_refuses_firmware_operations_with_explicit_check():
    pytest.importorskip("numpy")
    from openevos.core.command_language import CommandRejected
    from openevos.core.instrument import InstrumentConfig, build_instrument

    inst = build_instrument(InstrumentConfig(backend_options={"time_scale": 0.0}), initialize=False)
    try:
        n0 = len(inst.core.router._entries)
        for name in ("led.download_hex", "led.start_app", "carriage.update_firmware"):
            with pytest.raises(CommandRejected) as ei:
                inst.gateway.execute(name)
            assert ei.value.check == "firmware", ei.value
        assert len(inst.core.router._entries) == n0  # refused before a command even exists
    finally:
        inst.close()


def test_command_catalog_contains_no_firmware_operation():
    from openevos.core.command_language import build_catalog

    assert all(operation_refusal(n) is None for n in build_catalog())


# ------------------------------------------------------------------ static source guard
SRC = Path(__file__).resolve().parents[1] / "src" / "openevos"
GUARDED = ("core", "hal", "backends", "safety", "ui", "maintenance", "instrument", "acquisition",
           "imaging", "analysis")
POLICY_FILES = {SRC / "safety" / "firmware.py"}  # defines the banned vocabulary itself


def _names(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            yield node.lineno, node.func.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 60 \
                and "." in node.value and " " not in node.value:
            yield node.lineno, node.value  # command-kind-like literals, e.g. "led.download_hex"


def _reviewed_maintenance_modules() -> set[Path]:
    from openevos.safety.firmware import REVIEWED_MAINTENANCE

    return {SRC / entry["module"] for entry in REVIEWED_MAINTENANCE.values()}


def test_no_firmware_operation_is_defined_or_called_in_source():
    from openevos.safety.firmware import is_banned_firmware_operation, is_maintenance_only_operation

    reviewed = _reviewed_maintenance_modules()  # ADR-0012: the one place a reviewed L4 name may appear
    hits = []
    for sub in GUARDED:
        for p in (SRC / sub).rglob("*.py"):
            if p in POLICY_FILES:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for line, name in _names(tree):
                if name in ("operation_refusal", "is_banned_firmware_operation", "is_maintenance_only_operation",
                            "firmware_mode", "FirmwareMode", "FirmwareModeRefusal", "FirmwareOperationRefused",
                            "firmware_version", "_assert_no_firmware_command_types"):  # policy enforcement
                    continue
                if p in reviewed and is_maintenance_only_operation(name) and not is_banned_firmware_operation(name):
                    continue
                if operation_refusal(name):
                    hits.append(f"{p.relative_to(SRC)}:{line} {name}")
    assert not hits, hits


def test_reviewed_maintenance_modules_exist_and_are_maintenance_tools():
    for p in _reviewed_maintenance_modules():
        assert p.is_file(), p
        assert p.parent == SRC / "maintenance", p  # never inside core/, hal/, ui/ or backends/


def test_maintenance_names_outside_the_reviewed_module_are_still_flagged():
    from openevos.safety.firmware import is_maintenance_only_operation

    assert is_maintenance_only_operation("start_app") and operation_refusal("led.start_app")
