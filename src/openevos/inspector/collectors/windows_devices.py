"""PnP device inventory (present and non-present) with topology and driver properties.

Non-present ("phantom") devices matter: Windows remembers devices that were attached before.
On a PC that once ran the EVOS, these records can reveal VID/PIDs without the microscope.
"""

from __future__ import annotations

from collections import defaultdict

from openevos.inspector.collectors import Collector, CollectorContext, ScanResults, as_list
from openevos.inspector.collectors.usb_devices import parse_usb_identity
from openevos.inspector.safe_exec import QueryError
from openevos.inspector.schemas import PnpDevice

PROP_MAP = {
    "DEVPKEY_Device_Parent": "parent_instance_id",
    "DEVPKEY_Device_BusReportedDeviceDesc": "bus_reported_description",
    "DEVPKEY_Device_LocationInfo": "location_info",
    "DEVPKEY_Device_LocationPaths": "location_paths",
    "DEVPKEY_Device_DriverInfPath": "driver_inf",
    "DEVPKEY_Device_DriverProvider": "driver_provider",
    "DEVPKEY_Device_DriverVersion": "driver_version",
    "DEVPKEY_Device_DriverDate": "driver_date",
    "DEVPKEY_Device_LastArrivalDate": "last_arrival",
}


def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


class WindowsDevicesCollector(Collector):
    name = "windows_devices"
    description = "PnP devices, USB topology and driver properties (Get-PnpDevice)"

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        rows = ctx.query("pnp_devices")
        warning = None
        try:
            prop_rows = ctx.query("pnp_device_properties")
        except QueryError as exc:
            prop_rows, warning = [], f"device properties unavailable: {exc}"

        props: dict[str, dict] = defaultdict(dict)
        for p in prop_rows:
            iid, key = p.get("InstanceId"), p.get("KeyName")
            if iid and key in PROP_MAP:
                props[iid.upper()][key] = p.get("Data")

        for row in rows:
            iid = _str(row.get("InstanceId"))
            if not iid:
                continue
            prop = props.get(iid.upper(), {})
            eid = ctx.add_evidence(
                self, "pnp", iid, "powershell:pnp_devices+pnp_device_properties",
                {"pnp": row, "properties": prop},
            )
            hw = [str(h) for h in as_list(row.get("HardwareID"))]
            mapped = {PROP_MAP[k]: v for k, v in prop.items()}
            loc_paths = mapped.pop("location_paths", None)
            results.devices.append(
                PnpDevice(
                    instance_id=iid,
                    friendly_name=_str(row.get("FriendlyName")),
                    device_class=_str(row.get("Class")),
                    class_guid=_str(row.get("ClassGuid")),
                    manufacturer=_str(row.get("Manufacturer")),
                    status=_str(row.get("Status")),
                    present=row.get("Present") if isinstance(row.get("Present"), bool) else None,
                    problem=_str(row.get("Problem")),
                    problem_code=_int(row.get("ConfigManagerErrorCode")),
                    service=_str(row.get("Service")),
                    hardware_ids=hw,
                    compatible_ids=[str(c) for c in as_list(row.get("CompatibleID"))],
                    location_paths=[str(x) for x in as_list(loc_paths)],
                    usb=parse_usb_identity(iid, hw),
                    evidence_ids=[eid],
                    **{k: _str(v) for k, v in mapped.items()},
                )
            )
        return warning
