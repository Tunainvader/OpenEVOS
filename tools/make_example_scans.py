"""Regenerate docs/examples/ from the SYNTHETIC test fixtures (no real hardware involved).

    python tools/make_example_scans.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import conftest

from openevos.inspector.analysis.diff import diff_scans
from openevos.inspector.app import REPORT_NAME, build_models, run_scan
from openevos.inspector.collectors import CollectorContext
from openevos.inspector.evidence import EvidenceRegister
from openevos.inspector.reports.json_export import write_exports
from openevos.inspector.reports.markdown_export import render_diff_markdown, render_report

BANNER = "> **SYNTHETIC EXAMPLE** generated from test fixtures. VID/PIDs 1234:5678 and ABCD:0001 are fake.\n\n"


def scan(label, snapshot, roots, out):
    ctx = CollectorContext(
        scan_id="", register=EvidenceRegister(scan_id=""), runner=conftest.FakeRunner(snapshot),
        is_windows=True, artifact_roots=roots, include_default_roots=False, events_hours=24,
        registry_reader=conftest.fake_registry(), serial_lister=conftest.fake_ports(snapshot),
        environ={"SystemRoot": "/nonexistent/Windows"},
    )
    manifest, res, reg = run_scan(label, ctx)
    d = out / label
    write_exports(d, build_models(manifest, res, reg))
    (d / REPORT_NAME).write_text(BANNER + render_report(manifest, res, reg), "utf-8")
    return d


def main():
    out = ROOT / "docs" / "examples"
    shutil.rmtree(out, ignore_errors=True)
    tmp = Path(tempfile.mkdtemp())
    img = conftest.build_fake_image(tmp / "img")
    base = scan("baseline", "baseline", [], out)
    scope = scan("microscope", "microscope", [], out)
    rec = scan("recovered-image", "baseline", [img], out)
    d = diff_scans(base, scope, rec)
    (out / "diff_baseline_vs_microscope.md").write_text(BANNER + render_diff_markdown(d), "utf-8")
    (out / "diff_baseline_vs_microscope.json").write_text(d.model_dump_json(indent=2), "utf-8")
    # make temp paths readable
    for f in out.rglob("*"):
        if f.is_file():
            f.write_text(f.read_text("utf-8").replace(str(tmp), "<mounted-image>"), "utf-8")
    print(f"wrote examples to {out}")


if __name__ == "__main__":
    main()
