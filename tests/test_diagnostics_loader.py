"""Read-only loader behind the Diagnostics view."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from openevos.core.diagnostics import ScanLoadError, find_scans, load_scan
from openevos.inspector.evidence import EvidenceRegister

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
SCANS = [p for p in sorted(EXAMPLES.iterdir()) if (p / "scan_manifest.json").is_file()] \
    if EXAMPLES.is_dir() else []


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir()}


@pytest.mark.skipif(not SCANS, reason="no example scans in docs/examples")
@pytest.mark.parametrize("folder", SCANS, ids=lambda p: p.name)
def test_example_scans_load_and_validate(folder):
    raw = json.loads((folder / "evidence_register.json").read_text("utf-8"))
    devices = json.loads((folder / "device_inventory.json").read_text("utf-8"))
    try:  # the loader must agree with the Inspector's own integrity check
        EvidenceRegister.model_validate(raw)
        inspector_ok = True
    except ValidationError:
        inspector_ok = False
    before = _snapshot(folder)
    b = load_scan(folder)
    assert _snapshot(folder) == before  # nothing written or touched
    assert b.register_validated is inspector_ok
    if inspector_ok:
        assert b.problems == []
    else:
        assert len(b.problems) == 1 and "evidence_register.json: validation failed" in b.problems[0]
    assert b.evidence_count == len(raw["evidence"])
    assert len(b.findings) == len(raw["findings"])  # shown either way
    assert len(b.devices) == len(devices["devices"])
    assert b.scan_id == raw["scan_id"]
    assert b.collectors and "# OpenEVOS" in b.report_markdown
    assert sum(b.label_counts().values()) == len(b.findings)


@pytest.mark.skipif(not SCANS, reason="no example scans in docs/examples")
def test_tampered_register_is_flagged_but_still_shown(tmp_path):
    src = next(p for p in SCANS if p.name == "microscope")
    dst = tmp_path / "tampered"
    shutil.copytree(src, dst)
    reg_path = dst / "evidence_register.json"
    reg = json.loads(reg_path.read_text("utf-8"))
    first = next(iter(reg["evidence"]))
    reg["evidence"][first]["data"]["tampered"] = True  # sha256 no longer matches
    reg_path.write_text(json.dumps(reg), "utf-8")
    b = load_scan(dst)
    assert not b.register_validated
    assert any("evidence_register.json" in p for p in b.problems)
    assert len(b.findings) == len(reg["findings"])


def test_rejects_folders_that_are_not_scans(tmp_path):
    with pytest.raises(ScanLoadError):
        load_scan(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ScanLoadError):
        load_scan(tmp_path / "empty")


def test_partial_and_corrupt_scans_report_problems(tmp_path):
    d = tmp_path / "partial"
    d.mkdir()
    (d / "scan_manifest.json").write_text("{not json", "utf-8")
    (d / "evidence_register.json").write_text(json.dumps({"scan_id": "x", "evidence": {}, "findings": []}),
                                              "utf-8")
    b = load_scan(d)
    assert b.scan_id == "x" and b.findings == []
    assert any("scan_manifest.json: unreadable" in p for p in b.problems)
    assert any("device_inventory.json: missing" in p for p in b.problems)


def test_find_scans_lists_scan_folders(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "scan_manifest.json").write_text("{}", "utf-8")
    (tmp_path / "not-a-scan").mkdir()
    assert sorted(p.name for p in find_scans(tmp_path)) == ["a", "b"]
    assert find_scans(tmp_path / "nope") == []
