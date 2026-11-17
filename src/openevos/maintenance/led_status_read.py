"""LED Gate 3 (RE-4): read-only status of the LED application — version, cubes, epi channels (ADR-0031, L3).

Spec: docs/evidence/fl1_v1.6_led_app_command_table.md §4 (decoded from `EvosGenIII_LEDBoard64.dll`).

Four vendor *get* queries, each a bare command with no payload, each sent exactly once:

| Request | Reply | Vendor methods that send exactly this request |
|---|---|---|
| `0x40` | `0xC0` | `GetFirmwareVersion` (ILEDBoard), `GetPersistentData2AddressRange` |
| `0x47` | `0xC7` | `get_FilterCube`, `get_ObjectiveLens`, `get_FilterCubePositions`, `GetFilterCubeTypeByPosition`, … |
| `0x5A` | `0xDA` | `GetEpiMode`, `GetNumEpiFixedOutputChannels`, `GetEpiFixedOutputChannelState` |
| `0x4A` | `0xCA` | `get_EpiEnable`, `get_EpiDigipot`, `GetEpiDigipotRange`, `get_EpiCurrentMode`, `get_PWMPeriodEpi`, … |

What it does, and nothing else:
* Target: exactly one present `0DAD:0051`, no `0DAD:0057`/`0052`; driver reports `0DAD:0051` with
  65/65-byte reports; a StartApp record (`app_appeared`) lists this interface.
* Dry run by default. Sending needs `--send` and the exact confirmation phrase.
* For each command in the fixed order above: `HidD_FlushQueue`, ONE `WriteFile` (500 ms), then at most
  5 input-report reads within 1 s, accepting the first with the expected reply code and a valid
  frame checksum. Never resent. BUSY (`0xF0`), a failed write or no reply STOPS the whole run:
  later commands are not sent.
* Only `0x40`, `0x47`, `0x5A`, `0x4A` can be built. Every other byte is refused before any I/O.
  The board has no LED temperature or measured-current query (spec §3): none is attempted.
* Decoded values are CANDIDATE: field meanings come from the host DLL, not the board firmware.
"""

from __future__ import annotations

import argparse
import json
import platform
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openevos.maintenance.led_bootloader import (
    PID_BOOTLOADER,
    REPORT_LEN,
    VID,
    DeviceDescription,
    HidBackend,
    WindowsHid,
)
from openevos.maintenance.led_checksum import find_app_start_record, frame_checksum

PID_APPLICATION = 0x0051
PID_APPLICATION_ALT = 0x0052
CMD_VERSION = 0x40
CMD_POSITIONS = 0x47
CMD_EPI_CHANNELS = 0x5A
CMD_EPI_STATUS = 0x4A
SEQUENCE = (CMD_VERSION, CMD_POSITIONS, CMD_EPI_CHANNELS, CMD_EPI_STATUS)
ALLOWED_COMMANDS = frozenset(SEQUENCE)
REPLY_BUSY = 0xF0
IO_TIMEOUT_S = 0.5
READ_WINDOW_S = 1.0
MAX_READS = 5
CONFIRM_PHRASE = "READ LED STATUS 40 47 5A 4A ON 0DAD:0051"
SPEC = "docs/evidence/fl1_v1.6_led_app_command_table.md §4; ADR-0031"
EVIDENCE_PREFIX = "led-application-gate3-status"


class StatusReadRefused(RuntimeError):
    """Not sent: a precondition failed. Nothing reached the device."""


def build_request(command: int) -> bytes:
    """`00 <cmd>`, 62 zero bytes, two's-complement frame checksum. Only the four reviewed gets."""
    if command not in ALLOWED_COMMANDS:
        raise StatusReadRefused(f"command 0x{command:02X} is not in the reviewed status set; nothing sent")
    body = bytes([0x00, command]) + bytes(REPORT_LEN - 3)
    return body + bytes([frame_checksum(body + b"\x00")])


REQUESTS = {cmd: build_request(cmd) for cmd in SEQUENCE}


# Enum names from the DLL's embedded type library (spec §4b). Unlisted values decode as None.
CUBE_TYPES = {0: "SHORT", 1: "NONE", 2: "DISENGAGED", 3: "DAPI", 4: "GFP", 5: "RFP", 6: "CFP", 7: "YFP",
              8: "TEXAS_RED", 9: "CY5", 20: "MCHERRY", 22: "FITC", 23: "BFP", 50: "WHITE"}
OBJECTIVE_TYPES = {0: "NONE", 1: "2X", 2: "4X", 3: "10X", 4: "20X", 5: "40X"}
PHASE_RING = {0: "DISENGAGED", 1: "1", 2: "2", 3: "3", 4: "4"}
EPI_MODES = {1: "MOVEABLE_CARRIAGE", 2: "FIXED_OUTPUT_CHANNELS", 3: "PRISM"}
EPI_CURRENT_MODES = {0xFFFE: "LOW", 0xFFFF: "HIGH", 0xFFFD: "DIGIPOT"}
DIGIPOT_STATUS = {0: "OK", 1: "NOT_RESPONDING", 2: "NOT_IMPLEMENTED", 3: "MODE_MISMATCH"}


def _u16be(r: bytes, i: int) -> int:
    return (r[i] << 8) | r[i + 1]


EPI_MODE_FIXED_OUTPUT = 2  # the firmware fills the channel bytes only in this mode (spec §4c)


def decode_fields(command: int, r: bytes) -> dict:
    """Field layout per spec §4/§4c. Only bytes the firmware handler writes are decoded: the board does
    not clear its reply buffer, so everything else is residue of earlier replies. All CANDIDATE."""
    if command == CMD_VERSION:
        major, minor, build = struct.unpack(">III", r[2:14])
        return {"app_version": f"{major}.{minor}.{build}", "major": major, "minor": minor, "build": build,
                "bytes_14_63_hex": r[14:64].hex()}
    if command == CMD_POSITIONS:  # handler writes bytes 2..34, and 35..39 only in epi mode 2
        cubes = [r[25], r[26], r[27], r[28]]
        fixed = r[34] == EPI_MODE_FIXED_OUTPUT
        return {"filter_cube_position": r[2], "filter_cube_in_path_name": CUBE_TYPES.get(r[2]),
                "objective_position": r[3], "objective_name": OBJECTIVE_TYPES.get(r[3]),
                "filter_cube_positions": r[4], "cube_type_by_position": cubes,
                "cube_name_by_position": [CUBE_TYPES.get(c) for c in cubes],
                "objective_sensor_raw": r[29], "cube_sensor_raw": r[30], "filter_cube_position_alt": r[31],
                "phase_ring_position": r[32], "phase_ring_name": PHASE_RING.get(r[32]), "phase_sensor_raw": r[33],
                "epi_mode": r[34], "epi_channels": list(r[35:40]) if fixed else None,
                "written_bytes": [2, 39 if fixed else 34], "raw_hex": r[2:64].hex()}
    if command == CMD_EPI_CHANNELS:  # handler writes byte 2, and 3..7 only in epi mode 2
        fixed = r[2] == EPI_MODE_FIXED_OUTPUT
        return {"epi_mode": r[2], "epi_mode_name": EPI_MODES.get(r[2]),
                "epi_fixed_output_channels": r[3] if fixed else None,
                "epi_channel_state": [r[4], r[5], r[6], r[7]] if fixed else None,
                "written_bytes": [2, 7 if fixed else 2], "raw_hex": r[2:64].hex()}
    if command == CMD_EPI_STATUS:  # handler writes bytes 2..24
        return {"epi_enable": r[2], "written_bytes": [2, 24],
                "epi_digipot_status": r[3], "epi_digipot_status_name": DIGIPOT_STATUS.get(r[3]),
                "epi_digipot": r[4], "epi_digipot_range": [r[5], r[6]],
                "epi_current_mode": _u16be(r, 7), "epi_current_mode_name": EPI_CURRENT_MODES.get(_u16be(r, 7)),
                "pwm_width_epi": _u16be(r, 9), "pwm_period_epi": _u16be(r, 11),
                "epi_digipot_hardware_value": r[21], "epi_disabled_due_to_disengaged": r[24],
                "raw_hex": r[2:64].hex()}
    raise StatusReadRefused(f"no decoder for 0x{command:02X}")


@dataclass(frozen=True)
class StatusReply:
    command: int
    valid: bool
    busy: bool
    problem: str
    reply_code: int | None
    fields: dict = field(default_factory=dict)


def parse_reply(command: int, reply: bytes) -> StatusReply:
    expected = command | 0x80
    if len(reply) != REPORT_LEN:
        return StatusReply(command, False, False, f"reply is {len(reply)} bytes, expected {REPORT_LEN}", None)
    code = reply[1]
    if reply[64] != frame_checksum(reply):
        return StatusReply(command, False, False,
                           f"frame checksum 0x{reply[64]:02X} != 0x{frame_checksum(reply):02X}", code)
    if code == REPLY_BUSY:
        return StatusReply(command, False, True, "application answered BUSY (0xF0)", code)
    if code != expected:
        return StatusReply(command, False, False, f"reply code 0x{code:02X}, expected 0x{expected:02X}", code)
    return StatusReply(command, True, False, "", code, decode_fields(command, reply))


@dataclass
class Exchange:
    command: int
    request_hex: str
    bytes_written: int | None = None
    reply_hex: str | None = None
    ignored_reports_hex: list[str] = field(default_factory=list)
    decoded: StatusReply | None = None
    problem: str = ""
    elapsed_ms: float | None = None


@dataclass
class StatusResult:
    outcome: str  # "dry_run" | "refused" | "stopped" | "complete"
    message: str
    started_utc: str
    host: str
    device: DeviceDescription | None = None
    app_start_record: str | None = None
    planned_hex: list[str] = field(default_factory=lambda: [REQUESTS[c].hex() for c in SEQUENCE])
    sent: bool = False
    exchanges: list[Exchange] = field(default_factory=list)
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _one(ch, command: int, monotonic: Callable[[], float]) -> Exchange:
    ex = Exchange(command, REQUESTS[command].hex())
    t0 = monotonic()
    ch.flush_input()
    try:
        ex.bytes_written = ch.write(REQUESTS[command], IO_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - report, never resend
        ex.problem = f"write did not complete: {exc}"
        ex.elapsed_ms = (monotonic() - t0) * 1000
        return ex
    if ex.bytes_written != REPORT_LEN:
        ex.problem = f"wrote {ex.bytes_written} of {REPORT_LEN} bytes; no read attempted"
        ex.elapsed_ms = (monotonic() - t0) * 1000
        return ex
    for _ in range(MAX_READS):  # passive: reading input reports sends nothing
        left = READ_WINDOW_S - (monotonic() - t0)
        if left <= 0:
            break
        try:
            report = ch.read(REPORT_LEN, min(IO_TIMEOUT_S, left))
        except Exception as exc:  # noqa: BLE001
            ex.problem = f"no matching reply: {exc}"
            break
        d = parse_reply(command, report)
        if d.valid or d.busy:
            ex.reply_hex, ex.decoded = report.hex(), d
            break
        ex.ignored_reports_hex.append(report.hex())
    if ex.decoded is None and not ex.problem:
        ex.problem = "no matching reply"
    elif ex.decoded is not None and ex.decoded.busy:
        ex.problem = "application answered BUSY (0xF0)"
    ex.elapsed_ms = (monotonic() - t0) * 1000
    return ex


def read_status(backend: HidBackend, *, evidence_dir: Path, send: bool = False, confirm: str = "",
                monotonic: Callable[[], float] = time.monotonic) -> StatusResult:
    res = StatusResult("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                       platform.node())
    apps = backend.find(VID, PID_APPLICATION)
    others = {f"0DAD:{pid:04X}": backend.find(VID, pid) for pid in (PID_BOOTLOADER, PID_APPLICATION_ALT)}
    if len(apps) != 1:
        res.message = f"expected exactly one present 0DAD:0051 interface, found {len(apps)}; nothing sent"
        return res
    present = [k for k, v in others.items() if v]
    if present:
        res.message = f"unexpected interface(s) present ({', '.join(present)}); nothing sent"
        return res
    desc = backend.describe(apps[0])
    res.device = desc
    if (desc.vendor_id, desc.product_id) != (VID, PID_APPLICATION):
        res.message = f"driver reports {desc.vendor_id:04X}:{desc.product_id:04X}, not 0DAD:0051; nothing sent"
        return res
    if (desc.input_report_len, desc.output_report_len) != (REPORT_LEN, REPORT_LEN):
        res.message = (f"report sizes are in {desc.input_report_len} / out {desc.output_report_len} bytes, "
                       f"expected {REPORT_LEN}/{REPORT_LEN}; nothing sent")
        return res
    rec, problem = find_app_start_record(evidence_dir, desc.path)
    if rec is None:
        res.message = problem + "; nothing sent"
        return res
    res.app_start_record = rec.name
    if not send:
        res.outcome = "dry_run"
        res.message = ("dry run: would send, once each and in this order, "
                       + ", ".join(f"0x{c:02X}" for c in SEQUENCE) + "; nothing sent")
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    with backend.open(desc.path) as ch:
        for command in SEQUENCE:
            res.sent = True
            ex = _one(ch, command, monotonic)
            res.exchanges.append(ex)
            if ex.problem:
                res.outcome = "stopped"
                res.message = (f"0x{command:02X}: {ex.problem}; not resent, later commands not sent. "
                               "Run again only with a new approval")
                return res
    res.outcome = "complete"
    v = res.exchanges[0].decoded.fields["app_version"]
    res.message = (f"all four status replies valid; application reports {v}. "
                   "Decoded fields are CANDIDATE (meanings from the vendor DLL)")
    return res


def write_evidence(result: StatusResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_status_read",
        description="LED Gate 3: read the LED application's version, cube/objective positions and epi "
                    "channel status (0x40, 0x47, 0x5A, 0x4A; read-only). Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually send the four read requests, once each")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where the StartApp record is read from and this record is written (git-ignored)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("This tool needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res = read_status(backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm)
    print(f"[{res.outcome}] {res.message}")
    if res.app_start_record:
        print(f"application start record: {res.app_start_record}")
    for ex in res.exchanges:
        print(f"0x{ex.command:02X}: {ex.decoded.fields if ex.decoded and ex.decoded.valid else ex.problem}")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
