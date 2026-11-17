"""Read-only Windows registry access behind a small interface (mockable in tests).

Only KEY_READ is ever requested. There is deliberately no write method on this interface.
"""

from __future__ import annotations

from typing import Any, Protocol

VIEWS = ("HKLM64", "HKLM32", "HKCU")


class RegistryReader(Protocol):
    def subkeys(self, view: str, path: str) -> list[str]: ...
    def values(self, view: str, path: str) -> dict[str, Any]: ...


class WinRegReader:
    """winreg-backed reader. Opens keys with KEY_READ only."""

    def __init__(self) -> None:
        import winreg

        self._w = winreg

    def _open(self, view: str, path: str):
        w = self._w
        if view == "HKLM64":
            return w.OpenKey(w.HKEY_LOCAL_MACHINE, path, 0, w.KEY_READ | w.KEY_WOW64_64KEY)
        if view == "HKLM32":
            return w.OpenKey(w.HKEY_LOCAL_MACHINE, path, 0, w.KEY_READ | w.KEY_WOW64_32KEY)
        if view == "HKCU":
            return w.OpenKey(w.HKEY_CURRENT_USER, path, 0, w.KEY_READ)
        raise ValueError(f"unknown registry view {view}")

    def subkeys(self, view: str, path: str) -> list[str]:
        try:
            with self._open(view, path) as k:
                n = self._w.QueryInfoKey(k)[0]
                return [self._w.EnumKey(k, i) for i in range(n)]
        except OSError:
            return []

    def values(self, view: str, path: str) -> dict[str, Any]:
        try:
            with self._open(view, path) as k:
                n = self._w.QueryInfoKey(k)[1]
                out: dict[str, Any] = {}
                for i in range(n):
                    name, data, _typ = self._w.EnumValue(k, i)
                    out[name] = data if isinstance(data, (str, int, list)) else repr(data)
                return out
        except OSError:
            return {}


class DictRegistryReader:
    """In-memory reader for tests: {view: {path: {"__subkeys__": [...], value: data}}}."""

    def __init__(self, tree: dict[str, dict[str, dict[str, Any]]]):
        self.tree = tree

    def subkeys(self, view: str, path: str) -> list[str]:
        return list(self.tree.get(view, {}).get(path, {}).get("__subkeys__", []))

    def values(self, view: str, path: str) -> dict[str, Any]:
        node = self.tree.get(view, {}).get(path, {})
        return {k: v for k, v in node.items() if k != "__subkeys__"}
