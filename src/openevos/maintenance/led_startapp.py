"""Start the LED board application from its bootloader: one `0x08` report (ADR-0012, L4).

Review: docs/evidence/fl1_v1.6_startapp_l4_review.md §5. The vendor software sends this exact
report as the first LED-board traffic of every session; it is the only way to reach the
application interface (`0DAD:0051`) that Gate 2 needs.

What it does, and nothing else:
* Preconditions (any failure: nothing is sent):
  - exactly one present `0DAD:0057` interface, and no `0DAD:0051`/`0052` present;
  - the HID driver reports VID/PID `0DAD:0057` and 65/65-byte reports (zero-access handle);
  - a Gate 1 evidence record for this same device interface, at most 30 minutes old, with a
    valid reply and a non-blank application version;
  - the operator's physical checklist phrase and the confirmation phrase, both exact.
* Action: open the interface, ONE `WriteFile` of `00 08` + 63 zero bytes (500 ms limit), close.
  No flush and no read: the vendor reads no reply either. Never retried.
* Afterwards: enumeration only (SetupAPI), once a second for up to 30 s, recording when
  `0DAD:0057` disappears and `0DAD:0051`/`0052` appears. Nothing is ever sent to the application.
* Default is a dry run. Every dry run or attempt writes a JSON evidence record.

Return path if the application does not appear: power-cycle the instrument (expected to bring
back `0057`; to be confirmed and recorded the first time).
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openevos.maintenance.led_bootloader import (
    PID_BOOTLOADER,
    REPORT_LEN,
    VID,
    DeviceDescription,
    HidBackend,
    WindowsHid,
)

CMD_START_APP = 0x08
ALLOWED_COMMANDS = frozenset({CMD_START_APP})
PID_APPLICATION = (0x0051, 0x0052)
IO_TIMEOUT_S = 0.5
WAIT_FOR_APP_S = 30.0
POLL_S = 1.0
GATE1_MAX_AGE = timedelta(minutes=30)
CONFIRM_PHRASE = "START LED APP ON 0DAD:0057"
CHECKLIST_PHRASE = "STAGE EMPTY, LID CLOSED, OBSERVER PRESENT"
REVIEW = "docs/evidence/fl1_v1.6_startapp_l4_review.md §5; ADR-0012"
EVIDENCE_PREFIX = "led-bootloader-startapp"  # evidence file names; other tools find records by it


class StartRefused(RuntimeError):
    """Not sent: a precondition failed. Nothing reached the device."""


def build_report(command: int) -> bytes:
    """The only report this module can build: `00 08` + 63 zero bytes."""
    if command not in ALLOWED_COMMANDS:
        raise StartRefused(f"command 0x{command:02X} is not allowed here (only 0x08); nothing sent")
    return bytes([0x00, command]) + bytes(REPORT_LEN - 2)


START_REPORT = build_report(CMD_START_APP)


@dataclass
class StartResult:
    outcome: str  # "dry_run" | "refused" | "write_failed" | "app_appeared" | "no_app"
    message: str
    started_utc: str
    host: str
    device: DeviceDescription | None = None
    gate1_record: str | None = None
    request_hex: str = START_REPORT.hex()
    sent: bool = False
    bytes_written: int | None = None
    before: dict[str, list[str]] = field(default_factory=dict)
    after: dict[str, list[str]] = field(default_factory=dict)
    bootloader_gone_after_s: float | None = None
    app_appeared_after_s: float | None = None
    waited_s: float | None = None
    review: str = REVIEW

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _devices(backend: HidBackend) -> dict[str, list[str]]:
    out = {"0DAD:0057": backend.find(VID, PID_BOOTLOADER)}
    for pid in PID_APPLICATION:
        out[f"0DAD:{pid:04X}"] = backend.find(VID, pid)
    return out


def find_gate1_record(evidence_dir: Path, device_path: str, now: datetime) -> tuple[Path | None, str]:
    """Newest Gate 1 reply record for this device; returns (path, problem)."""
    records = sorted(evidence_dir.glob("led-bootloader-gate1-*-reply.json"), reverse=True)
    if not records:
        return None, f"no Gate 1 reply record in {evidence_dir}; run Gate 1 first"
    for path in records:
        try:
            rec = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if (rec.get("device") or {}).get("path") != device_path:
            continue
        started = datetime.fromisoformat(rec["started_utc"])
        if now - started > GATE1_MAX_AGE:
            return None, (f"newest Gate 1 record for this device ({path.name}) is older than "
                          f"{GATE1_MAX_AGE.seconds // 60} min; run Gate 1 again")
        decoded = rec.get("decoded") or {}
        if rec.get("outcome") != "reply" or not decoded.get("valid") or not decoded.get("application_image_evidenced"):
            return None, f"Gate 1 record {path.name} does not show a valid reply with a non-blank application version"
        return path, ""
    return None, "no Gate 1 reply record for this device interface; run Gate 1 first"


def start_app(backend: HidBackend, *, evidence_dir: Path, send: bool = False, confirm: str = "",
              checklist: str = "", monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> StartResult:
    res = StartResult("refused", "", now().isoformat(timespec="milliseconds"), platform.node())
    res.before = _devices(backend)
    boot = res.before["0DAD:0057"]
    if len(boot) != 1:
        res.message = f"expected exactly one present 0DAD:0057 interface, found {len(boot)}; nothing sent"
        return res
    present_app = [k for k in res.before if k != "0DAD:0057" and res.before[k]]
    if present_app:
        res.message = f"an application interface is already present ({', '.join(present_app)}); nothing sent"
        return res
    desc = backend.describe(boot[0])
    res.device = desc
    if (desc.vendor_id, desc.product_id) != (VID, PID_BOOTLOADER):
        res.message = f"driver reports {desc.vendor_id:04X}:{desc.product_id:04X}, not 0DAD:0057; nothing sent"
        return res
    if (desc.input_report_len, desc.output_report_len) != (REPORT_LEN, REPORT_LEN):
        res.message = (f"report sizes are in {desc.input_report_len} / out {desc.output_report_len} bytes, "
                       f"expected {REPORT_LEN}/{REPORT_LEN}; nothing sent")
        return res
    gate1, problem = find_gate1_record(evidence_dir, desc.path, now())
    if gate1 is None:
        res.message = problem + "; nothing sent"
        return res
    res.gate1_record = gate1.name
    if not send:
        res.outcome = "dry_run"
        res.message = f"dry run: all preconditions hold; would send {START_REPORT.hex(' ')} once; nothing sent"
        return res
    if checklist != CHECKLIST_PHRASE:
        res.message = f"physical checklist phrase missing or wrong (expected {CHECKLIST_PHRASE!r}); nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    with backend.open(desc.path) as ch:
        res.sent = True  # from here on the report may have reached the board
        try:
            res.bytes_written = ch.write(START_REPORT, IO_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - report, never retry
            res.outcome, res.message = "write_failed", f"write did not complete: {exc}; not retried"
            res.after = _devices(backend)
            return res
    if res.bytes_written != REPORT_LEN:
        res.outcome = "write_failed"
        res.message = f"wrote {res.bytes_written} of {REPORT_LEN} bytes; not retried"
        res.after = _devices(backend)
        return res

    t0 = monotonic()
    while True:
        elapsed = monotonic() - t0
        seen = _devices(backend)
        if res.bootloader_gone_after_s is None and not seen["0DAD:0057"]:
            res.bootloader_gone_after_s = round(elapsed, 2)
        if any(seen[k] for k in seen if k != "0DAD:0057"):
            res.app_appeared_after_s = round(elapsed, 2)
            res.after, res.waited_s = seen, round(elapsed, 2)
            res.outcome = "app_appeared"
            res.message = (f"application interface appeared after {elapsed:.1f} s. Nothing was sent to it. "
                           "Gate 2 reads need their own approval; a power cycle returns the board to its bootloader")
            return res
        if elapsed >= WAIT_FOR_APP_S:
            res.after, res.waited_s = seen, round(elapsed, 2)
            res.outcome = "no_app"
            res.message = (f"no application interface within {WAIT_FOR_APP_S:.0f} s; nothing more sent, not retried. "
                           "Power-cycle the instrument when convenient and check that 0DAD:0057 returns")
            return res
        sleep(POLL_S)


def write_evidence(result: StartResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_startapp",
        description="Start the LED board application from its bootloader (0DAD:0057) with one 0x08 report. "
                    "Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually send the one 0x08 report")
    p.add_argument("--checklist", default="", help=f"must be exactly: {CHECKLIST_PHRASE}")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where Gate 1 records are read from and this record is written (git-ignored)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("This tool needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res = start_app(backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm, checklist=a.checklist)
    print(f"[{res.outcome}] {res.message}")
    if res.gate1_record:
        print(f"gate 1 record: {res.gate1_record}")
    print(f"request: {START_REPORT.hex(' ')}")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "app_appeared") else 1


if __name__ == "__main__":
    raise SystemExit(main())
