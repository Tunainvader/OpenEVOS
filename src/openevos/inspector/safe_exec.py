"""The ONLY module in the Inspector permitted to start a subprocess.

Design:
* A fixed registry of named, read-only PowerShell queries. Callers pass a query *key*, never a
  command string. There is no API for running arbitrary commands.
* Every script is checked at import time: every cmdlet verb must be on a read-only allowlist and
  no known state-changing tool may appear. A violation raises at import, so a bad edit fails
  every test run before it can reach a user's machine.
* Numeric parameters are substituted only after int() validation and bounds checks. Path lists
  are passed as JSON on stdin, never interpolated into the script.
* PowerShell is launched by absolute path from %SystemRoot% with -NoProfile (no user profile
  scripts) and -EncodedCommand (no quoting/injection issues).

None of these queries need administrator rights.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


class PlatformUnsupported(RuntimeError):
    """Raised when a Windows-only query is requested on another OS."""


class QueryError(RuntimeError):
    pass


class UnsafeScriptError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# Static safety checks
# --------------------------------------------------------------------------------------------
ALLOWED_VERBS = frozenset(
    {"Get", "Select", "Where", "ForEach", "ConvertTo", "ConvertFrom", "Sort", "Group", "Measure"}
)

FORBIDDEN_TOOLS = re.compile(
    r"\b(pnputil|devcon|reg(\.exe)?\s+(add|delete|import|load|unload)|sc(\.exe)?\s|bcdedit|"
    r"chkdsk|diskpart|format(\.com)?\s|fsutil|wmic|netsh|icacls|takeown|cmd(\.exe)?\s*/c|"
    r"Invoke-Expression|iex\b|Start-Process|\.Invoke\(|\.Put\(|\.Delete\(|\.SetValue\()",
    re.IGNORECASE,
)

# PowerShell is case-insensitive: `remove-item` runs Remove-Item, so match cmdlets in any case.
_CMDLET_RE = re.compile(r"(?<![\w$-])([A-Za-z]+)-([A-Za-z][A-Za-z0-9]*)\b")
_ALLOWED_VERBS_CI = frozenset(v.lower() for v in ALLOWED_VERBS)
# Built-in aliases for state-changing cmdlets, when used in command position.
_DESTRUCTIVE_ALIAS_RE = re.compile(
    r"(?<![\w$.\-])(rm|del|erase|rd|rmdir|ri|ni|md|mkdir|mv|move|cp|copy|cpi|mi|rni|ren|ac|clc|"
    r"sp|rp|si|kill|spps|saps|start|icm|iwr|irm|curl|wget|sajb)(?=\s)",
    re.IGNORECASE,
)
_SQ_STRING_RE = re.compile(r"'[^']*'")


def validate_script(script: str) -> None:
    """Raise UnsafeScriptError unless `script` only uses read-only cmdlets and tools."""
    code = _SQ_STRING_RE.sub("''", script)  # ignore text inside single-quoted literals
    if '"' in code:
        raise UnsafeScriptError("double-quoted strings (expandable) are not allowed in queries")
    m = FORBIDDEN_TOOLS.search(code) or _DESTRUCTIVE_ALIAS_RE.search(code)
    if m:
        raise UnsafeScriptError(f"forbidden tool/method in query: {m.group(0)!r}")
    for verb, noun in _CMDLET_RE.findall(code):
        if verb.lower() not in _ALLOWED_VERBS_CI:
            raise UnsafeScriptError(f"cmdlet '{verb}-{noun}' is not on the read-only allowlist")


# --------------------------------------------------------------------------------------------
# Query registry
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReadOnlyQuery:
    key: str
    description: str
    script: str
    returns_list: bool = True
    int_params: tuple[tuple[str, int, int], ...] = ()  # (name, min, max)
    accepts_stdin_json: bool = False
    timeout_s: int = 180

    def render(self, **params: Any) -> str:
        script = self.script
        for name, lo, hi in self.int_params:
            if name not in params or params[name] is None:
                raise QueryError(f"query {self.key} requires integer parameter '{name}'")
            value = int(params[name])
            if not (lo <= value <= hi):
                raise QueryError(f"parameter {name}={value} outside [{lo}, {hi}]")
            script = script.replace(f"__{name.upper()}__", str(value))
        if re.search(r"__[A-Z]+__", script):
            raise QueryError(f"unrendered placeholder in query {self.key}")
        return script


_PNP_DEVICES = r"""
Get-PnpDevice -ErrorAction SilentlyContinue |
  Select-Object InstanceId, FriendlyName, Class, ClassGuid, Manufacturer, Status, Present,
    @{n='Problem';e={[string]$_.Problem}}, ConfigManagerErrorCode, Service, HardwareID, CompatibleID |
  ConvertTo-Json -Depth 3 -Compress
"""

_PNP_PROPERTIES = r"""
$keys = @('DEVPKEY_Device_Parent','DEVPKEY_Device_BusReportedDeviceDesc','DEVPKEY_Device_LocationInfo',
          'DEVPKEY_Device_LocationPaths','DEVPKEY_Device_DriverInfPath','DEVPKEY_Device_DriverProvider',
          'DEVPKEY_Device_DriverVersion','DEVPKEY_Device_DriverDate','DEVPKEY_Device_LastArrivalDate')
$classes = @('Ports','Image','Camera','USB','USBDevice','HIDClass','WPD','Media','libusbK devices','libusb-win32 devices')
$devs = @(Get-PnpDevice -ErrorAction SilentlyContinue |
  Where-Object { ($_.InstanceId -match '^(USB|HID|FTDIBUS|USBSTOR|USBPRINT)\\') -or ($classes -contains $_.Class) })
$rows = foreach ($k in $keys) {
  $devs | Get-PnpDeviceProperty -KeyName $k -ErrorAction SilentlyContinue |
    Where-Object { $_.Type -ne 'Empty' -and $null -ne $_.Data } |
    Select-Object InstanceId, KeyName,
      @{n='Data';e={ if ($_.Data -is [array]) { @($_.Data | ForEach-Object { [string]$_ }) }
                     elseif ($_.Data -is [datetime]) { $_.Data.ToString('o') }
                     else { [string]$_.Data } }}
}
@($rows) | ConvertTo-Json -Depth 3 -Compress
"""

_SIGNED_DRIVERS = r"""
Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
  Where-Object { $_.DeviceID } |
  Select-Object DeviceID, DeviceName, DriverProviderName, DriverVersion,
    @{n='DriverDate';e={ if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { $null } }},
    InfName, IsSigned, Signer, DeviceClass, HardWareID, Manufacturer |
  ConvertTo-Json -Depth 3 -Compress
"""

_SYSTEM_INFO = r"""
$cs   = Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object Name, Manufacturer, Model, SystemType, TotalPhysicalMemory
$os   = Get-CimInstance -ClassName Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture
$bios = Get-CimInstance -ClassName Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion,
          @{n='ReleaseDate';e={ if ($_.ReleaseDate) { $_.ReleaseDate.ToString('yyyy-MM-dd') } else { $null } }}
$bb   = Get-CimInstance -ClassName Win32_BaseBoard | Select-Object Manufacturer, Product
$cpu  = @(Get-CimInstance -ClassName Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, AddressWidth)
$gpu  = @(Get-CimInstance -ClassName Win32_VideoController | Select-Object Name, DriverVersion, AdapterCompatibility)
$disk = @(Get-CimInstance -ClassName Win32_DiskDrive | Select-Object Model, InterfaceType, MediaType, Size, Status)
$usb  = @(Get-CimInstance -ClassName Win32_USBController | Select-Object Name, Manufacturer, PNPDeviceID)
[pscustomobject]@{ ComputerSystem = $cs; OperatingSystem = $os; BIOS = $bios; BaseBoard = $bb;
                   Processors = $cpu; VideoControllers = $gpu; DiskDrives = $disk; USBControllers = $usb } |
  ConvertTo-Json -Depth 4 -Compress
"""

_EVENTS = r"""
$start = (Get-Date).AddHours(-__HOURS__)
$sys = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $start;
          ProviderName = @('Microsoft-Windows-Kernel-PnP','Microsoft-Windows-UserPnp','Microsoft-Windows-DriverFrameworks-UserMode') } -ErrorAction SilentlyContinue)
$cfg = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-Kernel-PnP/Configuration'; StartTime = $start } -ErrorAction SilentlyContinue)
@($sys + $cfg) | Where-Object { $_ } | Select-Object -First 2000 `
    @{n='Time';e={ $_.TimeCreated.ToString('o') }}, LogName, ProviderName, Id, LevelDisplayName, Message |
  ConvertTo-Json -Depth 3 -Compress
"""

_AUTHENTICODE = r"""
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
@($paths) | ForEach-Object {
  $p = $_
  $s = Get-AuthenticodeSignature -LiteralPath $p -ErrorAction SilentlyContinue
  if ($s) {
    [pscustomobject]@{ Path = $p; Status = [string]$s.Status;
      Signer = $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { $null });
      Thumbprint = $(if ($s.SignerCertificate) { $s.SignerCertificate.Thumbprint } else { $null }) }
  }
} | ConvertTo-Json -Depth 3 -Compress
"""

QUERIES: dict[str, ReadOnlyQuery] = {
    q.key: q
    for q in [
        ReadOnlyQuery("pnp_devices", "All PnP devices incl. non-present (Get-PnpDevice)", _PNP_DEVICES),
        ReadOnlyQuery(
            "pnp_device_properties",
            "Parent, bus-reported description, location and driver properties for USB/HID/serial/imaging devices",
            _PNP_PROPERTIES,
            timeout_s=300,
        ),
        ReadOnlyQuery("signed_drivers", "Drivers bound to devices (Win32_PnPSignedDriver)", _SIGNED_DRIVERS),
        ReadOnlyQuery("system_info", "OS, board, BIOS, CPU, GPU, disks, USB controllers (CIM)", _SYSTEM_INFO, returns_list=False),
        ReadOnlyQuery(
            "pnp_events",
            "PnP / driver-framework events from the System and Kernel-PnP/Configuration logs",
            _EVENTS,
            int_params=(("hours", 1, 24 * 90),),
        ),
        ReadOnlyQuery(
            "authenticode",
            "Authenticode signature status for selected files (paths via stdin)",
            _AUTHENTICODE,
            accepts_stdin_json=True,
        ),
    ]
}

for _q in QUERIES.values():  # fail fast at import if anyone edits a query unsafely
    validate_script(_q.script)


# --------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------
_PREAMBLE = "$ProgressPreference = 'SilentlyContinue'\n[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"


def is_windows() -> bool:
    return sys.platform == "win32"


def _powershell_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def run(key: str, *, stdin_obj: Any = None, **params: Any) -> Any:
    """Run an allowlisted read-only query and return parsed JSON."""
    if key not in QUERIES:
        raise QueryError(f"unknown query '{key}'")
    q = QUERIES[key]
    if not is_windows():
        raise PlatformUnsupported(f"query '{key}' requires Windows")
    if stdin_obj is not None and not q.accepts_stdin_json:
        raise QueryError(f"query '{key}' does not accept stdin data")

    script = _PREAMBLE + q.render(**params)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [_powershell_path(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(stdin_obj) if stdin_obj is not None else None,
            stdin=None if stdin_obj is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=q.timeout_s,
            creationflags=creationflags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise QueryError(f"query '{key}' timed out after {q.timeout_s}s") from exc
    if proc.returncode != 0:
        raise QueryError(f"query '{key}' failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}")
    return parse_output(proc.stdout, returns_list=q.returns_list)


def parse_output(stdout: str, *, returns_list: bool) -> Any:
    text = stdout.lstrip("\ufeff").strip()
    if not text:
        return [] if returns_list else {}
    data = json.loads(text)
    if returns_list:
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
    return data
