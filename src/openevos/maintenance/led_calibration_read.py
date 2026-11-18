"""Gate 2 Step C: read the 76 mapped HW_DATA fields from the LED application (ADR-0014, L3).

Spec: docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.3, §5.5, §6 Step C;
field map: docs/evidence/fl1_v1.6_hwdata_address_map.md.

What it does, and nothing else:
* Target: exactly one present `0DAD:0051`, no `0057`/`0052`; the driver reports `0DAD:0051` with
  65/65-byte reports (zero-access handle, nothing sent).
* Needs a Step B record for this same interface, at most 30 minutes old, with BOTH persistent-data
  sections consistent (stored == computed).
* Reads only the 76 mapped fields, one request per field, at the vendor's own size for that field
  (4 bytes int32 or 8 bytes double, little-endian). `build_request` refuses any (address, size)
  pair not in the map, and any command other than `0x51`.
* Request: `00 51`, BE24 address, count, zeros, two's-complement frame checksum in byte 64.
* Per field: flush, ONE write (500 ms), then up to 3 input reports within 1 s, accepting the first
  with code `0xD1`, a valid frame checksum and `reply[5] == count`. Unrelated reports are kept raw.
* Fail fast: on any write failure, timeout, BUSY (`0xF0`) or no matching reply, it STOPS the
  whole sequence. No field is ever requested twice.
* Dry run by default; sending needs `--send` and the exact phrase. It writes nothing to the board:
  the vendor's SetPersistentData is banned by ADR-0009 and only `0x51` can be built here.
* Output: a git-ignored JSON with the raw exchange and decoded values, plus sanity checks. Values
  are CANDIDATE data with UNKNOWN units (the int32 travel limits are probably encoder counts); they
  are not turned into capability limits by this tool.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openevos.maintenance.hwdata_fields import HW_DATA_FIELDS
from openevos.maintenance.led_bootloader import (
    PID_BOOTLOADER,
    REPORT_LEN,
    VID,
    DeviceDescription,
    HidBackend,
    WindowsHid,
)
from openevos.maintenance.led_checksum import PID_APPLICATION, PID_APPLICATION_ALT, frame_checksum

CMD_READ = 0x51
REPLY_READ = 0xD1
REPLY_BUSY = 0xF0
ALLOWED_COMMANDS = frozenset({CMD_READ})
ALLOWED_READS = frozenset((addr, size) for _, _, addr, size in HW_DATA_FIELDS)
IO_TIMEOUT_S = 0.5
READ_WINDOW_S = 1.0
MAX_READS_PER_FIELD = 3
STEP_B_MAX_AGE = timedelta(minutes=30)
CONFIRM_PHRASE = "READ 76 HW_DATA FIELDS FROM 0DAD:0051"
SPEC = "docs/evidence/fl1_v1.6_led_board_static_analysis.md §6 Step C; ADR-0014"


class StepCRefused(RuntimeError):
    """Not sent: a precondition failed, or the request is outside the reviewed field map."""


def build_request(command: int, address: int, count: int) -> bytes:
    if command not in ALLOWED_COMMANDS:
        raise StepCRefused(f"command 0x{command:02X} is not allowed by Step C (only 0x51)")
    if (address, count) not in ALLOWED_READS:
        raise StepCRefused(f"({address:#06x}, {count}) is not a mapped HW_DATA field read")
    body = bytearray(REPORT_LEN)
    body[1] = command
    body[2:5] = address.to_bytes(3, "big")
    body[5] = count
    body[64] = frame_checksum(bytes(body))
    return bytes(body)


@dataclass
class FieldRead:
    e_data_type: int
    name: str
    address: int
    size: int
    request_hex: str
    reply_hex: str | None = None
    ignored_hex: list[str] = field(default_factory=list)
    value: float | int | None = None
    problem: str = ""


def parse_reply(reply: bytes, count: int) -> tuple[bytes | None, str, bool]:
    """(payload, problem, busy)."""
    if len(reply) != REPORT_LEN:
        return None, f"reply is {len(reply)} bytes", False
    if reply[64] != frame_checksum(reply):
        return None, "frame checksum invalid", False
    if reply[1] == REPLY_BUSY:
        return None, "BUSY (0xF0)", True
    if reply[1] != REPLY_READ:
        return None, f"reply code 0x{reply[1]:02X}", False
    if reply[5] != count:
        return None, f"reply count {reply[5]} != requested {count}", False
    return reply[6:6 + count], "", False


def decode(payload: bytes, size: int) -> float | int:
    return struct.unpack("<d", payload)[0] if size == 8 else struct.unpack("<i", payload)[0]


@dataclass
class StepCResult:
    outcome: str  # "dry_run" | "refused" | "stopped" | "complete"
    message: str
    started_utc: str
    host: str
    device: DeviceDescription | None = None
    step_b_record: str | None = None
    sent_requests: int = 0
    fields: list[FieldRead] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    units: str = "UNKNOWN: int32 limits/offsets are probably encoder counts; doubles as stored"
    capability_source: str = "CANDIDATE"
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def find_step_b_record(evidence_dir: Path, app_path: str, now: datetime) -> tuple[Path | None, str]:
    for path in sorted(evidence_dir.glob("led-application-stepb-*-reply.json"), reverse=True):
        try:
            rec = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if (rec.get("device") or {}).get("path") != app_path:
            continue
        if now - datetime.fromisoformat(rec["started_utc"]) > STEP_B_MAX_AGE:
            return None, f"newest Step B record for this interface ({path.name}) is older than 30 min; run Step B again"
        d = rec.get("decoded") or {}
        if not (d.get("valid") and d.get("consistent") and d.get("consistent2")):
            return None, f"Step B record {path.name} does not show both sections consistent"
        return path, ""
    return None, "no Step B reply record for this application interface; run Step B first"


def sanity_checks(values: dict[str, float | int]) -> dict[str, str]:
    """Plausibility only (no unit claims). Each check is 'pass', 'FAIL' or 'n/a'."""
    out: dict[str, str] = {}
    doubles = {k: v for k, v in values.items() if isinstance(v, float)}
    out["all doubles finite"] = "pass" if all(math.isfinite(v) for v in doubles.values()) else "FAIL"
    for axis in ("X", "Y", "Z", "EPI"):
        lo, hi = values.get(f"HW_DATA__{axis}_POS_MIN"), values.get(f"HW_DATA__{axis}_POS_MAX")
        out[f"{axis} min < max"] = "n/a" if lo is None or hi is None else ("pass" if lo < hi else "FAIL")
    for axis in ("X", "Y", "Z"):
        v = values.get(f"HW_DATA__{axis}_AXIS_MM_PER_REVOLUTION")
        out[f"{axis} mm/rev > 0 and finite"] = "n/a" if v is None else (
            "pass" if math.isfinite(v) and v > 0 else "FAIL")
    blank = [k for k, v in values.items() if isinstance(v, int) and v in (-1, 0)]
    out["int32 fields that are 0 or -1 (erased?)"] = f"{len(blank)} of {sum(isinstance(v, int) for v in values.values())}"  # noqa: E501
    return out


def read_fields(backend: HidBackend, *, evidence_dir: Path, send: bool = False, confirm: str = "",
                monotonic: Callable[[], float] = time.monotonic,
                now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> StepCResult:
    res = StepCResult("refused", "", now().isoformat(timespec="milliseconds"), platform.node())
    apps = backend.find(VID, PID_APPLICATION)
    if len(apps) != 1:
        res.message = f"expected exactly one present 0DAD:0051 interface, found {len(apps)}; nothing sent"
        return res
    present = [f"0DAD:{p:04X}" for p in (PID_BOOTLOADER, PID_APPLICATION_ALT) if backend.find(VID, p)]
    if present:
        res.message = f"unexpected interface(s) present ({', '.join(present)}); nothing sent"
        return res
    desc = backend.describe(apps[0])
    res.device = desc
    if (desc.vendor_id, desc.product_id) != (VID, PID_APPLICATION) or \
            (desc.input_report_len, desc.output_report_len) != (REPORT_LEN, REPORT_LEN):
        res.message = (f"driver reports {desc.vendor_id:04X}:{desc.product_id:04X} with reports "
                       f"{desc.input_report_len}/{desc.output_report_len} B; expected 0DAD:0051, 65/65; nothing sent")
        return res
    rec, problem = find_step_b_record(evidence_dir, desc.path, now())
    if rec is None:
        res.message = problem + "; nothing sent"
        return res
    res.step_b_record = rec.name
    plan = [FieldRead(e, n, a, s, build_request(CMD_READ, a, s).hex()) for e, n, a, s in HW_DATA_FIELDS]
    if not send:
        res.outcome, res.fields = "dry_run", plan
        res.message = f"dry run: would send {len(plan)} single 0x51 reads (4/8 bytes each), once each; nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    values: dict[str, float | int] = {}
    with backend.open(desc.path) as ch:
        for fr in plan:
            res.fields.append(fr)
            request = bytes.fromhex(fr.request_hex)
            ch.flush_input()
            try:
                res.sent_requests += 1
                written = ch.write(request, IO_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                fr.problem = f"write did not complete: {exc}"
                break
            if written != REPORT_LEN:
                fr.problem = f"wrote {written} of {REPORT_LEN} bytes"
                break
            t0 = monotonic()
            for _ in range(MAX_READS_PER_FIELD):  # passive reads only
                left = READ_WINDOW_S - (monotonic() - t0)
                if left <= 0:
                    break
                try:
                    reply = ch.read(REPORT_LEN, min(IO_TIMEOUT_S, left))
                except Exception as exc:  # noqa: BLE001
                    fr.problem = f"no reply: {exc}"
                    break
                payload, why, busy = parse_reply(reply, fr.size)
                if payload is not None or busy:
                    fr.reply_hex = reply.hex()
                    if busy:
                        fr.problem = why
                    else:
                        fr.value = decode(payload, fr.size)
                        values[fr.name] = fr.value
                    break
                fr.ignored_hex.append(reply.hex())
            if fr.value is None:
                fr.problem = fr.problem or "no matching reply"
                break
    res.checks = sanity_checks(values)
    if len(values) == len(plan):
        res.outcome = "complete"
        res.message = (f"read {len(values)} of {len(plan)} fields, one request each. Values are CANDIDATE data, "
                       "units unknown. Run Step B again to confirm the checksums are unchanged")
    else:
        bad = res.fields[-1]
        res.outcome = "stopped"
        res.message = (f"stopped at field {len(res.fields)} of {len(plan)} ({bad.name} @ {bad.address:#06x}): "
                       f"{bad.problem}. Not retried; {len(values)} fields read")
    return res


def write_evidence(result: StepCResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"led-application-stepc-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_calibration_read",
        description="Gate 2 Step C: read the 76 mapped HW_DATA fields from the LED application (0DAD:0051), "
                    "one 0x51 request per field. Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually send the reads")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where the Step B record is read from and this record is written (git-ignored)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("Step C needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res = read_fields(backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm)
    print(f"[{res.outcome}] {res.message}")
    if res.step_b_record:
        print(f"step B record: {res.step_b_record}")
    if res.outcome == "dry_run":
        first = res.fields[0]
        print(f"first request ({first.name} @ {first.address:#06x}, {first.size} B): "
              f"{bytes.fromhex(first.request_hex).hex(' ')}")
    for k, v in res.checks.items():
        print(f"check: {k}: {v}")
    if res.sent_requests or res.outcome == "dry_run":
        print(f"evidence (git-ignored, contains values): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
