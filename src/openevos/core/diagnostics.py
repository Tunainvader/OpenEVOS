"""Read-only loader for Inspector scan folders (`openevos-scans/<scan_id>/*.json`).

The Diagnostics view shows what the Inspector recorded; it never re-labels, re-ranks or edits
anything. Files are parsed with the Inspector's own schemas when they validate (the evidence
register also re-checks every evidence hash). If a file fails validation it is still shown from
its raw JSON, and the problem is listed so the operator can see it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from openevos.inspector.evidence import EvidenceRegister
from openevos.inspector.schemas import DeviceInventory, ScanManifest

MAX_FILE_BYTES = 200 * 1024 * 1024
MANIFEST = "scan_manifest.json"
REGISTER = "evidence_register.json"
DEVICES = "device_inventory.json"
REPORT = "openevos_diagnostic_report.md"


class ScanLoadError(RuntimeError):
    pass


@dataclass
class ScanBundle:
    folder: Path
    meta: dict[str, Any] = field(default_factory=dict)
    collectors: list[dict[str, Any]] = field(default_factory=list)
    actions_not_performed: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    serial_ports: list[dict[str, Any]] = field(default_factory=list)
    evidence_count: int = 0
    register_validated: bool = False
    report_markdown: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def scan_id(self) -> str:
        return str(self.meta.get("scan_id", self.folder.name))

    @property
    def label(self) -> str:
        return str(self.meta.get("label", ""))

    def label_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[str(f.get("label"))] = out.get(str(f.get("label")), 0) + 1
        return out


def _read_json(path: Path, problems: list[str]) -> Any | None:
    if not path.is_file():
        problems.append(f"{path.name}: missing")
        return None
    if path.stat().st_size > MAX_FILE_BYTES:
        problems.append(f"{path.name}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB, not loaded")
        return None
    try:
        return json.loads(path.read_text("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"{path.name}: unreadable ({exc})")
        return None


def load_scan(folder: str | Path) -> ScanBundle:
    folder = Path(folder)
    if not folder.is_dir():
        raise ScanLoadError(f"{folder} is not a folder")
    if not (folder / MANIFEST).is_file() and not (folder / REGISTER).is_file():
        raise ScanLoadError(f"{folder} does not look like an Inspector scan "
                            f"(no {MANIFEST} or {REGISTER})")
    b = ScanBundle(folder=folder)

    raw = _read_json(folder / MANIFEST, b.problems)
    if raw is not None:
        try:
            m = ScanManifest.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            b.problems.append(f"{MANIFEST}: schema validation failed ({exc.error_count()} errors)")
            m = raw if isinstance(raw, dict) else {}
        b.meta = dict(m.get("meta") or {})
        b.collectors = list(m.get("collectors") or [])
        b.actions_not_performed = list(m.get("actions_not_performed") or [])

    raw = _read_json(folder / REGISTER, b.problems)
    if raw is not None:
        try:
            reg = EvidenceRegister.model_validate(raw)
            b.findings = [f.model_dump(mode="json") for f in reg.findings]
            b.evidence_count = len(reg.evidence)
            b.register_validated = True
        except ValidationError as exc:
            b.problems.append(f"{REGISTER}: validation failed ({exc.error_count()} errors); "
                              "showing raw findings")
            b.findings = list((raw or {}).get("findings") or []) if isinstance(raw, dict) else []
            b.evidence_count = len((raw or {}).get("evidence") or {}) if isinstance(raw, dict) else 0
        if not b.meta and isinstance(raw, dict):
            b.meta = {"scan_id": raw.get("scan_id", folder.name)}

    raw = _read_json(folder / DEVICES, b.problems)
    if raw is not None:
        try:
            inv = DeviceInventory.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            b.problems.append(f"{DEVICES}: schema validation failed ({exc.error_count()} errors)")
            inv = raw if isinstance(raw, dict) else {}
        b.devices = list(inv.get("devices") or [])
        b.serial_ports = list(inv.get("serial_ports") or [])

    report = folder / REPORT
    if report.is_file() and report.stat().st_size <= MAX_FILE_BYTES:
        try:
            b.report_markdown = report.read_text("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            b.problems.append(f"{REPORT}: unreadable ({exc})")
    return b


def find_scans(base: str | Path) -> list[Path]:
    """Sub-folders of `base` (one level) that contain a scan manifest, newest first."""
    base = Path(base)
    if not base.is_dir():
        return []
    scans = [p for p in base.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]
    return sorted(scans, key=lambda p: (p / MANIFEST).stat().st_mtime, reverse=True)
