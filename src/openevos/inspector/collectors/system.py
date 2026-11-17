"""Host PC profile: OS, board, BIOS, CPU, GPU, disks, USB controllers, .NET runtime."""

from __future__ import annotations

from openevos.inspector.collectors import (
    Collector,
    CollectorContext,
    ScanResults,
    as_list,
    first,
    ps_value,
)
from openevos.inspector.schemas import SystemInfo

NET_FX_KEY = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"


class SystemCollector(Collector):
    name = "system"
    description = "OS, hardware and BIOS profile via CIM"

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        raw = ctx.query("system_info")
        dotnet = None
        reader = ctx.registry_reader
        if reader is not None:
            ctx.op(f"registry-read:HKLM64\\{NET_FX_KEY}\\Release")
            rel = reader.values("HKLM64", NET_FX_KEY).get("Release")
            dotnet = int(rel) if isinstance(rel, int) or (isinstance(rel, str) and rel.isdigit()) else None
        data = {"cim": raw, "dotnet_release": dotnet}
        eid = ctx.add_evidence(self, "sys", "system", "powershell:system_info", data)

        cs, os_, bios, bb = (first(raw.get(k)) for k in ("ComputerSystem", "OperatingSystem", "BIOS", "BaseBoard"))
        mem = cs.get("TotalPhysicalMemory")
        results.system = SystemInfo(
            computer_name=cs.get("Name"),
            os_caption=os_.get("Caption"),
            os_version=os_.get("Version"),
            os_build=str(os_.get("BuildNumber")) if os_.get("BuildNumber") is not None else None,
            os_architecture=os_.get("OSArchitecture"),
            manufacturer=cs.get("Manufacturer"),
            model=cs.get("Model"),
            system_type=cs.get("SystemType"),
            total_memory_bytes=int(mem) if mem is not None else None,
            bios_vendor=bios.get("Manufacturer"),
            bios_version=bios.get("SMBIOSBIOSVersion"),
            bios_date=ps_value(bios.get("ReleaseDate")),
            baseboard_manufacturer=bb.get("Manufacturer"),
            baseboard_product=bb.get("Product"),
            cpus=as_list(raw.get("Processors")),
            gpus=as_list(raw.get("VideoControllers")),
            disks=as_list(raw.get("DiskDrives")),
            usb_controllers=as_list(raw.get("USBControllers")),
            dotnet_framework_release=dotnet,
            evidence_ids=[eid],
        )
        return None
