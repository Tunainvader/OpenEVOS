"""Gate 2 Step D: read PearlScope's calibration blob from LED persistent-data section 2 (ADR-0015, L3).

Spec: docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.5a. PearlScope stores the
instrument's persistent data (camera, motor-controller and turret data) as ONE compressed,
serialized blob, read by `GetPersistentData2Vector` (command `0x31` -> `0xB1`, 58-byte chunks):

    0x15713  4 B  magic, bytes 6C 69 63 6F ("lico"); PearlScope aborts unless it matches
    0x15717  8 B  header value (probably a timestamp)
    0x1571F  4 B  size handed to the decoder (probably the uncompressed length)
    0x15723  4 B  stored length N (PearlScope refuses N >= 35034)
    0x15727  N B  the blob

What it does, and nothing else:
* Same target and preconditions as Step C: exactly one `0DAD:0051` (no `0057`/`0052`), 65/65-byte
  reports, a Step B record for this interface <= 30 min old with both sections consistent, the
  exact phrase. Dry run by default.
* Only `0x31` reads can be built: the four header reads at the vendor's own sizes, and blob chunks
  of at most 58 bytes starting at `0x15727 + k*58`, never beyond `0x15727 + 35033`.
* Phase 1, the header: 4 reads. It STOPS unless the magic is "lico" and 0 < N < 35034.
  `--header-only` stops here by design.
* Phase 2, the blob: ceil(N/58) chunk reads, each requested once; fail fast on any problem.
* Per request: flush, one write (500 ms), up to 3 passive reads within 1 s, accepting the first
  `0xB1` with a valid two's-complement frame, the requested count and the echoed address.
* Output (git-ignored): JSON of the header and per-chunk raw exchange; the blob as .bin; and,
  decoded offline, a zlib/deflate decompression attempt saved as .bin if it succeeds. The tool does
  not interpret the contents: they are CANDIDATE data.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import struct
import sys
import time
import zlib
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
from openevos.maintenance.led_calibration_read import find_step_b_record
from openevos.maintenance.led_checksum import PID_APPLICATION, PID_APPLICATION_ALT, frame_checksum

CMD_READ2 = 0x31
REPLY_READ2 = 0xB1
REPLY_BUSY = 0xF0
ALLOWED_COMMANDS = frozenset({CMD_READ2})
CHUNK = 58
BLOB_START = 0x15727
MAX_N = 0x88DA - 1  # PearlScope refuses N >= 0x88DA
MAGIC = b"lico"
HEADER = (("magic", 0x15713, 4), ("header_value", 0x15717, 8), ("decoded_size", 0x1571F, 4),
          ("stored_length", 0x15723, 4))
HEADER_READS = frozenset((a, s) for _, a, s in HEADER)
IO_TIMEOUT_S = 0.5
READ_WINDOW_S = 1.0
MAX_READS_PER_REQUEST = 3
CONFIRM_PHRASE = "READ SECTION 2 BLOB FROM 0DAD:0051"
SPEC = "docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.5a; ADR-0015"


class StepDRefused(RuntimeError):
    """Not sent: a precondition failed, or the request is outside the reviewed layout."""


def _allowed(address: int, count: int) -> bool:
    if (address, count) in HEADER_READS:
        return True
    off = address - BLOB_START
    return 0 <= off and off % CHUNK == 0 and 1 <= count <= CHUNK and off + count <= MAX_N


def build_request(command: int, address: int, count: int) -> bytes:
    if command not in ALLOWED_COMMANDS:
        raise StepDRefused(f"command 0x{command:02X} is not allowed by Step D (only 0x31)")
    if not _allowed(address, count):
        raise StepDRefused(f"({address:#07x}, {count}) is not a reviewed section-2 read")
    r = bytearray(REPORT_LEN)
    r[1] = command
    r[2:5] = address.to_bytes(3, "big")
    r[5] = count
    r[64] = frame_checksum(bytes(r))
    return bytes(r)


def parse_reply(reply: bytes, address: int, count: int) -> tuple[bytes | None, str, bool]:
    """(payload, problem, busy)."""
    if len(reply) != REPORT_LEN:
        return None, f"reply is {len(reply)} bytes", False
    if reply[64] != frame_checksum(reply):
        return None, "frame checksum invalid", False
    if reply[1] == REPLY_BUSY:
        return None, "BUSY (0xF0)", True
    if reply[1] != REPLY_READ2:
        return None, f"reply code 0x{reply[1]:02X}", False
    if int.from_bytes(reply[2:5], "big") != address:
        return None, "reply does not echo the requested address", False
    if reply[5] != count:
        return None, f"reply count {reply[5]} != requested {count}", False
    return reply[6:6 + count], "", False


@dataclass
class Exchange:
    address: int
    count: int
    request_hex: str
    reply_hex: str | None = None
    ignored_hex: list[str] = field(default_factory=list)
    problem: str = ""


@dataclass
class StepDResult:
    outcome: str  # "dry_run" | "refused" | "stopped" | "header_only" | "complete"
    message: str
    started_utc: str
    host: str
    device: DeviceDescription | None = None
    step_b_record: str | None = None
    sent_requests: int = 0
    header: dict[str, object] = field(default_factory=dict)
    exchanges: list[Exchange] = field(default_factory=list)
    blob_bytes: int | None = None
    blob_file: str | None = None
    decode: dict[str, object] = field(default_factory=dict)
    capability_source: str = "CANDIDATE"
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, default=str)


def _exchange(ch, res: StepDResult, address: int, count: int, monotonic) -> bytes | None:
    ex = Exchange(address, count, build_request(CMD_READ2, address, count).hex())
    res.exchanges.append(ex)
    ch.flush_input()
    res.sent_requests += 1
    try:
        written = ch.write(bytes.fromhex(ex.request_hex), IO_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        ex.problem = f"write did not complete: {exc}"
        return None
    if written != REPORT_LEN:
        ex.problem = f"wrote {written} of {REPORT_LEN} bytes"
        return None
    t0 = monotonic()
    for _ in range(MAX_READS_PER_REQUEST):  # passive reads only
        left = READ_WINDOW_S - (monotonic() - t0)
        if left <= 0:
            break
        try:
            reply = ch.read(REPORT_LEN, min(IO_TIMEOUT_S, left))
        except Exception as exc:  # noqa: BLE001
            ex.problem = f"no reply: {exc}"
            return None
        payload, why, busy = parse_reply(reply, address, count)
        if busy:
            ex.reply_hex, ex.problem = reply.hex(), why
            return None
        if payload is not None:
            ex.reply_hex = reply.hex()
            return payload
        ex.ignored_hex.append(reply.hex())
    ex.problem = ex.problem or "no matching reply"
    return None


def decode_header(raw: dict[str, bytes]) -> dict[str, object]:
    hv = raw["header_value"]
    return {"magic_hex": raw["magic"].hex(), "magic_ok": raw["magic"] == MAGIC,
            "header_value_hex": hv.hex(), "header_value_le_u64": struct.unpack("<Q", hv)[0],
            "header_value_le_double": struct.unpack("<d", hv)[0],
            "decoded_size": struct.unpack("<i", raw["decoded_size"])[0],
            "stored_length": struct.unpack("<i", raw["stored_length"])[0]}


def try_decompress(blob: bytes, expected: int) -> tuple[dict[str, object], bytes | None]:
    """Offline only. Tries zlib, then raw deflate, then gzip. Never guesses beyond that."""
    for name, wbits in (("zlib", 15), ("raw deflate", -15), ("gzip", 31)):
        try:
            out = zlib.decompress(blob, wbits)
        except zlib.error:
            continue
        return {"method": name, "decoded_bytes": len(out), "matches_decoded_size": len(out) == expected}, out
    return {"method": None, "note": "not zlib/deflate/gzip; the format needs offline analysis"}, None


def read_section2(backend: HidBackend, *, evidence_dir: Path, send: bool = False, confirm: str = "",
                  header_only: bool = False, monotonic: Callable[[], float] = time.monotonic,
                  now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> tuple[StepDResult, bytes | None, bytes | None]:
    res = StepDResult("refused", "", now().isoformat(timespec="milliseconds"), platform.node())
    apps = backend.find(VID, PID_APPLICATION)
    if len(apps) != 1:
        res.message = f"expected exactly one present 0DAD:0051 interface, found {len(apps)}; nothing sent"
        return res, None, None
    present = [f"0DAD:{p:04X}" for p in (PID_BOOTLOADER, PID_APPLICATION_ALT) if backend.find(VID, p)]
    if present:
        res.message = f"unexpected interface(s) present ({', '.join(present)}); nothing sent"
        return res, None, None
    desc = backend.describe(apps[0])
    res.device = desc
    if (desc.vendor_id, desc.product_id) != (VID, PID_APPLICATION) or \
            (desc.input_report_len, desc.output_report_len) != (REPORT_LEN, REPORT_LEN):
        res.message = "driver does not report 0DAD:0051 with 65/65-byte reports; nothing sent"
        return res, None, None
    rec, problem = find_step_b_record(evidence_dir, desc.path, now())
    if rec is None:
        res.message = problem + "; nothing sent"
        return res, None, None
    res.step_b_record = rec.name
    if not send:
        res.outcome = "dry_run"
        res.exchanges = [Exchange(a, s, build_request(CMD_READ2, a, s).hex()) for _, a, s in HEADER]
        res.message = ("dry run: would read the 4 header fields, then (unless --header-only) the blob in "
                       f"{CHUNK}-byte chunks, each once; nothing sent")
        return res, None, None
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res, None, None

    blob = bytearray()
    with backend.open(desc.path) as ch:
        raw: dict[str, bytes] = {}
        for name, addr, size in HEADER:
            got = _exchange(ch, res, addr, size, monotonic)
            if got is None:
                res.outcome = "stopped"
                res.message = f"stopped reading header field {name}: {res.exchanges[-1].problem}. Not retried"
                return res, None, None
            raw[name] = got
        res.header = decode_header(raw)
        n = res.header["stored_length"]
        if not res.header["magic_ok"]:
            res.outcome = "stopped"
            res.message = f"magic is {raw['magic'].hex()}, not 'lico': no PearlScope blob here; blob not read"
            return res, None, None
        if not 0 < n <= MAX_N:  # type: ignore[operator]
            res.outcome = "stopped"
            res.message = f"stored length {n} is outside 1..{MAX_N}; blob not read"
            return res, None, None
        if header_only:
            res.outcome = "header_only"
            res.message = f"header valid: magic 'lico', stored length {n} bytes ({math.ceil(n / CHUNK)} chunks); blob not read"
            return res, None, None
        for k in range(math.ceil(n / CHUNK)):  # type: ignore[operator]
            count = min(CHUNK, n - k * CHUNK)  # type: ignore[operator]
            got = _exchange(ch, res, BLOB_START + k * CHUNK, count, monotonic)
            if got is None:
                res.outcome = "stopped"
                res.blob_bytes = len(blob)
                res.message = (f"stopped at chunk {k + 1} of {math.ceil(n / CHUNK)}: {res.exchanges[-1].problem}. "
                               f"Not retried; {len(blob)} of {n} bytes read")
                return res, bytes(blob), None
            blob += got
    res.blob_bytes = len(blob)
    res.decode, decoded = try_decompress(bytes(blob), res.header["decoded_size"])  # type: ignore[arg-type]
    res.outcome = "complete"
    res.message = (f"read the header and the {len(blob)}-byte blob, each request once; offline decode: "
                   f"{res.decode.get('method') or 'unrecognised format'}. Contents are CANDIDATE data")
    return res, bytes(blob), decoded


def write_evidence(result: StepDResult, blob: bytes | None, decoded: bytes | None, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"led-application-stepd-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{result.outcome}"
    if blob:
        (out_dir / f"{stem}-blob.bin").write_bytes(blob)
        result.blob_file = f"{stem}-blob.bin"
    if decoded:
        (out_dir / f"{stem}-decoded.bin").write_bytes(decoded)
        result.decode["decoded_file"] = f"{stem}-decoded.bin"
    path = out_dir / f"{stem}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_section2_read",
        description="Gate 2 Step D: read PearlScope's calibration blob from LED persistent-data section 2 "
                    "(0x31 reads). Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually send the reads")
    p.add_argument("--header-only", action="store_true", help="read and check the 4 header fields, then stop")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where the Step B record is read from and this record is written (git-ignored)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("Step D needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res, blob, decoded = read_section2(backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm,
                                       header_only=a.header_only)
    print(f"[{res.outcome}] {res.message}")
    if res.step_b_record:
        print(f"step B record: {res.step_b_record}")
    if res.header:
        h = res.header
        print(f"header: magic {h['magic_hex']} ok={h['magic_ok']}, stored length {h['stored_length']}, "
              f"decoded size {h['decoded_size']}, header value {h['header_value_hex']}")
    if res.outcome == "dry_run":
        print(f"first request: {bytes.fromhex(res.exchanges[0].request_hex).hex(' ')}")
    if res.sent_requests or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, blob, decoded, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "header_only", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
