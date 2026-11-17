"""Collector framework. Each collector is read-only and records every operation it performs."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openevos.inspector import safe_exec
from openevos.inspector.evidence import Evidence, EvidenceRegister, utcnow
from openevos.inspector.schemas import (
    DriverPackage,
    DriverRecord,
    EventRecord,
    FileArtifact,
    InstalledProgram,
    MicroManagerInstall,
    PnpDevice,
    RegistryVendorKey,
    SdkMatch,
    SerialPort,
    SystemInfo,
)


class CollectorSkipped(Exception):
    """Raise to mark a collector as skipped with a human-readable reason."""


@dataclass
class ScanResults:
    system: SystemInfo | None = None
    events: list[EventRecord] = field(default_factory=list)
    devices: list[PnpDevice] = field(default_factory=list)
    serial_ports: list[SerialPort] = field(default_factory=list)
    drivers: list[DriverRecord] = field(default_factory=list)
    packages: list[DriverPackage] = field(default_factory=list)
    programs: list[InstalledProgram] = field(default_factory=list)
    vendor_registry_keys: list[RegistryVendorKey] = field(default_factory=list)
    artifacts: list[FileArtifact] = field(default_factory=list)
    sdk_matches: list[SdkMatch] = field(default_factory=list)
    micromanager: list[MicroManagerInstall] = field(default_factory=list)


class CaseInsensitiveEnv(Mapping[str, str]):
    """Environment variables looked up without regard to case, as Windows itself does.

    A plain ``dict(os.environ)`` on Windows has UPPERCASE keys, so ``.get("ProgramFiles")``
    returns None and default search folders silently disappear. The first real Windows run
    (2026-11-12) found exactly that; tests/test_environment.py guards against it.
    """

    def __init__(self, source: Mapping[str, str] | None = None) -> None:
        self._data = {str(k).upper(): v for k, v in (source or {}).items()}

    def __getitem__(self, key: str) -> str:
        return self._data[key.upper()]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.upper() in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class CollectorContext:
    scan_id: str
    register: EvidenceRegister
    runner: Callable[..., Any] = safe_exec.run
    is_windows: bool = field(default_factory=safe_exec.is_windows)
    artifact_roots: list[Path] = field(default_factory=list)
    include_default_roots: bool = True
    events_hours: int | None = 72
    hash_size_limit: int = 256 * 1024 * 1024
    max_artifact_files: int = 20000
    registry_reader: Any = None  # RegistryReader; None => winreg on Windows
    serial_lister: Callable[[], Iterable[Any]] | None = None
    environ: Mapping[str, str] = field(default_factory=lambda: CaseInsensitiveEnv(os.environ))
    collected_at: datetime = field(default_factory=utcnow)
    ops: list[str] = field(default_factory=list)
    _walk_cache: dict[str, list[Path]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.environ, CaseInsensitiveEnv):
            self.environ = CaseInsensitiveEnv(self.environ)

    def env_dirs(self, names: Iterable[str]) -> tuple[list[Path], list[str]]:
        """Resolve folder-valued environment variables: (existing folders, names that did not resolve)."""
        found: list[Path] = []
        missing: list[str] = []
        for name in names:
            value = self.environ.get(name)
            if value and Path(value).is_dir():
                found.append(Path(value))
            else:
                missing.append(name)
        return found, missing

    def op(self, description: str) -> None:
        self.ops.append(description)

    def query(self, key: str, **kwargs: Any) -> Any:
        self.op(f"powershell:{key}")
        return self.runner(key, **kwargs)

    def add_evidence(
        self, collector: Collector, prefix: str, natural_key: str, source: str, data: dict
    ) -> str:
        ev = Evidence.create(
            prefix=prefix,
            natural_key=natural_key,
            collector=collector.name,
            collector_version=collector.version,
            source=source,
            data=data,
            collected_at=self.collected_at,
        )
        return self.register.add_evidence(ev)

    def walk_files(self, root: Path, skip_dir_names: frozenset[str] = frozenset()) -> list[Path]:
        """List files under root (cached; never follows symlinks; ignores permission errors)."""
        key = f"{root}|{','.join(sorted(skip_dir_names))}"
        if key in self._walk_cache:
            return self._walk_cache[key]
        self.op(f"list-directory-tree:{root}")
        out: list[Path] = []
        skip = {s.lower() for s in skip_dir_names}
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
            out.extend(Path(dirpath) / f for f in filenames)
        self._walk_cache[key] = out
        return out


class Collector(ABC):
    name: str = "abstract"
    version: str = "0.1.0"
    description: str = ""
    requires_windows: bool = True
    needs_admin: bool = False

    @abstractmethod
    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        """Populate `results`. Return a warning string for a partial result, else None."""


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def ps_value(value: Any) -> Any:
    """Normalise a scalar from Windows PowerShell 5.1 JSON.

    A calculated property whose script block outputs nothing serialises as ``{}`` (not null).
    The first real Windows run hit this on Win32_PnPSignedDriver.DriverDate.
    """
    if isinstance(value, (dict, list)) and not value:
        return None
    return value


def first(value: Any) -> dict:
    if isinstance(value, list):
        return value[0] if value else {}
    return value or {}


def child_ci(parent: Path, name: str) -> Path | None:
    """Case-insensitive child lookup (mounted NTFS images may be case-sensitive)."""
    try:
        for p in parent.iterdir():
            if p.name.lower() == name.lower():
                return p
    except OSError:
        return None
    return None


def windows_volume_roots(ctx: CollectorContext) -> list[Path]:
    """Volumes containing a Windows installation: the live system and/or recovered images."""
    roots: list[Path] = []
    if ctx.is_windows and ctx.include_default_roots:
        sysroot = Path(ctx.environ.get("SystemRoot", r"C:\Windows"))
        roots.append(sysroot.parent)
    for r in ctx.artifact_roots:
        win = child_ci(r, "Windows")
        if win is not None and child_ci(win, "INF") is not None:
            roots.append(r)
    seen: set[str] = set()
    uniq = []
    for r in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq
