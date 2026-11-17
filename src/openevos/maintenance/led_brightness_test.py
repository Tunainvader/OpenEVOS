"""LED Gate 5: one light at a LOW level briefly, then off and the original level restored (ADR-0049, RAM/I2C).

Spec: docs/evidence/fl1_v1.6_led_app_command_table.md §7 (PEARL-mode brightness, system mode 3/4).

Requests this tool can build, and nothing else:
* `0x5D` read: brightfield level = reply byte 35, range bytes 36/37.            (read-only)
* `0x4A` read: epi level = reply byte 4, range bytes 5/6.                       (read-only)
* `0x47` read: cube type in the light path = reply byte 2.                      (read-only)
* `0x5E` with byte 2 = `0x04` ONLY (the brightfield-level field) and byte 19 = level 0–31. Every other bit of
  the `0x5E` field mask (system mode, current mode, prism zero point, cube recache) is impossible to build.
* `0x4C` byte 2 = epi level 0–31.
* `0x43` / `0x49` value 0/1: brightfield / epi on-off (as LED Gate 4, ADR-0034).

Sequence for the chosen light (`--light brightfield|epi`), each request once, never resent:
  read level (and, for epi, the cube in the path: refused if NONE/DISENGAGED or DAPI) ->
  set level L -> read back == L -> ON -> hold -> OFF -> set the ORIGINAL level -> read back == original.
On any failure: OFF (confirmed by read-back) if the light may be on, then the original level is restored once.
The level chip's write may be non-volatile (Unknown, spec §7.2): the original level is always put back, and the
power-on level should be re-read after the next power cycle.
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
from openevos.maintenance.led_status_read import CUBE_TYPES, PID_APPLICATION, PID_APPLICATION_ALT

CMD_BF_ENABLE, CMD_EPI_ENABLE = 0x43, 0x49
CMD_EPI_STATUS, CMD_POSITIONS = 0x4A, 0x47
CMD_EPI_LEVEL = 0x4C
CMD_PEARL_GET, CMD_PEARL_SET = 0x5D, 0x5E
PEARL_FIELD_BF_LEVEL = 0x04  # the only 0x5E field this tool may set
READS = frozenset({CMD_EPI_STATUS, CMD_POSITIONS, CMD_PEARL_GET})
ALLOWED_COMMANDS = READS | {CMD_BF_ENABLE, CMD_EPI_ENABLE, CMD_EPI_LEVEL, CMD_PEARL_SET}
LEVEL_MIN, LEVEL_MAX = 0, 31
LEVEL_DEFAULT = 2
HOLD_DEFAULT_S, HOLD_MAX_S = 3.0, 10.0
EPI_REFUSED_CUBES = frozenset({"NONE", "DISENGAGED", "DAPI"})  # empty slot, or near-UV first: not in Gate 5
REPLY_BUSY = 0xF0
IO_TIMEOUT_S, READ_WINDOW_S, MAX_READS = 0.5, 1.0, 5
CONFIRM_PHRASE = "BRIGHTNESS TEST LOW LEVEL ON 0DAD:0051"
CHECKLIST_PHRASE = "NOBODY LOOKING INTO THE LIGHT PATH, OBSERVER PRESENT"
SPEC = "docs/evidence/fl1_v1.6_led_app_command_table.md §7; ADR-0049"
EVIDENCE_PREFIX = "led-application-gate5-brightness"


class BrightnessRefused(RuntimeError):
    """Not sent: a precondition failed or the frame is not allowed. Nothing reached the device."""


def build_request(command: int, value: int | None = None) -> bytes:
    if command not in ALLOWED_COMMANDS:
        raise BrightnessRefused(f"command 0x{command:02X} is not in the Gate 5 set; nothing sent")
    body = bytearray(REPORT_LEN)
    body[1] = command
    if command in READS:
        if value is not None:
            raise BrightnessRefused(f"read 0x{command:02X} takes no value; nothing sent")
    elif command in (CMD_BF_ENABLE, CMD_EPI_ENABLE):
        if value not in (0, 1):
            raise BrightnessRefused(f"0x{command:02X} accepts only 0 or 1; nothing sent")
        if command == CMD_BF_ENABLE:
            body[3] = value
        else:
            body[2] = value
    else:
        if not isinstance(value, int) or not LEVEL_MIN <= value <= LEVEL_MAX:
            raise BrightnessRefused(f"level must be {LEVEL_MIN}..{LEVEL_MAX}, not {value!r}; nothing sent")
        if command == CMD_PEARL_SET:
            body[2], body[19] = PEARL_FIELD_BF_LEVEL, value  # field mask: brightfield level only
        else:
            body[2] = value
    body[64] = frame_checksum(bytes(body))
    return bytes(body)


def decode(command: int, r: bytes) -> dict:
    """Byte offsets from the vendor readers (spec §7.1). CANDIDATE."""
    if command == CMD_PEARL_GET:
        return {"bf_level": r[35], "bf_range": [r[36], r[37]]}
    if command == CMD_EPI_STATUS:
        return {"epi_enable": r[2], "epi_level": r[4], "epi_range": [r[5], r[6]]}
    if command == CMD_POSITIONS:
        return {"cube_in_path": CUBE_TYPES.get(r[2], f"type {r[2]}")}
    if command == CMD_BF_ENABLE:
        return {"readback": (r[2] << 8) | r[3]}
    if command == CMD_EPI_ENABLE:
        return {"readback": r[2]}
    return {}


@dataclass
class Step:
    name: str
    request_hex: str
    reply_hex: str | None = None
    fields: dict = field(default_factory=dict)
    problem: str = ""


@dataclass
class BrightnessResult:
    outcome: str  # dry_run | refused | stopped | complete | off_failed | restore_failed
    message: str
    started_utc: str
    host: str
    light: str = ""
    level: int = LEVEL_DEFAULT
    hold_s: float = HOLD_DEFAULT_S
    original_level: int | None = None
    device: DeviceDescription | None = None
    app_start_record: str | None = None
    sent: bool = False
    steps: list[Step] = field(default_factory=list)
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


class _Failed(Exception):
    pass


def _exchange(ch, res: BrightnessResult, name: str, command: int, value: int | None,
              monotonic: Callable[[], float]) -> dict:
    req = build_request(command, value)
    st = Step(name, req.hex())
    res.steps.append(st)
    t0 = monotonic()
    ch.flush_input()
    try:
        written = ch.write(req, IO_TIMEOUT_S)
    except Exception as exc:
        st.problem = f"write did not complete: {exc}"
        raise _Failed(f"{name}: {st.problem}") from exc
    if written != REPORT_LEN:
        st.problem = f"wrote {written} of {REPORT_LEN} bytes"
        raise _Failed(f"{name}: {st.problem}")
    want = command | 0x80
    for _ in range(MAX_READS):
        left = READ_WINDOW_S - (monotonic() - t0)
        if left <= 0:
            break
        try:
            r = bytes(ch.read(REPORT_LEN, min(IO_TIMEOUT_S, left)))
        except Exception as exc:
            st.problem = f"no matching reply: {exc}"
            raise _Failed(f"{name}: {st.problem}") from exc
        if len(r) != REPORT_LEN or r[64] != frame_checksum(r):
            continue
        if r[1] == REPLY_BUSY:
            st.reply_hex, st.problem = r.hex(), "BUSY"
            raise _Failed(f"{name}: board answered BUSY; not resent")
        if r[1] == want:
            st.reply_hex, st.fields = r.hex(), decode(command, r)
            return st.fields
    st.problem = "no matching reply"
    raise _Failed(f"{name}: no matching reply; not resent")


def brightness_test(backend: HidBackend, *, evidence_dir: Path, light: str, level: int = LEVEL_DEFAULT,
                    send: bool = False, confirm: str = "", checklist: str = "", hold_s: float = HOLD_DEFAULT_S,
                    monotonic: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> BrightnessResult:
    res = BrightnessResult("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           platform.node(), light, level, hold_s)
    if light not in ("brightfield", "epi"):
        res.message = "light must be 'brightfield' or 'epi'; nothing sent"
        return res
    if not LEVEL_MIN <= level <= LEVEL_MAX:
        res.message = f"level must be {LEVEL_MIN}..{LEVEL_MAX}; nothing sent"
        return res
    if not 0 < hold_s <= HOLD_MAX_S:
        res.message = f"hold must be in (0, {HOLD_MAX_S:g}] s; nothing sent"
        return res
    apps = backend.find(VID, PID_APPLICATION)
    others = [k for k, pid in (("0DAD:0057", PID_BOOTLOADER), ("0DAD:0052", PID_APPLICATION_ALT))
              if backend.find(VID, pid)]
    if len(apps) != 1:
        res.message = f"expected exactly one present 0DAD:0051 interface, found {len(apps)}; nothing sent"
        return res
    if others:
        res.message = f"unexpected interface(s) present ({', '.join(others)}); nothing sent"
        return res
    desc = backend.describe(apps[0])
    res.device = desc
    if (desc.vendor_id, desc.product_id, desc.input_report_len, desc.output_report_len) != \
            (VID, PID_APPLICATION, REPORT_LEN, REPORT_LEN):
        res.message = "driver does not report 0DAD:0051 with 65/65-byte reports; nothing sent"
        return res
    rec, problem = find_app_start_record(evidence_dir, desc.path)
    if rec is None:
        res.message = problem + "; nothing sent"
        return res
    res.app_start_record = rec.name
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would read the {light} level, set {level}/31, switch {light} on for {hold_s:g} s, "
                       "off, restore the original level and read it back; nothing sent")
        return res
    if checklist != CHECKLIST_PHRASE:
        res.message = f"checklist phrase missing or wrong (expected {CHECKLIST_PHRASE!r}); nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    bf = light == "brightfield"
    get_cmd, level_key = (CMD_PEARL_GET, "bf_level") if bf else (CMD_EPI_STATUS, "epi_level")
    set_cmd = CMD_PEARL_SET if bf else CMD_EPI_LEVEL
    en_cmd = CMD_BF_ENABLE if bf else CMD_EPI_ENABLE
    maybe_on = level_changed = False
    failed = ""
    with backend.open(desc.path) as ch:
        res.sent = True

        def x(name, cmd, value=None):
            return _exchange(ch, res, name, cmd, value, monotonic)

        try:
            before = x(f"read {light} level", get_cmd)
            orig = before[level_key]
            rng = before["bf_range" if bf else "epi_range"]
            if not LEVEL_MIN <= orig <= LEVEL_MAX or rng != [LEVEL_MIN, LEVEL_MAX]:
                raise _Failed(f"unexpected level {orig} / range {rng}; expected 0..31 (layout not confirmed)")
            res.original_level = orig
            if not bf:
                cube = x("read cube in path", CMD_POSITIONS)["cube_in_path"]
                if cube in EPI_REFUSED_CUBES or cube.startswith("type "):
                    raise _Failed(f"cube in the light path is {cube}; Gate 5 epi needs RFP or GFP (no DAPI, no empty)")
            level_changed = True
            x(f"set {light} level {level}", set_cmd, level)
            got = x(f"read {light} level (after set)", get_cmd)[level_key]
            if got != level:
                raise _Failed(f"level read back {got}, commanded {level}")
            maybe_on = True
            if x(f"{light} ON", en_cmd, 1)["readback"] != 1:
                raise _Failed(f"{light} ON did not read back 1")
            sleep(hold_s)
            if x(f"{light} OFF", en_cmd, 0)["readback"] != 0:
                raise _Failed(f"{light} OFF did not read back 0")
            maybe_on = False
            x(f"restore {light} level {orig}", set_cmd, orig)
            if x(f"read {light} level (after restore)", get_cmd)[level_key] != orig:
                raise _Failed("restored level did not read back")
            level_changed = False
        except _Failed as exc:
            failed = str(exc)
        if failed and maybe_on:
            try:
                if x(f"{light} OFF (after failure)", en_cmd, 0)["readback"] == 0:
                    maybe_on = False
            except _Failed:
                pass
        if failed and level_changed and res.original_level is not None:
            try:
                x(f"restore {light} level (after failure)", set_cmd, res.original_level)
                if x(f"read {light} level (after failure restore)", get_cmd)[level_key] == res.original_level:
                    level_changed = False
            except _Failed:
                pass
    if maybe_on:
        res.outcome = "off_failed"
        res.message = f"{failed}; OFF could not be confirmed. Power-cycle the instrument now; not resent"
    elif failed and level_changed and res.original_level is not None:
        res.outcome = "restore_failed"
        res.message = (f"{failed}; light confirmed off, but the original level {res.original_level} could not be "
                       "confirmed restored. Power-cycle the instrument; not resent")
    elif failed:
        res.outcome = "stopped"
        res.message = f"{failed}; light off, level unchanged or restored; not resent"
    else:
        res.outcome = "complete"
        res.message = (f"{light} was on at level {level}/31 for {hold_s:g} s; off confirmed; original level "
                       f"{res.original_level} restored and read back. Report what the observer saw")
    return res


def write_evidence(result: BrightnessResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, backend: HidBackend | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.led_brightness_test",
                                description="LED Gate 5: one light at a low level briefly, then off and the "
                                            "original level restored. Dry run by default.")
    p.add_argument("--light", choices=("brightfield", "epi"), required=True)
    p.add_argument("--level", type=int, default=LEVEL_DEFAULT, help=f"{LEVEL_MIN}..{LEVEL_MAX} (default {LEVEL_DEFAULT})")
    p.add_argument("--hold", type=float, default=HOLD_DEFAULT_S, help=f"seconds on (max {HOLD_MAX_S:g})")
    p.add_argument("--send", action="store_true")
    p.add_argument("--checklist", default="", help=f"must be exactly: {CHECKLIST_PHRASE}")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if backend is None:
        if sys.platform != "win32":
            print("This tool needs Windows (HID via SetupAPI).", file=sys.stderr)
            return 2
        backend = WindowsHid()
    res = brightness_test(backend, evidence_dir=a.evidence_dir, light=a.light, level=a.level, send=a.send,
                          confirm=a.confirm, checklist=a.checklist, hold_s=a.hold)
    print(f"[{res.outcome}] {res.message}")
    for st in res.steps:
        print(f"  {st.name}: {st.fields if not st.problem else st.problem}")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
