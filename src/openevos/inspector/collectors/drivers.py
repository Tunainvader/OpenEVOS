"""Driver evidence from two sources.

1. `SignedDriversCollector` (live Windows): drivers bound to devices (Win32_PnPSignedDriver).
2. `DriverPackagesCollector` (any OS): third-party INF packages (Windows/INF/oem*.inf) parsed
   read-only from the live system and from any recovered Windows volume passed with
   --artifact-root. On the original EVOS disk image this lists the vendor drivers that were
   installed for the microscope and the USB VID/PIDs they bind to — before the microscope is
   ever connected to the new PC.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import ValidationError

from openevos.inspector.collectors import (
    Collector,
    CollectorContext,
    CollectorSkipped,
    ScanResults,
    child_ci,
    ps_value,
    windows_volume_roots,
)
from openevos.inspector.collectors.keywords import keyword_hits
from openevos.inspector.schemas import DriverPackage, DriverRecord

MAX_INF_BYTES = 8 * 1024 * 1024
_HWID_RE = re.compile(
    r"\b(?:USB|HID|FTDIBUS|USBPRINT)\\VID_[0-9A-F]{4}[&+]PID_[0-9A-F]{4}[^\s,;\"]*", re.IGNORECASE
)
_VIDPID_RE = re.compile(r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})", re.IGNORECASE)
_STRTOKEN = re.compile(r"%([^%]+)%")


class SignedDriversCollector(Collector):
    name = "drivers"
    description = "Drivers bound to devices (Win32_PnPSignedDriver)"

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        rejected: list[str] = []
        for row in ctx.query("signed_drivers"):
            dev_id = row.get("DeviceID")
            if not dev_id:
                continue
            fields = {k: ps_value(row.get(k)) for k in (
                "DeviceName", "DriverProviderName", "DriverVersion", "DriverDate", "InfName",
                "Signer", "DeviceClass", "HardWareID")}
            try:
                record = DriverRecord(
                    device_id=dev_id,
                    device_name=fields["DeviceName"],
                    provider=fields["DriverProviderName"],
                    version=fields["DriverVersion"],
                    date=fields["DriverDate"],
                    inf_name=fields["InfName"],
                    is_signed=row.get("IsSigned") if isinstance(row.get("IsSigned"), bool) else None,
                    signer=fields["Signer"],
                    device_class=fields["DeviceClass"],
                    hardware_id=fields["HardWareID"],
                )
            except ValidationError as exc:
                # One malformed row must not discard every other driver on the machine.
                rejected.append(f"{dev_id}: {exc.errors()[0]['loc'][0]}")
                continue
            record.evidence_ids.append(ctx.add_evidence(self, "drv", dev_id, "powershell:signed_drivers", row))
            results.drivers.append(record)
        if rejected:
            return f"{len(rejected)} driver row(s) could not be parsed and were left out: {'; '.join(rejected[:5])}"
        return None


# --------------------------------------------------------------------------------------------
# INF parsing (pure, cross-platform)
# --------------------------------------------------------------------------------------------
def decode_inf(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    sample = raw[:400]
    if len(sample) > 40 and sample[1::2].count(0) > len(sample) // 4:
        return raw.decode("utf-16-le", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _strip_comment(line: str) -> str:
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ";" and not in_q:
            return line[:i]
    return line


def parse_inf(text: str) -> dict:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            current = line[1 : line.index("]")].strip().lower()
            sections.setdefault(current, {})
            continue
        if current is not None and "=" in line:
            k, v = line.split("=", 1)
            sections[current].setdefault(k.strip().lower(), v.strip())

    strings = {k.lower(): v.strip().strip('"') for k, v in sections.get("strings", {}).items()}

    def resolve(v: str | None) -> str | None:
        if v is None:
            return None
        v = _STRTOKEN.sub(lambda m: strings.get(m.group(1).lower(), m.group(0)), v)
        return v.strip().strip('"') or None

    ver = sections.get("version", {})
    hwids = sorted({h.upper() for h in _HWID_RE.findall(text)})
    usb_ids = sorted({f"{a.upper()}:{b.upper()}" for a, b in _VIDPID_RE.findall(text)})
    return {
        "provider": resolve(ver.get("provider")),
        "class": resolve(ver.get("class")),
        "class_guid": resolve(ver.get("classguid")),
        "driver_ver": resolve(ver.get("driverver")),
        "catalog_file": resolve(ver.get("catalogfile") or ver.get("catalogfile.nt")),
        "hardware_ids": hwids,
        "usb_ids": usb_ids,
        "strings_sample": dict(list(strings.items())[:40]),
    }


def oem_infs(volume_root: Path) -> list[Path]:
    win = child_ci(volume_root, "Windows")
    inf_dir = child_ci(win, "INF") if win else None
    if inf_dir is None:
        return []
    try:
        return sorted(
            p for p in inf_dir.iterdir()
            if p.is_file() and p.name.lower().startswith("oem") and p.name.lower().endswith(".inf")
        )
    except OSError:
        return []


class DriverPackagesCollector(Collector):
    name = "driver_packages"
    description = "Third-party INF packages (oem*.inf) on live and recovered Windows volumes"
    requires_windows = False

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        volumes = windows_volume_roots(ctx)
        if not volumes:
            raise CollectorSkipped("no Windows volume available (live or via --artifact-root)")
        artifact_keys = {str(r).lower() for r in ctx.artifact_roots}
        for vol in volumes:
            source = "recovered" if str(vol).lower() in artifact_keys else "live"
            for inf in oem_infs(vol):
                ctx.op(f"read-file:{inf}")
                try:
                    raw = inf.read_bytes()[:MAX_INF_BYTES]
                except OSError:
                    continue
                text = decode_inf(raw)
                parsed = parse_inf(text)
                sha = hashlib.sha256(raw).hexdigest()
                data = {"path": str(inf), "sha256": sha, **parsed}
                eid = ctx.add_evidence(self, "inf", f"{vol}|{inf.name}", f"file:{inf}", data)
                results.packages.append(
                    DriverPackage(
                        inf_path=str(inf),
                        windows_root=str(vol),
                        source=source,
                        provider=parsed["provider"],
                        device_class=parsed["class"],
                        class_guid=parsed["class_guid"],
                        driver_ver=parsed["driver_ver"],
                        catalog_file=parsed["catalog_file"],
                        usb_ids=parsed["usb_ids"],
                        hardware_ids=parsed["hardware_ids"],
                        keyword_hits=keyword_hits(text),
                        sha256=sha,
                        evidence_ids=[eid],
                    )
                )
        return None
