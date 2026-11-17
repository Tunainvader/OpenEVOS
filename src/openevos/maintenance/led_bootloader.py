"""Gate 1: the LED board bootloader version query (ADR-0011; ADR-0009 L3 reviewed read).

Spec: docs/evidence/fl1_v1.6_led_board_static_analysis.md §6, Step A.

What it does, and nothing else:
* Target: exactly one present HID interface with VID 0x0DAD, PID 0x0057 (the bootloader). Any
  other PID (e.g. 0051/0052, the application) is never opened by this tool.
* Before any I/O it checks the HID class driver's own description: VID/PID must match and the
  input and output report sizes must both be 65 bytes.
* Sends exactly one 65-byte output report: report ID 0x00, command 0x0A, 63 zero bytes (no
  checksum: the bootloader protocol has none). The request is a constant; no other command byte
  can be built (`build_request` refuses everything except 0x0A).
* `HidD_FlushQueue`, one `WriteFile`, one `ReadFile`, each limited to 500 ms. No retry, no loop
  on "busy" (0xF0), and nothing else is sent afterwards. Never `StartApp` (0x08).
* Default is a dry run: find and describe the board, print the exact bytes, send nothing.
  Sending needs `--send` and the exact confirmation phrase.
* Writes the raw request and reply bytes and the decoded fields to a JSON evidence file.

What the reply means (from the vendor DLL's checks, Strongly supported; meanings partly unknown):
* byte 1 must be 0x8A (acknowledgement of 0x0A);
* BE32 at bytes 2–5 must be ≥ 2 and BE32 at 6–9 ≥ 1 for the vendor to accept it. What these two
  fields mean is a hypothesis (e.g. protocol revision / application valid);
* application version = BE32 major / minor / build at bytes 22–25 / 26–29 / 30–33.
"""

from __future__ import annotations

import argparse
import json
import platform
import struct
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

VID = 0x0DAD
PID_BOOTLOADER = 0x0057
REPORT_LEN = 65
CMD_VERSION_QUERY = 0x0A
REPLY_VERSION = 0x8A
REPLY_BUSY = 0xF0
ALLOWED_COMMANDS = frozenset({CMD_VERSION_QUERY})  # Gate 1 only; Gate 2 (0x59, 0x51) is held
IO_TIMEOUT_S = 0.5
CONFIRM_PHRASE = "SEND 0A TO 0DAD:0057"
SPEC = "docs/evidence/fl1_v1.6_led_board_static_analysis.md §6 Step A; ADR-0011"


class Gate1Refused(RuntimeError):
    """The query was not sent (precondition failed). Nothing reached the device."""


def build_request(command: int) -> bytes:
    """The only way this module makes a report. Refuses every command except 0x0A."""
    if command not in ALLOWED_COMMANDS:
        raise Gate1Refused(f"command 0x{command:02X} is not allowed by Gate 1 (only 0x0A); nothing sent")
    return bytes([0x00, command]) + bytes(REPORT_LEN - 2)


VERSION_QUERY = build_request(CMD_VERSION_QUERY)


# ---------------------------------------------------------------------------------- reply
@dataclass(frozen=True)
class VersionReply:
    valid: bool  # passes every check the vendor DLL applies
    problem: str  # why not, if not valid
    reply_code: int | None
    busy: bool
    header_a: int | None  # BE32 bytes 2–5 (vendor requires >= 2; meaning unknown)
    header_b: int | None  # BE32 bytes 6–9 (vendor requires >= 1; meaning unknown)
    app_version: tuple[int, int, int] | None  # BE32 major / minor / build at 22–25 / 26–29 / 30–33
    app_version_blank: bool | None  # bytes 22–33 all 0x00 or all 0xFF
    application_image_evidenced: bool  # valid and the application version is not blank
    unidentified_hex: dict[str, str] = field(default_factory=dict)  # bytes not decoded: kept raw


def parse_version_reply(reply: bytes) -> VersionReply:
    def fail(problem: str, code: int | None = None, busy: bool = False) -> VersionReply:
        return VersionReply(False, problem, code, busy, None, None, None, None, False)

    if len(reply) != REPORT_LEN:
        return fail(f"reply is {len(reply)} bytes, expected {REPORT_LEN}")
    code = reply[1]
    if code == REPLY_BUSY:
        return fail("board answered BUSY (0xF0); not retried", code, busy=True)
    if code != REPLY_VERSION:
        return fail(f"reply code 0x{code:02X}, expected 0x{REPLY_VERSION:02X}", code)
    a, b = struct.unpack(">II", reply[2:10])
    ver = struct.unpack(">III", reply[22:34])
    blank = reply[22:34] in (bytes(12), b"\xff" * 12)
    problems = []
    if a < 2:
        problems.append(f"header_a (bytes 2–5) = {a}, vendor requires >= 2")
    if b < 1:
        problems.append(f"header_b (bytes 6–9) = {b}, vendor requires >= 1")
    valid = not problems
    return VersionReply(valid, "; ".join(problems), code, False, a, b, ver, blank, valid and not blank,
                        {"0": reply[0:1].hex(), "10-21": reply[10:22].hex(), "34-64": reply[34:65].hex()})


# ---------------------------------------------------------------------------------- transport seam
@dataclass(frozen=True)
class DeviceDescription:
    path: str
    vendor_id: int
    product_id: int
    version: int
    input_report_len: int
    output_report_len: int


class Channel(Protocol):
    def flush_input(self) -> None: ...
    def write(self, report: bytes, timeout_s: float) -> int: ...
    def read(self, length: int, timeout_s: float) -> bytes: ...


class HidBackend(Protocol):
    def find(self, vid: int, pid: int) -> list[str]: ...
    def describe(self, path: str) -> DeviceDescription: ...
    def open(self, path: str) -> AbstractContextManager[Channel]: ...


class WindowsHid:
    """The real backend (Windows only); imported lazily so tests run anywhere."""

    def __init__(self) -> None:
        from openevos.maintenance import win_hid

        self._w = win_hid

    def find(self, vid: int, pid: int) -> list[str]:
        return self._w.find_interfaces(vid, pid)

    def describe(self, path: str) -> DeviceDescription:
        d = self._w.describe(path)
        return DeviceDescription(d.path, d.vendor_id, d.product_id, d.version, d.input_report_len,
                                 d.output_report_len)

    def open(self, path: str):
        return self._w.WinHidChannel(path)


# ---------------------------------------------------------------------------------- the query
@dataclass
class Gate1Result:
    outcome: str  # "dry_run" | "reply" | "no_reply" | "refused"
    message: str
    started_utc: str
    host: str
    device: DeviceDescription | None = None
    request_hex: str = VERSION_QUERY.hex()
    sent: bool = False
    bytes_written: int | None = None
    reply_hex: str | None = None
    decoded: VersionReply | None = None
    elapsed_ms: float | None = None
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def query_version(backend: HidBackend, *, send: bool = False, confirm: str = "",
                  monotonic: Callable[[], float] = time.monotonic) -> Gate1Result:
    """Gate 1. Dry run unless `send` and the exact confirmation phrase are given."""
    res = Gate1Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      platform.node())
    paths = backend.find(VID, PID_BOOTLOADER)
    if len(paths) != 1:
        res.message = (f"expected exactly one present 0DAD:0057 interface, found {len(paths)}; nothing sent"
                       if paths else "no LED board bootloader (0DAD:0057) is present; nothing sent")
        return res
    desc = backend.describe(paths[0])
    res.device = desc
    if (desc.vendor_id, desc.product_id) != (VID, PID_BOOTLOADER):
        res.message = (f"driver reports {desc.vendor_id:04X}:{desc.product_id:04X}, not 0DAD:0057; nothing sent")
        return res
    if (desc.input_report_len, desc.output_report_len) != (REPORT_LEN, REPORT_LEN):
        res.message = (f"report sizes are in {desc.input_report_len} / out {desc.output_report_len} bytes, "
                       f"expected {REPORT_LEN}/{REPORT_LEN}: not the interface the spec describes; nothing sent")
        return res
    if not send:
        res.outcome = "dry_run"
        res.message = f"dry run: would send {VERSION_QUERY.hex(' ')} once; nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    t0 = monotonic()
    with backend.open(desc.path) as ch:
        ch.flush_input()
        res.sent = True  # from here on the request may have reached the device
        try:
            res.bytes_written = ch.write(VERSION_QUERY, IO_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - report, never retry
            res.outcome, res.message = "no_reply", f"write did not complete: {exc}; not retried"
            res.elapsed_ms = (monotonic() - t0) * 1000
            return res
        if res.bytes_written != REPORT_LEN:
            res.outcome = "no_reply"
            res.message = f"wrote {res.bytes_written} of {REPORT_LEN} bytes; no read attempted, not retried"
            res.elapsed_ms = (monotonic() - t0) * 1000
            return res
        try:
            reply = ch.read(REPORT_LEN, IO_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            res.outcome, res.message = "no_reply", f"no reply: {exc}; not retried"
            res.elapsed_ms = (monotonic() - t0) * 1000
            return res
    res.elapsed_ms = (monotonic() - t0) * 1000
    res.outcome = "reply"
    res.reply_hex = reply.hex()
    res.decoded = parse_version_reply(reply)
    d = res.decoded
    if d.application_image_evidenced:
        major, minor, build = d.app_version  # type: ignore[misc]
        res.message = (f"valid reply; application version {major}.{minor}.{build} reported "
                       "(evidence an application image is present; starting it is a separate, "
                       "operator-approved step: ADR-0012)")
    elif d.valid:
        res.message = "valid reply but the application version is blank: StartApp must not be considered"
    else:
        res.message = f"reply rejected: {d.problem}. StartApp must not be considered"
    return res


def write_evidence(result: Gate1Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"led-bootloader-gate1-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_bootloader",
        description="Gate 1: query the LED board bootloader (0DAD:0057) version with command 0x0A. "
                    "Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually send the one 0x0A request")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where the JSON evidence record goes (git-ignored by default)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("Gate 1 needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res = query_version(backend, send=a.send, confirm=a.confirm)
    print(f"[{res.outcome}] {res.message}")
    if res.device:
        d = res.device
        print(f"device: {d.vendor_id:04X}:{d.product_id:04X} version 0x{d.version:04X}, "
              f"reports in/out {d.input_report_len}/{d.output_report_len} bytes")
    print(f"request: {VERSION_QUERY.hex(' ')}")
    if res.reply_hex:
        print(f"reply:   {bytes.fromhex(res.reply_hex).hex(' ')}")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "reply") else 1


if __name__ == "__main__":
    raise SystemExit(main())
