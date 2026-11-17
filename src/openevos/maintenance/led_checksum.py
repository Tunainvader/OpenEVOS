"""Gate 2 Step B: the LED application's calibration-memory checksum query `0x59` (ADR-0013, L3).

Spec: docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.3 and §6 Step B.

It reports the checksum the board has STORED for its persistent (calibration, `HW_DATA`) memory
and the checksum it COMPUTES over that memory now. It returns no calibration values. Equal
values are evidence the memory is consistent; unequal values mean the calibration must not be
trusted.

What it does, and nothing else:
* Target: exactly one present `0DAD:0051` (the LED application, started under ADR-0012), with no
  `0DAD:0057` or `0DAD:0052` present. The HID driver must report `0DAD:0051` with 65/65-byte
  reports (zero-access handle, nothing sent).
* It needs a StartApp record (`app_appeared`) whose post-start device list contains this
  interface, to show this is the application OpenEVOS started.
* Sends exactly ONE 65-byte report: `00 59`, 62 zero bytes, and the application-protocol checksum
  in byte 64 (two's complement of the 8-bit sum of bytes 1–63, here `0xA7`). Only `0x59` can be built.
* `HidD_FlushQueue`, one `WriteFile` (500 ms), then it reads at most 5 input reports within 1 s,
  accepting the first whose byte 1 is `0xD9` with a valid checksum. Other reports are kept raw
  and ignored. Reading sends nothing. Never resends; "busy" (`0xF0`) is reported, not retried.
* Dry run by default. Sending needs `--send` and the exact confirmation phrase.
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
from openevos.maintenance.led_startapp import EVIDENCE_PREFIX as APP_START_PREFIX

PID_APPLICATION = 0x0051
PID_APPLICATION_ALT = 0x0052
CMD_CHECKSUM = 0x59
REPLY_CHECKSUM = 0xD9
REPLY_BUSY = 0xF0
ALLOWED_COMMANDS = frozenset({CMD_CHECKSUM})
IO_TIMEOUT_S = 0.5
READ_WINDOW_S = 1.0
MAX_READS = 5
CONFIRM_PHRASE = "SEND 59 TO 0DAD:0051"
SPEC = "docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.3, §6 Step B; ADR-0013"


class StepBRefused(RuntimeError):
    """Not sent: a precondition failed. Nothing reached the device."""


def frame_checksum(report: bytes) -> int:
    """Application-protocol checksum (byte 64): the two's complement of the 8-bit sum of bytes 1..63,
    so bytes 1..64 sum to zero. `FUN_180033110` returns `~sum + 1`; the board's replies agree
    (a BUSY frame `F0 … 10`). An earlier version used the plain sum, which the board answered BUSY."""
    return (-sum(report[1:64])) & 0xFF


def build_request(command: int) -> bytes:
    if command not in ALLOWED_COMMANDS:
        raise StepBRefused(f"command 0x{command:02X} is not allowed by Step B (only 0x59); nothing sent")
    body = bytes([0x00, command]) + bytes(REPORT_LEN - 3)
    return body + bytes([frame_checksum(body + b"\x00")])


CHECKSUM_QUERY = build_request(CMD_CHECKSUM)


@dataclass(frozen=True)
class ChecksumReply:
    valid: bool
    problem: str
    reply_code: int | None
    busy: bool
    frame_checksum_ok: bool | None
    stored: int | None  # section 1 (GetPersistentData, holds HW_DATA): BE32 bytes 2–5
    computed: int | None  # section 1: BE32 bytes 6–9
    consistent: bool  # valid and section-1 stored == computed (the HW_DATA verdict)
    stored2: int | None = None  # section 2 (GetPersistentData2): BE32 bytes 10–13
    computed2: int | None = None  # section 2: BE32 bytes 14–17
    consistent2: bool | None = None
    unidentified_hex: str = ""  # bytes 18–63, kept raw


def parse_reply(reply: bytes) -> ChecksumReply:
    def fail(problem, code=None, busy=False, ck=None):
        return ChecksumReply(False, problem, code, busy, ck, None, None, False)

    if len(reply) != REPORT_LEN:
        return fail(f"reply is {len(reply)} bytes, expected {REPORT_LEN}")
    code = reply[1]
    ck_ok = reply[64] == frame_checksum(reply)
    if not ck_ok:
        return fail(f"frame checksum 0x{reply[64]:02X} != 0x{frame_checksum(reply):02X}", code, ck=False)
    if code == REPLY_BUSY:
        return fail("application answered BUSY (0xF0); not retried", code, True, ck_ok)
    if code != REPLY_CHECKSUM:
        return fail(f"reply code 0x{code:02X}, expected 0x{REPLY_CHECKSUM:02X}", code, ck=ck_ok)
    stored, computed, stored2, computed2 = struct.unpack(">IIII", reply[2:18])
    return ChecksumReply(True, "", code, False, True, stored, computed, stored == computed,
                         stored2, computed2, stored2 == computed2, reply[18:64].hex())


@dataclass
class StepBResult:
    outcome: str  # "dry_run" | "refused" | "no_reply" | "reply"
    message: str
    started_utc: str
    host: str
    device: DeviceDescription | None = None
    app_start_record: str | None = None
    request_hex: str = CHECKSUM_QUERY.hex()
    sent: bool = False
    bytes_written: int | None = None
    reply_hex: str | None = None
    ignored_reports_hex: list[str] = field(default_factory=list)
    decoded: ChecksumReply | None = None
    elapsed_ms: float | None = None
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def find_app_start_record(evidence_dir: Path, app_path: str) -> tuple[Path | None, str]:
    records = sorted(evidence_dir.glob(f"{APP_START_PREFIX}-*-app_appeared.json"), reverse=True)
    for path in records:
        try:
            rec = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        after = rec.get("after") or {}
        if any(app_path in (after.get(k) or []) for k in ("0DAD:0051", "0DAD:0052")):
            return path, ""
    return None, ("no StartApp record (app_appeared) lists this application interface; "
                  "start the application with the reviewed tool first")


def query_checksum(backend: HidBackend, *, evidence_dir: Path, send: bool = False, confirm: str = "",
                   monotonic: Callable[[], float] = time.monotonic) -> StepBResult:
    res = StepBResult("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())
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
        res.message = f"dry run: would send {CHECKSUM_QUERY.hex(' ')} once; nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    t0 = monotonic()
    with backend.open(desc.path) as ch:
        ch.flush_input()
        res.sent = True
        try:
            res.bytes_written = ch.write(CHECKSUM_QUERY, IO_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - report, never resend
            res.outcome, res.message = "no_reply", f"write did not complete: {exc}; not resent"
            res.elapsed_ms = (monotonic() - t0) * 1000
            return res
        if res.bytes_written != REPORT_LEN:
            res.outcome = "no_reply"
            res.message = f"wrote {res.bytes_written} of {REPORT_LEN} bytes; no read attempted, not resent"
            res.elapsed_ms = (monotonic() - t0) * 1000
            return res
        for _ in range(MAX_READS):  # passive: reading input reports sends nothing
            left = READ_WINDOW_S - (monotonic() - t0)
            if left <= 0:
                break
            try:
                report = ch.read(REPORT_LEN, min(IO_TIMEOUT_S, left))
            except Exception as exc:  # noqa: BLE001
                res.message = f"no matching reply: {exc}"
                break
            decoded = parse_reply(report)
            if decoded.valid or decoded.busy:
                res.reply_hex, res.decoded = report.hex(), decoded
                break
            res.ignored_reports_hex.append(report.hex())
    res.elapsed_ms = (monotonic() - t0) * 1000
    d = res.decoded
    if d is None:
        res.outcome = "no_reply"
        res.message = (res.message or "no matching reply") + (
            f" ({len(res.ignored_reports_hex)} other report(s) ignored); not resent")
        return res
    res.outcome = "reply"
    if d.busy:
        res.message = "application answered BUSY (0xF0); not resent. Try again only with a new approval"
    elif d.consistent:
        res.message = (f"section 1 (HW_DATA) stored and computed checksums agree (0x{d.stored:08X}): the "
                       "calibration memory is internally consistent. This does not prove the values are correct "
                       "for this instrument")
    else:
        res.message = (f"section 1 (HW_DATA) stored 0x{d.stored:08X} != computed 0x{d.computed:08X}: the "
                       "calibration memory is inconsistent. Do not trust or use HW_DATA; do not proceed to Step C "
                       "without review")
    if not d.busy:
        res.message += (f". Section 2: stored 0x{d.stored2:08X}, computed 0x{d.computed2:08X} "
                        f"({'agree' if d.consistent2 else 'DIFFER'})")
    return res


def write_evidence(result: StepBResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"led-application-stepb-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_checksum",
        description="Gate 2 Step B: ask the LED application (0DAD:0051) for its stored and computed "
                    "calibration-memory checksums with command 0x59. Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually send the one 0x59 request")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where the StartApp record is read from and this record is written (git-ignored)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("Step B needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res = query_checksum(backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm)
    print(f"[{res.outcome}] {res.message}")
    if res.app_start_record:
        print(f"application start record: {res.app_start_record}")
    print(f"request: {CHECKSUM_QUERY.hex(' ')}")
    if res.reply_hex:
        print(f"reply:   {bytes.fromhex(res.reply_hex).hex(' ')}")
    if res.ignored_reports_hex:
        print(f"ignored: {len(res.ignored_reports_hex)} other input report(s), kept in the evidence")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "reply") else 1


if __name__ == "__main__":
    raise SystemExit(main())
