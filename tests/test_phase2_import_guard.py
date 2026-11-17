"""Static tripwires for the Phase 2 packages (simulation first; nothing physical).

* `ui/`, `hal/`, `core/` and `backends/simulation/` import no hardware transport, no
  Micro-Manager binding, no FFI, no process spawning and no networking (a local STOP must not
  depend on anything remote).
* The UI never imports a backend: it reaches devices only through core services and the HAL.
* `hal/` and `core/` do not depend on Qt.
* The AI layer cannot reach the command path.

Like tests/test_readonly_guard.py, these catch obvious regressions and force them through review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "openevos"
GUARDED = ["ui", "hal", "core", "backends/simulation", "backends/rig",
           "instrument", "acquisition", "imaging", "analysis"]  # UI merge: both UIs + the v0.4.0 layers
FILES = sorted({p for d in GUARDED for p in (SRC / d).rglob("*.py")} | {SRC / "backends" / "__init__.py"})

FORBIDDEN_TOP = {
    # hardware transports and SDK bindings
    "serial", "usb", "hid", "hidapi", "pywinusb", "ftd2xx", "pyftdi", "pymmcore", "pymmcore_plus",
    "pycromanager", "PySpin", "pyueye", "flycapture2",
    # FFI / process spawning
    "ctypes", "cffi", "subprocess", "multiprocessing",
    # networking
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websockets",
    "websocket", "ftplib", "smtplib",
}
FORBIDDEN_CALLS = {("os", "system"), ("os", "popen"), ("os", "startfile"), ("os", "execv"),
                   ("os", "execl"), ("os", "spawnl"), ("os", "spawnv")}


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module))
    return out


def test_guard_covers_the_phase2_packages():
    rels = {_rel(p) for p in FILES}
    for must in ("hal/adapter.py", "core/command_language.py", "core/core_gateway.py",
                 "core/controller.py", "backends/simulation/camera.py",
                 "ui/shell.py", "acquisition/controller.py", "backends/rig/rig.py"):
        assert must in rels
    for retired in ("hal/gateway.py", "hal/command_policy.py", "hal/commands.py"):  # ADR-0010
        assert retired not in rels


@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_no_transport_ffi_process_or_network_imports(path):
    tree = ast.parse(path.read_text("utf-8"))
    for line, name in _imports(tree):
        top = name.split(".")[0]
        assert top not in FORBIDDEN_TOP, f"{_rel(path)}:{line} imports '{name}'"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in {"__import__", "exec", "eval"}:
                pytest.fail(f"{_rel(path)}:{node.lineno} calls {f.id}()")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                assert (f.value.id, f.attr) not in FORBIDDEN_CALLS, f"{_rel(path)}:{node.lineno}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "\\\\.\\" not in node.value, f"{_rel(path)}: Win32 device path literal"


@pytest.mark.parametrize("path", [p for p in FILES if _rel(p).startswith("ui/")], ids=_rel)
def test_ui_never_imports_a_backend(path):
    tree = ast.parse(path.read_text("utf-8"))
    for line, name in _imports(tree):
        assert not name.startswith("openevos.backends"), f"{_rel(path)}:{line} imports {name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "openevos":
            assert "backends" not in {a.name for a in node.names}, f"{_rel(path)}:{node.lineno}"


@pytest.mark.parametrize("path", [p for p in FILES if _rel(p).startswith(("hal/", "core/", "backends/", "instrument/", "acquisition/",
                                                                     "imaging/", "analysis/"))],
                         ids=_rel)
def test_core_and_hal_are_ui_free(path):
    for line, name in _imports(ast.parse(path.read_text("utf-8"))):
        assert not name.startswith(("PySide6", "PyQt", "openevos.ui")), f"{_rel(path)}:{line} {name}"


def test_dynamic_backend_loading_is_limited_to_registered_backends():
    from openevos.backends import BACKENDS

    for name, target in BACKENDS.items():
        module, _, attr = target.partition(":")
        assert module.startswith("openevos.backends.") and attr, (name, target)
    # importlib is used in exactly one place, with targets from BACKENDS only
    # (`importlib.resources` reads packaged data files, it loads no module: allowed anywhere)
    users = [_rel(p) for p in FILES if "import_module" in p.read_text("utf-8")]
    assert users == ["core/instrument.py"]


def test_maintenance_tools_are_unreachable_from_the_application():
    """ADR-0011: reviewed device reads run only as operator CLI tools, never from the core or UI."""
    for path in FILES:
        for line, name in _imports(ast.parse(path.read_text("utf-8"))):
            assert not name.startswith("openevos.maintenance"), f"{_rel(path)}:{line} imports {name}"


def test_ai_layer_cannot_reach_the_command_path():
    for path in (SRC / "ai").rglob("*.py"):
        for line, name in _imports(ast.parse(path.read_text("utf-8"))):
            assert not name.startswith(("openevos.hal.adapter", "openevos.safety",
                                        "openevos.core", "openevos.backends", "openevos.ui")), \
                f"ai/{path.name}:{line} imports {name}"
