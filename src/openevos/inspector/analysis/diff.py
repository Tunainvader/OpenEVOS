"""Differential hardware discovery (plan §4.3 steps 1–4, §18 steps 4–6).

    openevos-inspector diff <baseline_scan_dir> <connected_scan_dir> [--recovered <image_scan_dir>]

Devices present in the connected snapshot but not in the baseline are candidates for the
microscope (or AMC1000). Physical devices and their interfaces are grouped. If a scan of the
recovered EVOS disk image is supplied, each new VID:PID is cross-referenced against the
third-party driver packages found on that image.

Tip: plug the instrument into the *same* USB port for every snapshot. Windows derives instance
IDs from the port for devices without a serial number; changing ports looks like remove+add.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from openevos.inspector.analysis.rules import candidate_for
from openevos.inspector.collectors.usb_devices import physical_parent
from openevos.inspector.schemas import (
    DeviceInventory,
    DriverInventory,
    DriverPackage,
    DriverRecord,
    PnpDevice,
    ScanMeta,
    SerialPort,
)

_COMPARE_FIELDS = ("status", "problem_code", "service", "driver_inf", "driver_version", "driver_provider")


class DeviceChange(BaseModel):
    instance_id: str
    field: str
    before: str | int | None
    after: str | int | None


class AddedDevice(BaseModel):
    device: PnpDevice
    physical_device_id: str
    candidate_role: str | None = None  # from vendor-software analysis; a hypothesis, not a mapping
    candidate_label: str | None = None
    previously_known: bool = False  # phantom in baseline -> Windows had seen it before
    serial_ports: list[str] = Field(default_factory=list)
    driver: DriverRecord | None = None
    recovered_inf_matches: list[DriverPackage] = Field(default_factory=list)


class PhysicalGroup(BaseModel):
    physical_device_id: str
    vid_pid: str | None
    interface_ids: list[str]


class DiffResult(BaseModel):
    baseline: ScanMeta
    other: ScanMeta
    recovered: ScanMeta | None = None
    added: list[AddedDevice] = Field(default_factory=list)
    removed: list[PnpDevice] = Field(default_factory=list)
    changed: list[DeviceChange] = Field(default_factory=list)
    physical_groups: list[PhysicalGroup] = Field(default_factory=list)
    added_serial_ports: list[SerialPort] = Field(default_factory=list)
    removed_serial_ports: list[SerialPort] = Field(default_factory=list)
    port_change_suspects: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def load_devices(scan_dir: Path) -> DeviceInventory:
    return DeviceInventory.model_validate_json((scan_dir / "device_inventory.json").read_text("utf-8"))


def load_drivers(scan_dir: Path) -> DriverInventory | None:
    p = scan_dir / "driver_inventory.json"
    return DriverInventory.model_validate_json(p.read_text("utf-8")) if p.exists() else None


def _present(devs: list[PnpDevice]) -> dict[str, PnpDevice]:
    return {d.instance_id.upper(): d for d in devs if d.present is not False}


def diff_scans(baseline_dir: Path, other_dir: Path, recovered_dir: Path | None = None) -> DiffResult:
    base, other = load_devices(baseline_dir), load_devices(other_dir)
    other_drv = load_drivers(other_dir)
    rec_drv = load_drivers(recovered_dir) if recovered_dir else None

    res = DiffResult(baseline=base.meta, other=other.meta, recovered=rec_drv.meta if rec_drv else None)
    if base.meta.redacted != other.meta.redacted:
        res.notes.append("One scan is redacted and the other is not; serial-based IDs will not line up.")

    b_present, o_present = _present(base.devices), _present(other.devices)
    b_all = base.by_instance_id()
    o_all = other.by_instance_id()
    drivers_by_dev = {d.device_id.upper(): d for d in (other_drv.drivers if other_drv else [])}

    ports_by_hwid: dict[str, list[str]] = defaultdict(list)
    for p in other.serial_ports:
        if p.vid and p.pid:
            ports_by_hwid[f"{p.vid}:{p.pid}"].append(p.device)

    added_keys = sorted(set(o_present) - set(b_present))
    for k in added_keys:
        d = o_present[k]
        phys = physical_parent(d, o_all)
        matches = []
        if rec_drv and d.usb and d.usb.vid_pid:
            matches = [pkg for pkg in rec_drv.packages if d.usb.vid_pid in pkg.usb_ids]
        cand = candidate_for(d.usb.vid, d.usb.pid) if d.usb else None
        res.added.append(
            AddedDevice(
                device=d,
                physical_device_id=phys.instance_id,
                candidate_role=cand["role"] if cand else None,
                candidate_label=cand["label"] if cand else None,
                previously_known=k in b_all,
                serial_ports=ports_by_hwid.get(d.usb.vid_pid, []) if d.usb and d.usb.vid_pid else [],
                driver=drivers_by_dev.get(k),
                recovered_inf_matches=matches,
            )
        )

    res.removed = [b_present[k] for k in sorted(set(b_present) - set(o_present))]

    for k in sorted(set(b_present) & set(o_present)):
        b, o = b_present[k], o_present[k]
        for f in _COMPARE_FIELDS:
            bv, ov = getattr(b, f), getattr(o, f)
            if bv != ov:
                res.changed.append(DeviceChange(instance_id=o.instance_id, field=f, before=bv, after=ov))

    groups: dict[str, list[str]] = defaultdict(list)
    for a in res.added:
        groups[a.physical_device_id].append(a.device.instance_id)
    for pid, members in sorted(groups.items()):
        root = o_all.get(pid.upper())
        res.physical_groups.append(
            PhysicalGroup(
                physical_device_id=pid,
                vid_pid=root.usb.vid_pid if root and root.usb else None,
                interface_ids=sorted(m for m in members if m.upper() != pid.upper()),
            )
        )

    b_ports = {p.device: p for p in base.serial_ports}
    o_ports = {p.device: p for p in other.serial_ports}
    res.added_serial_ports = [o_ports[k] for k in sorted(set(o_ports) - set(b_ports))]
    res.removed_serial_ports = [b_ports[k] for k in sorted(set(b_ports) - set(o_ports))]

    removed_vp = {d.usb.vid_pid for d in res.removed if d.usb and d.usb.vid_pid}
    added_vp = {a.device.usb.vid_pid for a in res.added if a.device.usb and a.device.usb.vid_pid}
    res.port_change_suspects = sorted(removed_vp & added_vp)
    if res.port_change_suspects:
        res.notes.append(
            "Same VID:PID both removed and added — likely the same hardware on a different USB port: "
            + ", ".join(res.port_change_suspects)
        )
    if not res.added:
        res.notes.append("No new devices. Was the instrument powered and connected for the second snapshot?")
    return res
