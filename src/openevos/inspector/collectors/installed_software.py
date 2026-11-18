"""Installed programs (Uninstall keys) and vendor registry roots — names and display values only."""

from __future__ import annotations

from openevos.inspector.collectors import Collector, CollectorContext, CollectorSkipped, ScanResults
from openevos.inspector.collectors.keywords import keyword_hits
from openevos.inspector.schemas import InstalledProgram, RegistryVendorKey

UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
SOFTWARE_ROOT = {"HKLM64": "SOFTWARE", "HKLM32": "SOFTWARE", "HKCU": "Software"}
FIELDS = ("DisplayName", "DisplayVersion", "Publisher", "InstallDate", "InstallLocation")


class InstalledSoftwareCollector(Collector):
    name = "installed_software"
    description = "Installed programs and EVOS-related vendor registry key names (read-only)"

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        reader = ctx.registry_reader
        if reader is None:
            raise CollectorSkipped("registry reader unavailable")

        for view in ("HKLM64", "HKLM32", "HKCU"):
            ctx.op(f"registry-read:{view}\\{UNINSTALL}")
            for sub in reader.subkeys(view, UNINSTALL):
                vals = reader.values(view, f"{UNINSTALL}\\{sub}")
                name = vals.get("DisplayName")
                if not name:
                    continue
                data = {k: vals.get(k) for k in FIELDS} | {"view": view, "key": sub}
                eid = ctx.add_evidence(self, "prog", f"{view}|{sub}", f"registry:{view}\\{UNINSTALL}\\{sub}", data)
                results.programs.append(
                    InstalledProgram(
                        name=str(name),
                        version=_s(vals.get("DisplayVersion")),
                        publisher=_s(vals.get("Publisher")),
                        install_date=_s(vals.get("InstallDate")),
                        install_location=_s(vals.get("InstallLocation")),
                        registry_view=view,
                        keyword_hits=keyword_hits(str(name), _s(vals.get("Publisher")), _s(vals.get("InstallLocation"))),  # noqa: E501
                        evidence_ids=[eid],
                    )
                )

            # Vendor roots: key *names* only, two levels deep, no values read.
            root = SOFTWARE_ROOT[view]
            ctx.op(f"registry-list-keys:{view}\\{root}")
            for vendor in reader.subkeys(view, root):
                hits = keyword_hits(vendor)
                children = reader.subkeys(view, f"{root}\\{vendor}") if hits else []
                candidates = [(vendor, hits)] + [
                    (f"{vendor}\\{c}", keyword_hits(vendor, c)) for c in children
                ]
                for path, h in candidates:
                    if not h:
                        continue
                    full = f"{view}\\{root}\\{path}"
                    eid = ctx.add_evidence(self, "regkey", full, f"registry:{full}", {"key": full})
                    results.vendor_registry_keys.append(
                        RegistryVendorKey(path=full, keyword_hits=h, evidence_ids=[eid])
                    )
        return None


def _s(v) -> str | None:
    return str(v) if v not in (None, "") else None
