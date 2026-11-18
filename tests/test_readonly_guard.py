"""Static guarantees that the Inspector and AI layer cannot write to hardware or the system.

These are tripwires, not proofs: they catch the obvious ways a future edit could break the
read-only contract (ADR-0003) and force that edit through code review.
"""

import ast
from pathlib import Path

import pytest

from openevos.inspector import safe_exec

SRC = Path(__file__).resolve().parents[1] / "src" / "openevos"
INSPECTOR_AND_AI = [p for p in SRC.rglob("*.py") if "inspector" in p.parts or "ai" in p.parts]


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                yield node, f.attr, ast.unparse(f.value)
            elif isinstance(f, ast.Name):
                yield node, f.id, ""


@pytest.mark.parametrize("path", INSPECTOR_AND_AI, ids=lambda p: str(p.relative_to(SRC)))
def test_no_forbidden_calls(path):
    tree = ast.parse(path.read_text("utf-8"))
    rel = path.relative_to(SRC).as_posix()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])  # noqa: E501
            for n in names:
                if n and n.split(".")[0] in {"subprocess", "usb", "ctypes.wintypes", "pywinusb", "hid", "ftd2xx"}:  # noqa: SIM102
                    if not (n == "subprocess" and rel.endswith("inspector/safe_exec.py")):
                        pytest.fail(f"{rel}: import of '{n}' is not allowed outside safe_exec")
    for node, name, owner in _calls(tree):
        loc = f"{rel}:{node.lineno}"
        assert not (name in {"Serial", "serial_for_url"}), f"{loc}: opening a serial port is forbidden"
        assert name not in {"SetValue", "SetValueEx", "CreateKey", "CreateKeyEx", "DeleteKey", "DeleteValue",
                            "DeleteKeyEx"}, f"{loc}: registry write"
        # Names that are destructive on any receiver (str/list have no such methods):
        assert name not in {"unlink", "rmtree", "rmdir", "rename", "copyfile", "copy2", "copytree", "chmod",
                            "truncate", "startfile", "popen", "spawnl", "execv", "symlink_to",
                            "hardlink_to"}, f"{loc}: {owner}.{name}"
        # Names that are only destructive on os/shutil (str.replace, list.remove are fine):
        if owner in {"os", "shutil"}:
            assert name not in {"remove", "replace", "move", "copy", "system"}, f"{loc}: {owner}.{name}"
        if name == "open" and len(node.args) >= 2:
            mode = ast.unparse(node.args[1])
            assert "r" in mode and not any(c in mode for c in "wax+"), f"{loc}: open() with mode {mode}"


def test_file_writes_only_in_export_modules():
    allowed = {"inspector/reports/json_export.py", "inspector/reports/redaction.py", "inspector/app.py"}
    for path in INSPECTOR_AND_AI:
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        for node, name, _owner in _calls(tree):
            if name in {"write_text", "write_bytes", "mkdir"}:
                assert rel in allowed, f"{rel}:{node.lineno} writes a file"


def test_ai_layer_has_no_hardware_or_collector_imports():
    for path in (SRC / "ai").rglob("*.py"):
        text = path.read_text("utf-8")
        for bad in ("collectors", "safe_exec", "backends", "hardware", "serial", "pymmcore"):
            assert f"import {bad}" not in text and f".{bad} import" not in text, f"{path.name} imports {bad}"


def test_every_query_passes_validator():
    for q in safe_exec.QUERIES.values():
        safe_exec.validate_script(q.script)


@pytest.mark.parametrize("script", [
    "Set-ItemProperty -Path x", "Get-PnpDevice | Disable-PnpDevice", "pnputil /delete-driver oem1.inf",
    "Invoke-Expression $x", "$s = Get-CimInstance Win32_Service; $s.Delete()", "Start-Process cmd",
    "reg add HKLM\\x", "Restart-Computer", "chkdsk C: /f", "Write-Output $x", 'Get-Item "$env:x"',
    # PowerShell is case-insensitive and has short aliases for destructive cmdlets:
    "remove-item C:\\x", "set-itemproperty -Path x", "rm -Recurse C:\\x", "Get-ChildItem x | ri -Force",
    "Get-Process x | kill -Force", "ni C:\\x -ItemType File",
])
def test_validator_blocks_state_changes(script):
    with pytest.raises(safe_exec.UnsafeScriptError):
        safe_exec.validate_script(script)


def test_validator_ignores_text_inside_single_quotes():
    safe_exec.validate_script("Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-Kernel-PnP/Configuration' }")  # noqa: E501


def test_run_refuses_unknown_query_and_non_windows(monkeypatch):
    with pytest.raises(safe_exec.QueryError):
        safe_exec.run("Remove-Everything")
    monkeypatch.setattr(safe_exec, "is_windows", lambda: False)
    with pytest.raises(safe_exec.PlatformUnsupported):
        safe_exec.run("pnp_devices")


def test_stdin_only_for_declared_queries(monkeypatch):
    monkeypatch.setattr(safe_exec, "is_windows", lambda: True)
    with pytest.raises(safe_exec.QueryError, match="stdin"):
        safe_exec.run("pnp_devices", stdin_obj=["x"])


def test_int_param_bounds():
    q = safe_exec.QUERIES["pnp_events"]
    assert "AddHours(-24)" in q.render(hours=24)
    with pytest.raises(safe_exec.QueryError):
        q.render(hours=0)
    with pytest.raises(safe_exec.QueryError):
        q.render()
    with pytest.raises(ValueError):
        q.render(hours="24; Remove-Item x")


def test_parse_output_normalises():
    assert safe_exec.parse_output("", returns_list=True) == []
    assert safe_exec.parse_output("﻿{\"a\":1}", returns_list=True) == [{"a": 1}]
    assert safe_exec.parse_output("{\"a\":1}", returns_list=False) == {"a": 1}
