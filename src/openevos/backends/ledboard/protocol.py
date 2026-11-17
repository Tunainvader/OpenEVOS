"""LED board application protocol for the app (ADR-0048): the only frames the app can build.

Layouts come from the reviewed LED gates (spec docs/evidence/fl1_v1.6_led_app_command_table.md §4, §6;
ADR-0031, ADR-0034). This module is independent of `openevos.maintenance` (ADR-0011: the app never imports the
maintenance tools); tests/test_ledboard_illumination.py checks every frame here is byte-identical to the
reviewed tools' frames.

65-byte HID reports: byte 0 = report ID 0, byte 1 = command, byte 64 = two's complement of the 8-bit sum of
bytes 1–63. The reply echoes `command | 0x80`; `0xF0` = busy.
"""

from __future__ import annotations

REPORT_LEN = 65
VID = 0x0DAD
PID_APPLICATION = 0x0051
PID_APPLICATION_ALT = 0x0052
PID_BOOTLOADER = 0x0057
REPLY_BUSY = 0xF0

CMD_BF_ENABLE = 0x43  # brightfield PWM enable, BE16 value at bytes 2–3
CMD_BF_STATUS = 0x45
CMD_POSITIONS = 0x47  # cube / objective positions (read-only)
CMD_EPI_ENABLE = 0x49  # epi enable, byte 2
CMD_EPI_STATUS = 0x4A
CMD_EPI_LEVEL = 0x4C  # epi level, byte 2 = 0..31; read back via 0x4A byte 4 (verified, spec §7.2b)
CMD_PEARL_GET = 0x5D  # PEARL-mode status: brightfield level at byte 35, range 36/37 (spec §7)
CMD_PEARL_SET = 0x5E  # PEARL-mode field write: ONLY field mask 0x04 (brightfield level, byte 19) may be built
PEARL_FIELD_BF_LEVEL = 0x04
LEVEL_MIN, LEVEL_MAX = 0, 31
WRITE_COMMANDS = frozenset({CMD_BF_ENABLE, CMD_EPI_ENABLE})
LEVEL_COMMANDS = frozenset({CMD_PEARL_SET, CMD_EPI_LEVEL})
READ_COMMANDS = frozenset({CMD_BF_STATUS, CMD_POSITIONS, CMD_EPI_STATUS, CMD_PEARL_GET})
ALLOWED_COMMANDS = WRITE_COMMANDS | LEVEL_COMMANDS | READ_COMMANDS

# Gen3FilterCubeType (DLL type library, spec §4b)
CUBE_TYPES = {0: "SHORT", 1: "NONE", 2: "DISENGAGED", 3: "DAPI", 4: "GFP", 5: "RFP", 6: "CFP", 7: "YFP",
              8: "TEXAS_RED", 9: "CY5", 20: "MCHERRY", 22: "FITC", 23: "BFP", 50: "WHITE"}


class ProtocolRefused(ValueError):
    """The frame is not in the app's allowlist; nothing is built."""


def frame_checksum(report: bytes) -> int:
    return (-sum(report[1:64])) & 0xFF


def build_request(command: int, value: int | None = None) -> bytes:
    """Writes: 0x43 / 0x49 with value 0 or 1 only. Reads: 0x45 / 0x47 / 0x4A without a value. Nothing else."""
    if command not in ALLOWED_COMMANDS:
        raise ProtocolRefused(f"command 0x{command:02X} is not allowed in the app")
    body = bytearray(REPORT_LEN)
    body[1] = command
    if command in WRITE_COMMANDS:
        if value not in (0, 1):
            raise ProtocolRefused(f"0x{command:02X} accepts only 0 or 1, not {value!r}")
        if command == CMD_BF_ENABLE:
            body[3] = value  # BE16 0/1 at bytes 2–3
        else:
            body[2] = value
    elif command in LEVEL_COMMANDS:
        if not isinstance(value, int) or not LEVEL_MIN <= value <= LEVEL_MAX:
            raise ProtocolRefused(f"level must be {LEVEL_MIN}..{LEVEL_MAX}, not {value!r}")
        if command == CMD_PEARL_SET:
            body[2], body[19] = PEARL_FIELD_BF_LEVEL, value  # never any other 0x5E field (system mode, recache, ...)
        else:
            body[2] = value
    elif value is not None:
        raise ProtocolRefused(f"read 0x{command:02X} takes no value")
    body[64] = frame_checksum(bytes(body))
    return bytes(body)


def reply_ok(command: int, reply: bytes) -> bool:
    return len(reply) == REPORT_LEN and reply[64] == frame_checksum(reply) and reply[1] == (command | 0x80)


def enable_readback(command: int, reply: bytes) -> int:
    """0x43 reply: BE16 at bytes 2–3; 0x49 reply: byte 2 (firmware handlers, spec §6)."""
    if command == CMD_BF_ENABLE:
        return (reply[2] << 8) | reply[3]
    if command == CMD_EPI_ENABLE:
        return reply[2]
    raise ProtocolRefused(f"0x{command:02X} has no enable read-back")


def brightfield_level(reply: bytes) -> tuple[int, tuple[int, int]]:
    """0x5D reply: (level at byte 35, (range min byte 36, max byte 37)) — verified on the board (spec §7.2a)."""
    return reply[35], (reply[36], reply[37])


def epi_level(reply: bytes) -> tuple[int, tuple[int, int]]:
    """0x4A reply: (epi level at byte 4, (range bytes 5, 6)) — verified on the board (spec §7.2b)."""
    return reply[4], (reply[5], reply[6])


def cube_in_path(reply: bytes) -> str | None:
    """0x47 byte 2: Gen3FilterCubeType of the cube in the light path (handler writes bytes 2–34, spec §4c)."""
    return CUBE_TYPES.get(reply[2])
