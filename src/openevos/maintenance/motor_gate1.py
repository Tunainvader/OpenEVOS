"""Motor Gate 1: read-only first contact with the Technosoft drives (ADR-0016; L3 reviewed read; spec rev 2).

Spec: docs/evidence/fl1_v1.6_motor_gate1_spec.md. Everything this tool can send is in FRAMES (and the
single sync byte): the gateway axis-ID query and 22 fixed variable reads (state, homing-done marker,
script version, position) for the six drives. Only opcodes 0xB000 / 0xB004 / 0xB005 (reads) can be
built; the vendor's baud change (0x0820) and axis-ID write (0x0801), and every write, motion, enable,
reset, EEPROM or TML_lib operation, are impossible here by construction.

Modes (dry run by default):
* --inspect-port: open exclusively, read back DCB + timeouts, close. No write, no SetCommState.
* --send: the rev 2 sequence, copying the vendor's FUN_180041560:
  - exactly one FTDI 0403:6001 port; the operator confirms >= 2 minutes on and types the phrase;
  - read back the DCB; refuse unless DTR/RTS control are ENABLE and no flow control is set;
  - baud pass 1 at 115,200, pass 2 at 9,600 (only the four line fields change; never a baud command);
  - every request preceded by its own line sync (FF -> 0D, <= 15 tries, 10 ms apart), then one write,
    50 ms, one read of up to 32 bytes; each frame written once; fail fast; stop if a drive is not idle;
  - git-ignored JSON evidence. Values are CANDIDATE.
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
from typing import Protocol

FTDI_VID, FTDI_PID = 0x0403, 0x6001
HOST = 0x0011
SYNC_BYTE, SYNC_REPLY, SYNC_ATTEMPTS = 0xFF, 0x0D, 15  # FUN_18002C430
BAUD_PASSES = (115200, 9600)  # the vendor's pass order (FUN_180041560)
SPEC_REVISION = 2
ACK = 0x4F  # 'O'
OP_ID_QUERY, OP_READ16, OP_READ32 = 0xB000, 0xB004, 0xB005
ALLOWED_OPCODES = frozenset({OP_ID_QUERY, OP_READ16, OP_READ32})
ID_QUERY = bytes.fromhex("08 00 00 B0 00 00 01 00 00 B9")
INIT_DONE_MARKER = 12345
MOTOR_FIRMWARE_INI = {"X": 29, "Y": 29, "Z": 21, "F": 29, "O": 7, "P": 6}
UNITS = {"X": (2880, 1.27), "Y": (2880, 1.27), "Z": (5000, 0.61)}  # counts/rev, default mm/rev
SPEC = "docs/evidence/fl1_v1.6_motor_gate1_spec.md; ADR-0016"

# (axis, ID, {variable: (address, bits)}) in spec order X, Y, P, Z, F, O
AXES = (
    ("X", 1, {"ESTATEV": (0x03EA, 16), "IINITONCEDONE": (0x03FE, 16), "IVERSION": (0x03EF, 16), "CPOS": (0x029E, 32)}),
    ("Y", 2, {"ESTATEV": (0x03EA, 16), "IINITONCEDONE": (0x03FE, 16), "IVERSION": (0x03EF, 16), "CPOS": (0x029E, 32)}),
    ("P", 3, {"ESTATEV": (0x03B2, 16), "IVERSION": (0x03E8, 16), "CPOS": (0x029E, 32)}),
    ("Z", 4, {"ESTATEV": (0x03B2, 16), "IINITONCEDONE": (0x03F6, 16), "IVERSION": (0x03E2, 16), "CPOS": (0x029E, 32)}),
    ("F", 5, {"ESTATEV": (0x03B6, 16), "IINITONCEDONE": (0x03F8, 16), "IVERSION": (0x03BC, 16), "CPOS": (0x029E, 32)}),
    ("O", 6, {"ESTATEV": (0x03B2, 16), "IVERSION": (0x03E8, 16), "CPOS": (0x029E, 32)}),
)


class MotorGate1Refused(RuntimeError):
    pass


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_read(axis_id: int, opcode: int, address: int) -> bytes:
    if opcode not in (OP_READ16, OP_READ32):
        raise MotorGate1Refused(f"opcode 0x{opcode:04X} is not a Gate 1 read")
    if not 1 <= axis_id <= 6:
        raise MotorGate1Refused(f"axis ID {axis_id} is outside 1..6")
    body = bytes([0x08, 0x00, (axis_id << 4) & 0xFF, opcode >> 8, opcode & 0xFF, HOST >> 8, HOST & 0xFF,
                  address >> 8, address & 0xFF])
    return body + bytes([checksum(body)])


def _frames() -> tuple[tuple[str, int, str, int, int, bytes], ...]:
    out = []
    for axis, aid, variables in AXES:
        for var, (addr, bits) in variables.items():
            out.append((axis, aid, var, addr, bits, build_read(aid, OP_READ32 if bits == 32 else OP_READ16, addr)))
    return tuple(out)


FRAMES = _frames()  # the only reads this tool can send
ALLOWED_REQUESTS = frozenset({ID_QUERY, *(f[5] for f in FRAMES)})


def assert_allowed(request: bytes) -> None:
    """Last line of defence before every write: only the fixed frames (or the sync byte) may go out."""
    if request == bytes([SYNC_BYTE]) or request in ALLOWED_REQUESTS:
        if len(request) > 1 and ((request[3] << 8) | request[4]) not in ALLOWED_OPCODES:
            raise MotorGate1Refused("opcode outside the allowlist")
        return
    raise MotorGate1Refused(f"request {request.hex(' ')} is not one of the reviewed Gate 1 frames")


# ---------------------------------------------------------------------------------- replies
def parse_reply(raw: bytes) -> tuple[bytes | None, str]:
    """Generic check: 'O', then length L, L bytes, checksum over bytes[1 : L+2] at index L+2.
    Returns (frame bytes after 'O', problem)."""
    if not raw:
        return None, "no reply"
    if raw[0] != ACK:
        return None, f"no acknowledgement (first byte 0x{raw[0]:02X})"
    if len(raw) < 4:
        return None, f"reply too short ({len(raw)} bytes)"
    L = raw[1]
    if len(raw) != L + 3:
        return None, f"length byte {L} does not match {len(raw)} received bytes"
    if checksum(raw[1:L + 2]) != raw[L + 2]:
        return None, "reply checksum invalid"
    return raw[1:], ""


def parse_id_reply(raw: bytes) -> tuple[int | None, str]:
    """As the vendor (FUN_18003F300): byte 3 bit 0 must be set; ID = (b7 >> 4) | (b6 << 4)."""
    frame, why = parse_reply(raw)
    if frame is None:
        return None, why
    if len(raw) < 8 or not raw[3] & 0x01:
        return None, "axis-ID reply has an unexpected layout"
    return ((raw[7] >> 4) | (raw[6] << 4)) & 0xFF, ""


def parse_read_reply(raw: bytes, axis_id: int, address: int, bits: int) -> tuple[bytes | None, str]:
    frame, why = parse_reply(raw)
    if frame is None:
        return None, why
    n = 2 if bits == 16 else 4
    if len(raw) != 11 + n:
        return None, f"{bits}-bit reply is {len(raw)} bytes, expected {11 + n}"
    if ((raw[6] << 8) | raw[7]) != (axis_id << 4):
        return None, f"reply sender 0x{(raw[6] << 8) | raw[7]:04X} is not axis {axis_id}"
    if ((raw[8] << 8) | raw[9]) != address:
        return None, f"reply variable 0x{(raw[8] << 8) | raw[9]:04X} != requested 0x{address:04X}"
    return raw[10:10 + n], ""


def decode(value: bytes, bits: int) -> dict:
    if bits == 16:
        return {"u16": int.from_bytes(value, "big"), "i16": int.from_bytes(value, "big", signed=True)}
    swapped = value[2:4] + value[0:2]
    return {"i32_big": int.from_bytes(value, "big", signed=True),
            "i32_word_swapped": int.from_bytes(swapped, "big", signed=True)}


# ---------------------------------------------------------------------------------- transport seam
class Port(Protocol):
    name: str

    def inspect(self) -> dict: ...
    def configure(self, baud: int) -> None: ...
    def purge_rx(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, max_bytes: int) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class PortInfo:
    device: str
    hwid: str
    serial_number: str | None
    location: str | None


def find_ftdi_ports() -> list[PortInfo]:
    from serial.tools import list_ports  # read-only enumeration; pyserial never opens the port here

    return [PortInfo(p.device, p.hwid, p.serial_number, p.location) for p in list_ports.comports()
            if p.vid == FTDI_VID and p.pid == FTDI_PID]


def open_vendor_port(device: str) -> Port:
    from openevos.maintenance.win_serial import WinSerialPort

    return WinSerialPort(device)


DTR_CONTROL_ENABLE = RTS_CONTROL_ENABLE = 1
FLOW_FLAGS = ("fOutxCtsFlow", "fOutxDsrFlow", "fDsrSensitivity", "fOutX", "fInX")


def driver_state_problems(state: dict) -> list[str]:
    """Spec §4 driver-state check on the read-back DCB. Empty list = the driver's normal state."""
    flags = state["dcb"]["flags"]
    problems = []
    if flags["fDtrControl"] != DTR_CONTROL_ENABLE:
        problems.append(f"fDtrControl = {flags['fDtrControl']} (expected 1, DTR_CONTROL_ENABLE)")
    if flags["fRtsControl"] != RTS_CONTROL_ENABLE:
        problems.append(f"fRtsControl = {flags['fRtsControl']} (expected 1, RTS_CONTROL_ENABLE)")
    problems += [f"{name} = {flags[name]} (flow control must be off)" for name in FLOW_FLAGS if flags[name]]
    return problems


# ---------------------------------------------------------------------------------- the run
@dataclass
class Exchange:
    what: str
    request_hex: str
    reply_hex: str | None = None
    value: dict | None = None
    problem: str = ""
    ms: float | None = None
    attempts: int | None = None


@dataclass
class MotorGate1Result:
    outcome: str  # "dry_run" | "refused" | "inspected" | "driver_state" | "stopped" | "not_idle" | "complete"
    message: str
    started_utc: str
    host: str
    spec_revision: int = SPEC_REVISION
    port: PortInfo | None = None
    port_state_read_back: dict | None = None
    driver_state_problems: list[str] = field(default_factory=list)
    baud: int | None = None
    bytes_written: int = 0
    gateway_axis_id: int | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    axes: dict[str, dict] = field(default_factory=dict)
    capability_source: str = "CANDIDATE"
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def confirm_phrase(device: str) -> str:
    return f"READ MOTOR DRIVES ON {device.upper()}"


def inspect_phrase(device: str) -> str:
    return f"INSPECT MOTOR PORT {device.upper()}"


def run(*, send: bool = False, inspect_only: bool = False, confirm: str = "", on_for_2_min: bool = False,
        find_ports: Callable[[], list[PortInfo]] = find_ftdi_ports,
        open_port: Callable[[str], Port] = open_vendor_port,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> MotorGate1Result:  # noqa: E501
    res = MotorGate1Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())  # noqa: E501
    if send and inspect_only:
        res.message = "--send and --inspect-port are separate steps; choose one. Nothing opened"
        return res
    ports = find_ports()
    if len(ports) != 1:
        res.message = f"expected exactly one FTDI 0403:6001 serial port, found {len(ports)}; nothing opened"
        return res
    res.port = ports[0]
    dev = ports[0].device
    if not send and not inspect_only:
        res.outcome = "dry_run"
        res.exchanges = [Exchange("axis-ID query", ID_QUERY.hex(" "))] + [
            Exchange(f"{axis} {var}", frame.hex(" ")) for axis, _aid, var, _addr, _bits, frame in FRAMES]
        res.message = (f"dry run: would open {dev}, read back its settings, refuse unless DTR/RTS are in the "
                       f"driver's normal state, sync at {BAUD_PASSES[0]} then {BAUD_PASSES[1]}, query the axis ID "
                       f"and send {len(FRAMES)} fixed reads, each once; nothing opened or sent")
        return res
    if inspect_only:
        if confirm != inspect_phrase(dev):
            res.message = f"confirmation phrase missing or wrong (expected {inspect_phrase(dev)!r}); nothing opened"
            return res
    else:
        if not on_for_2_min:
            res.message = "operator has not confirmed the instrument has been on for >= 2 minutes; nothing opened"
            return res
        if confirm != confirm_phrase(dev):
            res.message = f"confirmation phrase missing or wrong (expected {confirm_phrase(dev)!r}); nothing opened"
            return res

    try:
        port = open_port(dev)
    except Exception as exc:  # noqa: BLE001
        res.message = f"could not open {dev}: {exc}; nothing sent"
        return res
    try:
        res.port_state_read_back = port.inspect()
        res.driver_state_problems = driver_state_problems(res.port_state_read_back)
        if inspect_only:
            res.outcome = "inspected"
            res.message = ("port settings read back; nothing written. Driver state "
                           + ("normal (DTR/RTS enabled, no flow control)" if not res.driver_state_problems
                              else "NOT normal: " + "; ".join(res.driver_state_problems)))
            return res
        if res.driver_state_problems:
            res.outcome = "driver_state"
            res.message = ("driver state is not the vendor's normal state (" + "; ".join(res.driver_state_problems)
                           + "). Nothing sent. Re-plug the microscope's USB cable (or power-cycle the instrument and "
                           "wait 2 minutes), then inspect again")
            return res
        _exchange_all(port, res, monotonic, sleep)
    finally:
        port.close()
    return res


def _sync(port: Port, res: MotorGate1Result, label: str, monotonic, sleep) -> bool:
    """FUN_18002C430: write FF, read 1 byte; up to 15 tries, 10 ms apart. Recorded as one exchange."""
    ex = Exchange(label, f"{SYNC_BYTE:02x}", attempts=0)
    res.exchanges.append(ex)
    port.purge_rx()
    t0 = monotonic()
    for _ in range(SYNC_ATTEMPTS):
        assert_allowed(bytes([SYNC_BYTE]))
        ex.attempts += 1
        res.bytes_written += port.write(bytes([SYNC_BYTE]))
        got = port.read(1)
        if got == bytes([SYNC_REPLY]):
            ex.reply_hex = got.hex()
            ex.ms = round((monotonic() - t0) * 1000, 1)
            return True
        if got:
            ex.reply_hex = got.hex()
        sleep(0.010)
    ex.ms = round((monotonic() - t0) * 1000, 1)
    ex.problem = f"no 0D after {SYNC_ATTEMPTS} attempts"
    return False


def _request(port: Port, res: MotorGate1Result, request: bytes, what: str, monotonic, sleep) -> Exchange:
    """One write of a fixed frame, 50 ms, one read of up to 32 bytes (as the vendor). Never resent."""
    assert_allowed(request)
    ex = Exchange(what, request.hex(" "))
    res.exchanges.append(ex)
    t0 = monotonic()
    res.bytes_written += port.write(request)
    sleep(0.050)
    raw = port.read(32)
    ex.ms = round((monotonic() - t0) * 1000, 1)
    ex.reply_hex = raw.hex(" ") if raw else None
    if not raw:
        ex.problem = "no reply"
    return ex


def _exchange_all(port: Port, res: MotorGate1Result, monotonic, sleep) -> None:
    for baud in BAUD_PASSES:  # the pass sync is the axis-ID query's own sync, as in FUN_18003F300
        port.configure(baud)
        if _sync(port, res, f"line sync @ {baud}", monotonic, sleep):
            res.baud = baud
            break
    if res.baud is None:
        res.outcome = "stopped"
        res.message = f"no line sync at {' or '.join(map(str, BAUD_PASSES))} baud; no frame sent"
        return
    ex = _request(port, res, ID_QUERY, "axis-ID query", monotonic, sleep)
    gid, why = parse_id_reply(bytes.fromhex(ex.reply_hex)) if ex.reply_hex else (None, ex.problem)
    if gid is None:
        ex.problem = why
        res.outcome, res.message = "stopped", f"axis-ID query: {why}. Not retried"
        return
    res.gateway_axis_id = gid
    if gid != 1:
        res.outcome = "stopped"
        res.message = f"gateway reports axis ID {gid}, not 1; stopped (OpenEVOS never rewrites axis IDs)"
        return
    for axis, aid, var, addr, bits, frame in FRAMES:
        if not _sync(port, res, f"line sync before {axis} {var}", monotonic, sleep):
            res.outcome, res.message = "stopped", f"line sync lost before {axis} {var}; stopped"
            return
        ex = _request(port, res, frame, f"{axis} {var}", monotonic, sleep)
        value, why = parse_read_reply(bytes.fromhex(ex.reply_hex), aid, addr, bits) if ex.reply_hex else (None, ex.problem)  # noqa: E501
        if value is None:
            ex.problem = why
            res.outcome, res.message = "stopped", f"{axis} {var}: {why}. Not retried"
            return
        ex.value = decode(value, bits)
        res.axes.setdefault(axis, {"id": aid})[var] = ex.value
        if var == "ESTATEV" and ex.value["u16"] != 0:
            res.outcome, res.message = "not_idle", f"{axis} state is {ex.value['u16']}, not idle: wait for power-up homing to finish"  # noqa: E501
            return
        if var == "IINITONCEDONE" and ex.value["u16"] != INIT_DONE_MARKER:
            res.outcome, res.message = "not_idle", f"{axis} has not finished power-up initialisation (marker {ex.value['u16']})"  # noqa: E501
            return
    for axis, info in res.axes.items():
        info["ini_version"] = MOTOR_FIRMWARE_INI[axis]
        info["older_than_ini"] = info["IVERSION"]["u16"] < MOTOR_FIRMWARE_INI[axis]
        if axis in UNITS:
            cpr, mmrev = UNITS[axis]
            for order in ("i32_big", "i32_word_swapped"):
                info.setdefault("CPOS_mm_candidate", {})[order] = round(info["CPOS"][order] / cpr * mmrev, 4)
    res.outcome = "complete"
    res.message = (f"all {len(FRAMES)} reads answered at {res.baud} baud; drives idle and homed. Versions vs "
                   "MotorFirmware.ini: "
                   + ", ".join(f"{a} {i['IVERSION']['u16']}/{i['ini_version']}" for a, i in res.axes.items()))


def write_evidence(result: MotorGate1Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"motor-gate1-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, **seams) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.motor_gate1",
                                description="Motor Gate 1 (spec rev 2): read-only first contact with the Technosoft "
                                            "drives. Dry run by default.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--inspect-port", action="store_true",
                      help="open the port, read back its settings, close; nothing written (phrase: INSPECT MOTOR PORT COMn)")  # noqa: E501
    mode.add_argument("--send", action="store_true", help="run the full read sequence (phrase: READ MOTOR DRIVES ON COMn)")  # noqa: E501
    p.add_argument("--instrument-on-2min", action="store_true",
                   help="operator confirms the EVOS has been powered on for at least 2 minutes (--send only)")
    p.add_argument("--confirm", default="", help="the exact phrase for the chosen mode")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32" and not seams:
        print("Motor Gate 1 needs Windows.", file=sys.stderr)
        return 2
    res = run(send=a.send, inspect_only=a.inspect_port, confirm=a.confirm, on_for_2_min=a.instrument_on_2min, **seams)
    print(f"[{res.outcome}] {res.message}")
    if res.port:
        print(f"port: {res.port.device}  {res.port.hwid}")
    if res.port_state_read_back:
        d = res.port_state_read_back["dcb"]
        print(f"read back: {d['BaudRate']} baud, {d['ByteSize']} bits, parity {d['Parity']}, stop {d['StopBits']}, "
              f"flags {d['flags']}")
    if res.outcome == "dry_run":
        for ex in res.exchanges[:3]:
            print(f"  {ex.what:22s} {ex.request_hex}")
        print(f"  ... {len(res.exchanges) - 3} more fixed reads")
    if res.port_state_read_back or res.outcome == "dry_run":
        print(f"evidence (git-ignored): {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "inspected", "complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
