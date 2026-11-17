"""LED Gate 4: switch the brightfield LED and the epi (cube) LED on briefly, then off (ADR-0034, RAM write).

Spec: docs/evidence/fl1_v1.6_led_app_command_table.md §6.

Only two write commands exist here, each with value 0 (off) or 1 (on), and nothing else:
* `0x43` brightfield PWM enable: request bytes 2–3 = BE16 0/1. Firmware `0x53A2` sets the PWM output-compare
  mode and LATB12; no flash. Reply `0xC3`, bytes 2–3 = read-back.
* `0x49` epi enable: request byte 2 = 0/1. Firmware `0x4A7E` drives the epi LED pin; no flash. Reply `0xC9`,
  byte 2 = read-back. In moveable-carriage mode only the cube in the light path lights.
Intensity, PWM width/period and current mode are NOT changed: the board's current settings are used.
Reads `0x45` (brightfield state) and `0x4A` (epi state) before and after.

Sequence (each request once, never resent): read 0x45, read 0x4A; brightfield ON, hold, OFF; epi ON, hold,
OFF; read 0x45, read 0x4A. Any failure stops the sequence, but every light switched on (or possibly on) is
then sent its OFF once. If an OFF fails, the tool says so: power-cycle the instrument.
Dry run by default; needs the checklist phrase and the confirmation phrase.
"""

from __future__ import annotations

import argparse
import json
import platform
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
from openevos.maintenance.led_status_read import (
    EPI_CURRENT_MODES,
    PID_APPLICATION,
    PID_APPLICATION_ALT,
)

CMD_BF_STATUS = 0x45
CMD_EPI_STATUS = 0x4A
CMD_BF_ENABLE = 0x43
CMD_EPI_ENABLE = 0x49
ALLOWED_COMMANDS = frozenset({CMD_BF_STATUS, CMD_EPI_STATUS, CMD_BF_ENABLE, CMD_EPI_ENABLE})
WRITE_COMMANDS = frozenset({CMD_BF_ENABLE, CMD_EPI_ENABLE})
REPLY_BUSY = 0xF0
IO_TIMEOUT_S = 0.5
READ_WINDOW_S = 1.0
MAX_READS = 5
HOLD_DEFAULT_S = 3.0
HOLD_MAX_S = 10.0
HOLD_MAX_BRIGHTFIELD_ONLY_S = 120.0  # white transmitted light only (camera tests); epi stays limited to 10 s
ONLY_CHOICES = ("brightfield", "epi")
CONFIRM_PHRASE = "LIGHT TEST BRIGHTFIELD AND EPI ON 0DAD:0051"
CHECKLIST_PHRASE = "NOBODY LOOKING INTO THE LIGHT PATH, OBSERVER PRESENT"
SPEC = "docs/evidence/fl1_v1.6_led_app_command_table.md §6; ADR-0034"
EVIDENCE_PREFIX = "led-application-gate4-light"


class LightTestRefused(RuntimeError):
    """Not sent: a precondition failed. Nothing reached the device."""


def build_request(command: int, value: int | None = None) -> bytes:
    """Only the four commands above; writes only with value 0 or 1, reads only without a value."""
    if command not in ALLOWED_COMMANDS:
        raise LightTestRefused(f"command 0x{command:02X} is not in the Gate 4 set; nothing sent")
    body = bytearray(REPORT_LEN)
    body[1] = command
    if command in WRITE_COMMANDS:
        if value not in (0, 1):
            raise LightTestRefused(f"0x{command:02X} accepts only 0 (off) or 1 (on), not {value!r}; nothing sent")
        if command == CMD_BF_ENABLE:
            body[2], body[3] = 0x00, value  # BE16, as the vendor's PWMEnableSync
        else:
            body[2] = value  # Gen3EpiEnableType: 0 disabled, 1 enabled
    elif value is not None:
        raise LightTestRefused(f"read 0x{command:02X} takes no value; nothing sent")
    body[64] = frame_checksum(bytes(body))
    return bytes(body)


def _u16(r: bytes, i: int) -> int:
    return (r[i] << 8) | r[i + 1]


def decode(command: int, r: bytes) -> dict:
    """Byte layouts from the firmware handlers (spec §6). CANDIDATE."""
    if command == CMD_BF_STATUS:
        return {"bf_enable": _u16(r, 2), "bf_pwm_width": _u16(r, 4), "bf_pwm_period": _u16(r, 6),
                "bf_current_mode": _u16(r, 8)}
    if command == CMD_EPI_STATUS:
        return {"epi_enable": r[2], "epi_digipot_status": r[3], "epi_digipot": r[4],
                "epi_current_mode_name": EPI_CURRENT_MODES.get(_u16(r, 7))}
    if command == CMD_BF_ENABLE:
        return {"bf_enable_readback": _u16(r, 2)}
    return {"epi_enable_readback": r[2], "epi_digipot_status": r[3], "epi_digipot": r[4]}


def _reads_off(st: Step) -> bool:
    return st.fields.get("bf_enable_readback", st.fields.get("epi_enable_readback")) == 0


@dataclass
class Step:
    name: str
    command: int
    value: int | None
    request_hex: str
    reply_hex: str | None = None
    ignored_reports_hex: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    problem: str = ""
    elapsed_ms: float | None = None


@dataclass
class LightResult:
    outcome: str  # "dry_run" | "refused" | "stopped" | "complete" | "off_failed"
    message: str
    started_utc: str
    host: str
    hold_s: float = HOLD_DEFAULT_S
    device: DeviceDescription | None = None
    app_start_record: str | None = None
    sent: bool = False
    steps: list[Step] = field(default_factory=list)
    lights_possibly_on: list[str] = field(default_factory=list)
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _exchange(ch, name: str, command: int, value: int | None, monotonic: Callable[[], float]) -> Step:
    req = build_request(command, value)
    st = Step(name, command, value, req.hex())
    t0 = monotonic()
    ch.flush_input()
    try:
        written = ch.write(req, IO_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - report, never resend
        st.problem = f"write did not complete: {exc}"
        st.elapsed_ms = (monotonic() - t0) * 1000
        return st
    if written != REPORT_LEN:
        st.problem = f"wrote {written} of {REPORT_LEN} bytes"
        st.elapsed_ms = (monotonic() - t0) * 1000
        return st
    want = command | 0x80
    for _ in range(MAX_READS):
        left = READ_WINDOW_S - (monotonic() - t0)
        if left <= 0:
            break
        try:
            r = ch.read(REPORT_LEN, min(IO_TIMEOUT_S, left))
        except Exception as exc:  # noqa: BLE001
            st.problem = f"no matching reply: {exc}"
            break
        if len(r) != REPORT_LEN or r[64] != frame_checksum(r):
            st.ignored_reports_hex.append(bytes(r).hex())
            continue
        if r[1] == REPLY_BUSY:
            st.reply_hex, st.problem = r.hex(), "application answered BUSY (0xF0)"
            break
        if r[1] == want:
            st.reply_hex, st.fields = r.hex(), decode(command, r)
            break
        st.ignored_reports_hex.append(r.hex())
    if st.reply_hex is None and not st.problem:
        st.problem = "no matching reply"
    st.elapsed_ms = (monotonic() - t0) * 1000
    return st


def light_test(backend: HidBackend, *, evidence_dir: Path, send: bool = False, confirm: str = "",
               checklist: str = "", hold_s: float = HOLD_DEFAULT_S, only: str | None = None,
               monotonic: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep,
               on_light: Callable[[str, float], None] | None = None) -> LightResult:
    res = LightResult("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      platform.node(), hold_s)
    if only not in (None, *ONLY_CHOICES):
        res.message = f"only must be one of {ONLY_CHOICES}; nothing sent"
        return res
    hold_max = HOLD_MAX_BRIGHTFIELD_ONLY_S if only == "brightfield" else HOLD_MAX_S
    if not 0 < hold_s <= hold_max:
        res.message = f"hold must be in (0, {hold_max:.0f}] s for this mode; nothing sent"
        return res
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
    lights = [("brightfield", CMD_BF_ENABLE), ("epi", CMD_EPI_ENABLE)]
    if only:
        lights = [x for x in lights if x[0] == only]
    if not send:
        res.outcome = "dry_run"
        res.message = ("dry run: would read 0x45, 0x4A; "
                       + "; ".join(f"{n} 0x{c:02X} on, hold {hold_s:g} s, off" for n, c in lights)
                       + "; read 0x45, 0x4A; nothing sent")
        return res
    if checklist != CHECKLIST_PHRASE:
        res.message = f"checklist phrase missing or wrong (expected {CHECKLIST_PHRASE!r}); nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    plan = [("read brightfield (before)", CMD_BF_STATUS, None), ("read epi (before)", CMD_EPI_STATUS, None)]
    for n, c in lights:
        plan += [(f"{n} ON", c, 1), ("hold", None, n), (f"{n} OFF", c, 0)]
    plan += [("read brightfield (after)", CMD_BF_STATUS, None), ("read epi (after)", CMD_EPI_STATUS, None)]
    off_for = {CMD_BF_ENABLE: "brightfield", CMD_EPI_ENABLE: "epi"}
    on: set[int] = set()  # lights that were sent ON (successfully or not) and not yet confirmed OFF
    failed = ""
    with backend.open(desc.path) as ch:
        res.sent = True
        for name, cmd, value in plan:
            if cmd is None:
                if on_light is not None:
                    on_light(value, hold_s)  # value carries the light's name: "<name> is on now"
                sleep(hold_s)
                continue
            st = _exchange(ch, name, cmd, value, monotonic)
            res.steps.append(st)
            if cmd in WRITE_COMMANDS:
                if value == 1:
                    on.add(cmd)
                elif not st.problem and _reads_off(st):
                    on.discard(cmd)
                elif not st.problem:
                    st.problem = f"read-back after OFF is not 0: {st.fields}"
            if st.problem:
                failed = f"{name}: {st.problem}"
                break
        if failed:  # switch off anything that may be on, once each
            for cmd in sorted(on):
                st = _exchange(ch, f"{off_for[cmd]} OFF (after failure)", cmd, 0, monotonic)
                res.steps.append(st)
                if not st.problem and _reads_off(st):
                    on.discard(cmd)
    res.lights_possibly_on = [off_for[c] for c in sorted(on)]
    if res.lights_possibly_on:
        res.outcome = "off_failed"
        res.message = (f"{failed}; OFF could not be confirmed for {', '.join(res.lights_possibly_on)}. "
                       "Power-cycle the instrument now; not resent")
    elif failed:
        res.outcome = "stopped"
        res.message = f"{failed}; lights confirmed off; not resent. Run again only with a new approval"
    else:
        res.outcome = "complete"
        names = " and ".join(n for n, _ in lights)
        res.message = (f"sequence complete; {names} switched on and confirmed off by read-back. "
                       "Report what the observer saw")
    return res


def write_evidence(result: LightResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m openevos.maintenance.led_light_test",
        description="LED Gate 4: brightfield and epi LED briefly on, then off, at the board's current "
                    "intensity settings. Dry run by default.")
    p.add_argument("--send", action="store_true", help="actually run the sequence once")
    p.add_argument("--hold", type=float, default=HOLD_DEFAULT_S,
                   help=f"seconds each light stays on (max {HOLD_MAX_S:g}; {HOLD_MAX_BRIGHTFIELD_ONLY_S:g} with "
                        "--only brightfield)")
    p.add_argument("--only", choices=ONLY_CHOICES, help="test one light only")
    p.add_argument("--checklist", default="", help=f"must be exactly: {CHECKLIST_PHRASE}")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans",
                   help="where the StartApp record is read from and this record is written (git-ignored)")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("This tool needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    def announce(name: str, hold: float) -> None:
        print(f"  {name} ON at {datetime.now(timezone.utc).isoformat(timespec='seconds')} for {hold:g} s",
              flush=True)

    res = light_test(backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm,
                     checklist=a.checklist, hold_s=a.hold, only=a.only, on_light=announce)
    print(f"[{res.outcome}] {res.message}")
    for st in res.steps:
        print(f"  {st.name}: {st.fields if not st.problem else st.problem}")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
