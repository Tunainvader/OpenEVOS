"""Typed schemas for the Inspector's exported files.

Exports (plan §18):
  system_inventory.json    -> SystemInventory
  device_inventory.json    -> DeviceInventory   (PnP devices incl. USB, serial ports)
  driver_inventory.json    -> DriverInventory   (bound drivers + third-party INF packages)
  software_inventory.json  -> SoftwareInventory (programs, EVOS artifacts, SDK matches, MM)
  evidence_register.json   -> EvidenceRegister  (raw evidence + findings; provenance)
  scan_manifest.json       -> ScanManifest      (collector runs, skipped operations)
  openevos_diagnostic_report.md

Normalised records carry `evidence_ids` pointing into evidence_register.json, so every value in
a report can be traced back to the raw OS/file record it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from openevos.inspector.evidence import EvidenceLabel, EvidenceStage

SCHEMA_VERSION = "0.1.0"

SnapshotLabel = str  # free text, conventionally: baseline | microscope | amc1000 | recovered-image


class ScanMeta(BaseModel):
    scan_id: str
    label: SnapshotLabel
    tool_version: str
    schema_version: str = SCHEMA_VERSION
    started_at: datetime
    finished_at: datetime | None = None
    host_platform: str
    python_version: str
    elevated: bool | None = None
    redacted: bool = False
    artifact_roots: list[str] = Field(default_factory=list)


class CollectorRun(BaseModel):
    name: str
    status: Literal["ok", "partial", "skipped", "failed"]
    reason: str = ""
    started_at: datetime
    duration_s: float
    evidence_count: int = 0
    operations: list[str] = Field(default_factory=list)  # read-only operations performed


class ScanManifest(BaseModel):
    meta: ScanMeta
    collectors: list[CollectorRun] = Field(default_factory=list)
    actions_not_performed: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------- system
class SystemInfo(BaseModel):
    computer_name: str | None = None
    os_caption: str | None = None
    os_version: str | None = None
    os_build: str | None = None
    os_architecture: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    system_type: str | None = None
    total_memory_bytes: int | None = None
    bios_vendor: str | None = None
    bios_version: str | None = None
    bios_date: str | None = None
    baseboard_manufacturer: str | None = None
    baseboard_product: str | None = None
    cpus: list[dict] = Field(default_factory=list)
    gpus: list[dict] = Field(default_factory=list)
    disks: list[dict] = Field(default_factory=list)
    usb_controllers: list[dict] = Field(default_factory=list)
    dotnet_framework_release: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EventRecord(BaseModel):
    time: str | None = None
    log: str
    provider: str | None = None
    event_id: int | None = None
    level: str | None = None
    message: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class SystemInventory(BaseModel):
    meta: ScanMeta
    system: SystemInfo | None = None
    events: list[EventRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------- devices
class UsbIdentity(BaseModel):
    """Parsed from Windows hardware/instance IDs. A VID/PID is a USB code, not a product model."""

    vid: str | None = None
    pid: str | None = None
    rev: str | None = None
    mi: str | None = None  # interface number for composite devices
    is_interface: bool = False
    serial_in_instance: str | None = None  # iSerialNumber, if Windows used it in the instance ID

    @property
    def vid_pid(self) -> str | None:
        return f"{self.vid}:{self.pid}" if self.vid and self.pid else None


class PnpDevice(BaseModel):
    instance_id: str
    friendly_name: str | None = None
    device_class: str | None = None
    class_guid: str | None = None
    manufacturer: str | None = None
    status: str | None = None  # OK / Error / Degraded / Unknown (as Windows reports it)
    present: bool | None = None  # False => Windows remembers it but it is not attached now
    problem: str | None = None
    problem_code: int | None = None  # ConfigManagerErrorCode
    service: str | None = None
    hardware_ids: list[str] = Field(default_factory=list)
    compatible_ids: list[str] = Field(default_factory=list)
    bus_reported_description: str | None = None  # USB iProduct string as reported by the device
    parent_instance_id: str | None = None
    location_info: str | None = None
    location_paths: list[str] = Field(default_factory=list)
    driver_inf: str | None = None
    driver_provider: str | None = None
    driver_version: str | None = None
    driver_date: str | None = None
    last_arrival: str | None = None
    usb: UsbIdentity | None = None
    stage: EvidenceStage = EvidenceStage.DETECTED
    evidence_ids: list[str] = Field(default_factory=list)


class SerialPort(BaseModel):
    device: str  # e.g. COM3 — listed only, never opened
    description: str | None = None
    hwid: str | None = None
    vid: str | None = None
    pid: str | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class DeviceInventory(BaseModel):
    meta: ScanMeta
    devices: list[PnpDevice] = Field(default_factory=list)
    serial_ports: list[SerialPort] = Field(default_factory=list)

    def by_instance_id(self) -> dict[str, PnpDevice]:
        return {d.instance_id.upper(): d for d in self.devices}

    def usb_devices(self) -> list[PnpDevice]:
        return [d for d in self.devices if d.usb is not None]


# ---------------------------------------------------------------------------- drivers
class DriverRecord(BaseModel):
    """A driver bound to a device (Win32_PnPSignedDriver)."""

    device_id: str
    device_name: str | None = None
    provider: str | None = None
    version: str | None = None
    date: str | None = None
    inf_name: str | None = None
    is_signed: bool | None = None
    signer: str | None = None
    device_class: str | None = None
    hardware_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class DriverPackage(BaseModel):
    """A third-party INF package (oem*.inf) parsed read-only from a live or recovered Windows."""

    inf_path: str
    windows_root: str
    source: Literal["live", "recovered"] = "live"
    provider: str | None = None
    device_class: str | None = None
    class_guid: str | None = None
    driver_ver: str | None = None
    catalog_file: str | None = None
    usb_ids: list[str] = Field(default_factory=list)  # "VVVV:PPPP"
    hardware_ids: list[str] = Field(default_factory=list)
    keyword_hits: list[str] = Field(default_factory=list)
    sha256: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class DriverInventory(BaseModel):
    meta: ScanMeta
    drivers: list[DriverRecord] = Field(default_factory=list)
    packages: list[DriverPackage] = Field(default_factory=list)


# ---------------------------------------------------------------------------- software
class InstalledProgram(BaseModel):
    name: str
    version: str | None = None
    publisher: str | None = None
    install_date: str | None = None
    install_location: str | None = None
    registry_view: str | None = None  # HKLM64 / HKLM32 / HKCU
    keyword_hits: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class PeInfo(BaseModel):
    machine: Literal["x86", "x64", "arm64", "arm", "ia64", "unknown"]
    pe_format: Literal["PE32", "PE32+", "unknown"]
    is_managed: bool = False  # .NET assembly
    clr_flags: dict[str, bool] | None = None
    effective_bitness: Literal["32", "64", "any", "unknown"] = "unknown"


class FileArtifact(BaseModel):
    path: str
    root: str
    size: int
    modified: str | None = None
    sha256: str | None = None
    skipped_hash_reason: str | None = None
    keyword_hits: list[str] = Field(default_factory=list)
    pe: PeInfo | None = None
    version_info: dict[str, str] = Field(default_factory=dict)
    signature: dict[str, str | None] | None = None  # Authenticode status (live Windows only)
    evidence_ids: list[str] = Field(default_factory=list)


class SdkMatch(BaseModel):
    signature_id: str
    category: Literal["camera_sdk", "usb_bridge", "runtime", "other"]
    vendor: str
    sdk: str
    path: str
    label: EvidenceLabel  # PROBABLE (filename) or STRONGLY_SUPPORTED (filename + version info)
    basis: list[str] = Field(default_factory=list)
    pe: PeInfo | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class MicroManagerInstall(BaseModel):
    path: str
    version_hint: str | None = None
    adapters: list[str] = Field(default_factory=list)  # mmgr_dal_*.dll names
    evidence_ids: list[str] = Field(default_factory=list)


class RegistryVendorKey(BaseModel):
    path: str  # key name only — values are not read
    keyword_hits: list[str]
    evidence_ids: list[str] = Field(default_factory=list)


class SoftwareInventory(BaseModel):
    meta: ScanMeta
    programs: list[InstalledProgram] = Field(default_factory=list)
    vendor_registry_keys: list[RegistryVendorKey] = Field(default_factory=list)
    artifacts: list[FileArtifact] = Field(default_factory=list)
    sdk_matches: list[SdkMatch] = Field(default_factory=list)
    micromanager: list[MicroManagerInstall] = Field(default_factory=list)
