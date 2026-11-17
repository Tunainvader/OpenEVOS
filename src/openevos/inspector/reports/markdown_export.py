"""Human-readable reports. Structure follows plan §8.3 and the five-way split in §18:
what Windows reported / what drivers & files identify / what is inferred / what is unknown /
which read-only step would reduce uncertainty next.
"""

from __future__ import annotations

from collections import Counter

from openevos.inspector.analysis.diff import DiffResult
from openevos.inspector.collectors import ScanResults
from openevos.inspector.collectors.usb_devices import build_tree
from openevos.inspector.evidence import EvidenceLabel, EvidenceRegister
from openevos.inspector.schemas import ScanManifest

L = EvidenceLabel
MAX_ROWS = 200


def _c(v) -> str:
    if v is None or v == "":
        return "—"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows[:MAX_ROWS]:
        out.append("| " + " | ".join(_c(x) for x in r) + " |")
    if len(rows) > MAX_ROWS:
        out.append(f"\n_{len(rows) - MAX_ROWS} more rows in the JSON export._")
    out.append("")
    return out


def _ev(ids: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids[:3]) + (" …" if len(ids) > 3 else "")


def render_report(manifest: ScanManifest, results: ScanResults, register: EvidenceRegister) -> str:
    m = manifest.meta
    usb_present = [d for d in results.devices if d.usb and d.present is not False]
    usb_phantom = [d for d in results.devices if d.usb and d.present is False]
    problems = [d for d in results.devices if d.present is not False and d.problem_code not in (None, 0)]
    label_counts = Counter(f.label for f in register.findings)
    recovered_pkgs = [p for p in results.packages if p.source == "recovered"]

    o: list[str] = [
        "# OpenEVOS diagnostic report",
        "",
        f"- **Snapshot label:** `{m.label}`",
        f"- **Scan ID:** `{m.scan_id}`",
        (f"- **Started / finished (UTC):** {m.started_at:%Y-%m-%d %H:%M:%S} / {m.finished_at:%Y-%m-%d %H:%M:%S}"
         if m.finished_at else f"- **Started (UTC):** {m.started_at:%Y-%m-%d %H:%M:%S}"),
        f"- **Tool / schema:** {m.tool_version} / {m.schema_version}",
        f"- **Host:** {m.host_platform}; Python {m.python_version}; elevated: {_c(m.elevated)}; redacted: {m.redacted}",
        f"- **Artifact roots:** {', '.join(m.artifact_roots) or '—'}",
        "",
        ("> This report was produced by a read-only scan. No device was opened, no serial port or USB endpoint "
         "was written to, and nothing was changed on this system. Labels follow the OpenEVOS evidence policy: "
         "*Verified* = read directly from Windows or a file; *Probable / Strongly supported* = inference with "
         "alternatives; *Unknown / Not tested* = no basis yet."),
        "",
        "## 1. Summary",
        "",
    ]
    o += _table(
        ["Item", "Count"],
        [
            ["USB devices/interfaces present", len(usb_present)],
            ["USB devices remembered but not attached", len(usb_phantom)],
            ["Present devices with a Windows problem code", len(problems)],
            ["Serial (COM) ports", len(results.serial_ports)],
            ["Third-party driver packages (oem*.inf)", len(results.packages)],
            ["EVOS-related files", len(results.artifacts)],
            ["Camera SDK / USB-bridge library matches", len(results.sdk_matches)],
            ["Micro-Manager installations", len(results.micromanager)],
        ]
        + [[f"Findings — {lab.display}", label_counts.get(lab, 0)] for lab in EvidenceLabel],
    )

    # ---------------------------------------------------------------- 2. Windows reported
    o += ["## 2. What Windows directly reported (Verified)", ""]
    s = results.system
    if s:
        o += ["### Host system", ""]
        o += _table(
            ["Field", "Value"],
            [
                ["Computer", s.computer_name], ["Manufacturer / model", f"{s.manufacturer} / {s.model}"],
                ["OS", f"{s.os_caption} {s.os_version} (build {s.os_build}, {s.os_architecture})"],
                ["BIOS", f"{s.bios_vendor} {s.bios_version} ({s.bios_date})"],
                ["Baseboard", f"{s.baseboard_manufacturer} {s.baseboard_product}"],
                ["RAM (GiB)", round(s.total_memory_bytes / 2**30, 1) if s.total_memory_bytes else None],
                ["CPU", "; ".join(str(c.get("Name")) for c in s.cpus)],
                ["USB host controllers", "; ".join(str(c.get("Name")) for c in s.usb_controllers)],
                [".NET Framework 4 release", s.dotnet_framework_release],
            ],
        )
    else:
        o += ["_Host system profile not collected (non-Windows host or collector skipped)._", ""]

    o += ["### USB topology (present devices)", ""]
    tree = build_tree(results.devices)
    if tree:
        o.append("```text")
        for depth, d in tree:
            ident = d.usb.vid_pid if d.usb and d.usb.vid_pid else d.instance_id.split("\\")[1] if "\\" in d.instance_id else d.instance_id
            mi = f" MI_{d.usb.mi}" if d.usb and d.usb.mi else ""
            br = f' "{d.bus_reported_description}"' if d.bus_reported_description else ""
            prob = f" [problem {d.problem_code}]" if d.problem_code not in (None, 0) else ""
            o.append(f"{'  ' * depth}- {ident}{mi}  {d.friendly_name or '?'}{br}  ({d.service or 'no driver'}){prob}")
        o += ["```", ""]
    else:
        o += ["_No USB topology collected._", ""]

    o += ["### Devices with a Windows problem code", ""]
    o += _table(["Device", "Instance ID", "Code", "Problem", "Evidence"],
                [[d.friendly_name, d.instance_id, d.problem_code, d.problem, _ev(d.evidence_ids)] for d in problems])
    o += ["### Serial ports (listed only — never opened)", ""]
    o += _table(["Port", "Description", "VID:PID", "Manufacturer", "Evidence"],
                [[p.device, p.description, f"{p.vid}:{p.pid}" if p.vid else None, p.manufacturer, _ev(p.evidence_ids)]
                 for p in results.serial_ports])
    o += ["### USB devices Windows remembers but are not attached now", ""]
    o += _table(["VID:PID", "Name", "Bus-reported description", "Last arrival", "Instance ID"],
                [[d.usb.vid_pid, d.friendly_name, d.bus_reported_description, d.last_arrival, d.instance_id]
                 for d in usb_phantom])

    # ---------------------------------------------------------------- 3. drivers & files
    o += ["## 3. What drivers and files identify", "", "### Drivers bound to USB devices", ""]
    usb_ids = {d.instance_id.upper() for d in results.devices if d.usb}
    o += _table(["Device", "INF", "Provider", "Version", "Signed", "Evidence"],
                [[r.device_name, r.inf_name, r.provider, r.version, r.is_signed, _ev(r.evidence_ids)]
                 for r in results.drivers if r.device_id.upper() in usb_ids])
    o += ["### Third-party driver packages (oem*.inf)", ""]
    o += _table(["INF", "Windows volume", "Provider", "Class", "DriverVer", "USB IDs", "Keyword hits"],
                [[p.inf_path, p.windows_root, p.provider, p.device_class, p.driver_ver,
                  ", ".join(p.usb_ids[:6]) + (" …" if len(p.usb_ids) > 6 else ""), ", ".join(p.keyword_hits)]
                 for p in results.packages])
    if recovered_pkgs:
        o += [f"_{len(recovered_pkgs)} package(s) come from a recovered/mounted Windows volume._", ""]
    o += [f"### EVOS-related files ({len(results.artifacts)})", ""]
    o += _table(["Path", "Size", "Bitness", "Product / version", "SHA-256", "Hits"],
                [[a.path, a.size, a.pe.effective_bitness if a.pe else None,
                  " ".join(filter(None, [a.version_info.get("ProductName"), a.version_info.get("FileVersion")])) or None,
                  (a.sha256 or "")[:16] or a.skipped_hash_reason, ", ".join(a.keyword_hits)]
                 for a in results.artifacts])
    o += ["### Camera SDK / USB-bridge libraries (filename heuristics)", ""]
    o += _table(["Vendor / SDK", "Path", "Label", "Bitness", "Basis"],
                [[f"{x.vendor} / {x.sdk}", x.path, x.label.display, x.pe.effective_bitness if x.pe else None,
                  "; ".join(x.basis)] for x in results.sdk_matches])
    o += ["### Micro-Manager", ""]
    o += _table(["Path", "Adapters"],
                [[mm.path, ", ".join(mm.adapters[:30]) + (" …" if len(mm.adapters) > 30 else "")]
                 for mm in results.micromanager])

    # ---------------------------------------------------------------- 4. inferred
    inferred = [f for f in register.findings if f.label in (L.STRONGLY_SUPPORTED, L.PROBABLE)]
    o += ["## 4. What is inferred (not verified)", ""]
    o += _table(["Label", "Conf.", "Claim", "Alternatives", "Evidence"],
                [[f.label.display, f.confidence, f.claim, "; ".join(f.alternatives), _ev(f.evidence_ids)]
                 for f in sorted(inferred, key=lambda f: -(f.confidence or 0))])

    # ---------------------------------------------------------------- 5. unknown
    o += ["## 5. What remains unknown — documented EVOS FL Auto / AMC1000 profile", ""]
    o += _table(["Component", "Label", "Status"],
                [[f.component.removeprefix("role:"), f.label.display, f.claim]
                 for f in register.findings if f.rule_id == "R-PROFILE"])

    # ---------------------------------------------------------------- 6. next steps
    o += ["## 6. Next read-only steps that would reduce uncertainty", ""]
    o += [f"- {s}" for s in next_steps(m.label, results)] + [""]

    # ---------------------------------------------------------------- 7/8
    o += ["## 7. Actions not performed", ""]
    o += [f"- {a}" for a in manifest.actions_not_performed] + [""]
    o += ["## 8. Collector runs", ""]
    o += _table(["Collector", "Status", "Reason", "Duration (s)", "Evidence items"],
                [[c.name, c.status, c.reason, f"{c.duration_s:.2f}", c.evidence_count] for c in manifest.collectors])
    if manifest.notes:
        o += ["### Notes", ""] + [f"- {n}" for n in manifest.notes] + [""]
    o += ["---", "Every row above cites evidence IDs that resolve in `evidence_register.json`.", ""]
    return "\n".join(o)


def next_steps(label: str, results: ScanResults) -> list[str]:
    steps: list[str] = []
    has_recovered = any(p.source == "recovered" for p in results.packages)
    if label == "baseline":
        steps.append("Power the microscope, connect ONLY its documented USB cable (same port each time), wait for "
                     "Windows to settle, then run `openevos-inspector scan --label microscope` and "
                     "`openevos-inspector diff <baseline> <microscope>`.")
        steps.append("Disconnect the microscope, connect ONLY the AMC1000, and repeat with `--label amc1000`.")
    elif label in ("microscope", "amc1000"):
        steps.append("Run `openevos-inspector diff <baseline> <this scan>` to list newly enumerated devices.")
    elif has_recovered:
        steps.append("Pass this scan as `diff --recovered <this scan>` when comparing baseline and microscope "
                     "snapshots, so new VID:PIDs are matched against the original EVOS driver packages.")
    if not has_recovered:
        steps.append("Once the original OptiPlex disk has been imaged, mount a COPY of the image read-only and run "
                     "`openevos-inspector scan --label recovered-image --artifact-root <mount>` (works on Linux or "
                     "Windows). Then pass it to `diff --recovered` to cross-reference new VID:PIDs with the "
                     "original EVOS driver packages.")
    if any(d.usb and d.present is not False and not d.service for d in results.devices):
        steps.append("Some USB devices have no bound driver. Look up their VID:PID in the recovered INF packages "
                     "before installing anything; do not install drivers from untrusted sources.")
    if any(x.pe and x.pe.effective_bitness == "32" for x in results.sdk_matches):
        steps.append("A 32-bit SDK was found. Record its exact version; plan a 32-bit helper process before any "
                     "integration (ADR-0002).")
    steps.append("Photograph internal/rear controller labels where accessible without disassembly; record them as "
                 "operator evidence.")
    return steps


def render_diff_markdown(diff: DiffResult) -> str:
    o = [
        "# OpenEVOS snapshot diff",
        "",
        f"- **Baseline:** `{diff.baseline.label}` ({diff.baseline.scan_id}, {diff.baseline.started_at:%Y-%m-%d %H:%M})",
        f"- **Compared:** `{diff.other.label}` ({diff.other.scan_id}, {diff.other.started_at:%Y-%m-%d %H:%M})",
        f"- **Recovered-image scan:** {diff.recovered.scan_id if diff.recovered else '— (none supplied)'}",
        "",
        ("> Devices listed as *added* are **Detected** only. Their role (camera, stage controller, …) is "
         "**Unknown** until further evidence maps them."),
        "",
        "## Physical devices that appeared",
        "",
    ]
    o += _table(["Physical device", "VID:PID", "Interfaces"],
                [[g.physical_device_id, g.vid_pid, ", ".join(g.interface_ids) or "—"] for g in diff.physical_groups])
    o += ["## Added devices and interfaces", ""]
    o += _table(
        ["VID:PID", "MI", "Name", "Bus-reported", "Class", "Driver / service", "COM", "Seen before",
         "Candidate role (vendor-software evidence)", "Recovered INF match"],
        [[a.device.usb.vid_pid if a.device.usb else None, a.device.usb.mi if a.device.usb else None,
          a.device.friendly_name, a.device.bus_reported_description, a.device.device_class,
          f"{a.driver.inf_name} ({a.driver.provider})" if a.driver else a.device.service,
          ", ".join(a.serial_ports), "yes" if a.previously_known else "no",
          f"{a.candidate_role} ({a.candidate_label.replace('_', ' ')})" if a.candidate_role else None,
          "; ".join(f"{p.inf_path} [{p.provider}]" for p in a.recovered_inf_matches) or None]
         for a in diff.added],
    )
    o += ["## Removed devices", ""]
    o += _table(["Instance ID", "Name"], [[d.instance_id, d.friendly_name] for d in diff.removed])
    o += ["## Changed devices", ""]
    o += _table(["Instance ID", "Field", "Before", "After"],
                [[c.instance_id, c.field, c.before, c.after] for c in diff.changed])
    o += ["## Serial ports", ""]
    o += _table(["Change", "Port", "Description", "VID:PID"],
                [["added", p.device, p.description, f"{p.vid}:{p.pid}" if p.vid else None] for p in diff.added_serial_ports]
                + [["removed", p.device, p.description, f"{p.vid}:{p.pid}" if p.vid else None] for p in diff.removed_serial_ports])
    if diff.notes:
        o += ["## Notes", ""] + [f"- {n}" for n in diff.notes] + [""]
    return "\n".join(o)
