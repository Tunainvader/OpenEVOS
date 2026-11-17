"""OpenEVOS Inspector v0.1 command-line interface.

    openevos-inspector scan  [--label baseline] [--out DIR] [--artifact-root PATH ...] [--redact]
    openevos-inspector diff  BASELINE_DIR OTHER_DIR [--recovered IMAGE_SCAN_DIR] [--out DIR]
    openevos-inspector validate-ai RESPONSE.json --scan SCAN_DIR
    openevos-inspector collectors

There is intentionally no --read-only flag: read-only is the only mode the Inspector has.
"""

from __future__ import annotations

import argparse
import os
import platform
import secrets
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from openevos.inspector import TOOL_VERSION, safe_exec
from openevos.inspector.analysis.diff import diff_scans
from openevos.inspector.analysis.rules import run_rules
from openevos.inspector.collectors import (
    Collector,
    CollectorContext,
    CollectorSkipped,
    ScanResults,
)
from openevos.inspector.collectors.camera_sdks import CameraSdksCollector
from openevos.inspector.collectors.drivers import DriverPackagesCollector, SignedDriversCollector
from openevos.inspector.collectors.evos_artifacts import EvosArtifactsCollector
from openevos.inspector.collectors.installed_software import InstalledSoftwareCollector
from openevos.inspector.collectors.logs import EventLogCollector
from openevos.inspector.collectors.serial_ports import SerialPortsCollector
from openevos.inspector.collectors.system import SystemCollector
from openevos.inspector.collectors.windows_devices import WindowsDevicesCollector
from openevos.inspector.evidence import EvidenceRegister, utcnow
from openevos.inspector.reports.json_export import write_exports
from openevos.inspector.reports.markdown_export import render_diff_markdown, render_report
from openevos.inspector.reports.redaction import (
    Redactor,
    collect_sensitive_values,
    load_or_create_key,
)
from openevos.inspector.schemas import (
    CollectorRun,
    DeviceInventory,
    DriverInventory,
    ScanManifest,
    ScanMeta,
    SoftwareInventory,
    SystemInventory,
)

REPORT_NAME = "openevos_diagnostic_report.md"

ACTIONS_NOT_PERFORMED = [
    "No device was opened through a driver, SDK or Micro-Manager.",
    "No serial port was opened; no data was sent to any COM port or USB endpoint.",
    "No driver was installed, removed, enabled, disabled or updated.",
    "No registry value, service, file, firmware or configuration was modified.",
    "No filesystem check or repair (e.g. CHKDSK) was run; no source disk or image was written.",
    "No file was copied out of an EVOS installation; only metadata and hashes were recorded.",
    "No data was sent to any AI provider or network service.",
]


def default_collectors() -> list[Collector]:
    return [
        SystemCollector(),
        WindowsDevicesCollector(),
        SerialPortsCollector(),
        SignedDriversCollector(),
        DriverPackagesCollector(),
        InstalledSoftwareCollector(),
        EvosArtifactsCollector(),
        CameraSdksCollector(),
        EventLogCollector(),
    ]


def is_elevated() -> bool | None:
    try:
        if sys.platform == "win32":
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def run_scan(
    label: str,
    ctx: CollectorContext | None = None,
    collectors: list[Collector] | None = None,
    skip: set[str] | None = None,
) -> tuple[ScanManifest, ScanResults, EvidenceRegister]:
    started = utcnow()
    scan_id = f"{label}-{started:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    register = EvidenceRegister(scan_id=scan_id)
    if ctx is None:
        ctx = CollectorContext(scan_id=scan_id, register=register)
    else:
        ctx.scan_id, ctx.register = scan_id, register
    if ctx.registry_reader is None and ctx.is_windows:
        from openevos.inspector.collectors.registry import WinRegReader

        ctx.registry_reader = WinRegReader()

    meta = ScanMeta(
        scan_id=scan_id, label=label, tool_version=TOOL_VERSION, started_at=started,
        host_platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python_version=platform.python_version(), elevated=is_elevated(),
        artifact_roots=[str(r) for r in ctx.artifact_roots],
    )
    manifest = ScanManifest(meta=meta, actions_not_performed=list(ACTIONS_NOT_PERFORMED))
    if meta.elevated:
        manifest.notes.append("Running elevated is not required for v0.1; prefer a standard user session.")
    results = ScanResults()

    for c in collectors or default_collectors():
        t0, ev0 = time.perf_counter(), len(register.evidence)
        ctx.ops = []
        status, reason = "ok", ""
        if skip and c.name in skip:
            status, reason = "skipped", "skipped by user (--skip)"
        elif c.requires_windows and not ctx.is_windows:
            status, reason = "skipped", "requires Windows"
        else:
            try:
                warn = c.collect(ctx, results)
                if warn:
                    status, reason = "partial", warn
            except (CollectorSkipped, safe_exec.PlatformUnsupported) as exc:
                status, reason = "skipped", str(exc)
            except Exception as exc:  # noqa: BLE001 — one failing collector must not abort the scan
                status, reason = "failed", f"{exc.__class__.__name__}: {exc}"[:500]
        manifest.collectors.append(
            CollectorRun(
                name=c.name, status=status, reason=reason, started_at=utcnow(),
                duration_s=time.perf_counter() - t0, evidence_count=len(register.evidence) - ev0,
                operations=sorted(set(ctx.ops))[:500],
            )
        )

    run_rules(results, register, label)
    meta.finished_at = utcnow()
    return manifest, results, register


def build_models(manifest: ScanManifest, results: ScanResults, register: EvidenceRegister) -> dict:
    meta = manifest.meta
    return {
        "system": SystemInventory(meta=meta, system=results.system, events=results.events),
        "devices": DeviceInventory(meta=meta, devices=results.devices, serial_ports=results.serial_ports),
        "drivers": DriverInventory(meta=meta, drivers=results.drivers, packages=results.packages),
        "software": SoftwareInventory(
            meta=meta, programs=results.programs, vendor_registry_keys=results.vendor_registry_keys,
            artifacts=results.artifacts, sdk_matches=results.sdk_matches, micromanager=results.micromanager,
        ),
        "register": register,
        "manifest": manifest,
    }


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------
def cmd_scan(args: argparse.Namespace) -> int:
    roots = [Path(r) for r in args.artifact_root or []]
    for r in roots:
        if not r.is_dir():
            print(f"error: artifact root not found: {r}", file=sys.stderr)
            return 2
    out_base = Path(args.out)
    for r in roots:
        if _inside(out_base, r):
            print(f"error: output directory {out_base} is inside artifact root {r}; refusing to write there",
                  file=sys.stderr)
            return 2

    ctx = CollectorContext(
        scan_id="", register=EvidenceRegister(scan_id=""), artifact_roots=roots,
        include_default_roots=not args.no_default_roots, events_hours=args.events_hours,
        max_artifact_files=args.max_artifact_files,
    )
    print(f"OpenEVOS Inspector {TOOL_VERSION} — read-only scan, label '{args.label}'")
    manifest, results, register = run_scan(args.label, ctx, skip=set(args.skip or []))

    redactor = None
    if args.redact:
        redactor = Redactor(load_or_create_key(), collect_sensitive_values(results, args.redact_value))
        manifest.meta.redacted = True

    out_dir = out_base / manifest.meta.scan_id
    written = write_exports(out_dir, build_models(manifest, results, register), redactor)
    report = render_report(manifest, results, register)
    if redactor is not None:
        report = redactor.text(report)
    (out_dir / REPORT_NAME).write_text(report, "utf-8")

    for c in manifest.collectors:
        print(f"  {c.name:<20} {c.status:<8} {c.evidence_count:>5} evidence  {c.reason}")
    print(f"\nWrote {len(written) + 1} files to {out_dir}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    rec = Path(args.recovered) if args.recovered else None
    diff = diff_scans(Path(args.baseline), Path(args.other), rec)
    out = Path(args.out) if args.out else Path(args.other)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"diff_{diff.baseline.label}_vs_{diff.other.label}"
    (out / f"{stem}.json").write_text(diff.model_dump_json(indent=2), "utf-8")
    (out / f"{stem}.md").write_text(render_diff_markdown(diff), "utf-8")
    print(f"{len(diff.added)} added, {len(diff.removed)} removed, {len(diff.changed)} changed "
          f"({len(diff.physical_groups)} physical device group(s)). Wrote {out / stem}.md/.json")
    for n in diff.notes:
        print(f"note: {n}")
    return 0


def cmd_validate_ai(args: argparse.Namespace) -> int:
    from openevos.ai.schemas import AITriageResponse, validate_against_register

    reg = EvidenceRegister.model_validate_json(
        (Path(args.scan) / "evidence_register.json").read_text("utf-8")
    )
    try:
        resp = AITriageResponse.model_validate_json(Path(args.response).read_text("utf-8"))
    except ValidationError as exc:
        print("REJECTED (schema):")
        for e in exc.errors():
            print(f"  {'.'.join(str(x) for x in e['loc'])}: {e['msg']}")
        return 1
    violations = validate_against_register(resp, reg)
    if violations:
        print("REJECTED (evidence policy):")
        for v in violations:
            print(f"  {v.where}: {v.message}")
        return 1
    print("ACCEPTED: schema valid and all citations resolve.")
    return 0


def cmd_collectors(_args: argparse.Namespace) -> int:
    for c in default_collectors():
        need = "Windows" if c.requires_windows else "any OS"
        print(f"{c.name:<20} {need:<8} admin={'yes' if c.needs_admin else 'no ':<3}  {c.description}")
    print("\nRead-only PowerShell queries:")
    for q in safe_exec.QUERIES.values():
        print(f"  {q.key:<22} {q.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openevos-inspector", description="OpenEVOS Inspector v0.1 (read-only)")
    p.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="run a read-only inventory scan")
    s.add_argument("--label", default="baseline",
                   help="snapshot label: baseline | microscope | amc1000 | recovered-image | <free text>")
    s.add_argument("--out", default="openevos-scans", help="output base directory (a subfolder per scan)")
    s.add_argument("--artifact-root", action="append", metavar="PATH",
                   help="extra root to search, e.g. a read-only mount of the recovered disk image (repeatable)")
    s.add_argument("--no-default-roots", action="store_true",
                   help="do not search the live system's Program Files/ProgramData/AppData")
    s.add_argument("--events-hours", type=int, default=72, help="event-log window in hours (0 disables)")
    s.add_argument("--max-artifact-files", type=int, default=20000)
    s.add_argument("--skip", action="append", metavar="COLLECTOR", help="skip a collector (repeatable)")
    s.add_argument("--redact", action="store_true", help="redact computer name, serials and user names")
    s.add_argument("--redact-value", action="append", default=[], metavar="TEXT",
                   help="additional literal to redact, e.g. an institution name (repeatable)")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("diff", help="compare two scans (e.g. baseline vs microscope)")
    d.add_argument("baseline")
    d.add_argument("other")
    d.add_argument("--recovered", help="scan of the recovered EVOS disk image for INF cross-referencing")
    d.add_argument("--out", help="output directory (default: the OTHER scan's folder)")
    d.set_defaults(func=cmd_diff)

    v = sub.add_parser("validate-ai", help="validate an AI triage response against a scan's evidence")
    v.add_argument("response")
    v.add_argument("--scan", required=True)
    v.set_defaults(func=cmd_validate_ai)

    c = sub.add_parser("collectors", help="list collectors and read-only queries")
    c.set_defaults(func=cmd_collectors)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
