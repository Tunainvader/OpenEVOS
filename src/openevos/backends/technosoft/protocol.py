"""Read-only Technosoft binary protocol for live motor telemetry (ADR-0023).

Pure codec, no I/O. Verified on the instrument by Motor Gates 1-6 (ADR-0016..0021):
* request: len | dest(ID<<4, 2) | opcode(2) | sender 0x0011 | variable(2) | 8-bit sum
* reply:   'O' | len | dest | opcode | sender(axis<<4) | variable | value | sum over len..value
* 32-bit values: low word first, each word big-endian (vendor code; verified on hardware)
* line sync: FF -> 0D

Only three opcodes exist here: 0xB000 (gateway axis-ID query), 0xB004 / 0xB005 (16/32-bit variable
read). Nothing in this module can build a write. The maintenance package (`openevos.maintenance`) is
deliberately not imported (ADR-0011); the read path is small enough to carry its own tested copy.
"""

from __future__ import annotations

from dataclasses import dataclass

HOST = 0x0011
ACK = 0x4F
SYNC_BYTE, SYNC_REPLY = 0xFF, 0x0D
OP_READ16, OP_READ32 = 0xB004, 0xB005
ID_QUERY = bytes.fromhex("08 00 00 B0 00 00 01 00 00 B9")
INIT_DONE_MARKER = 12345
SRH_MOTION_COMPLETE_BIT = 1 << 9  # vendor XYStageXAxisIsMoving: moving while clear


class ProtocolRefused(RuntimeError):
    pass


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_read(axis_id: int, var_bits: int, address: int) -> bytes:
    if not 1 <= axis_id <= 6:
        raise ProtocolRefused(f"axis ID {axis_id} outside 1..6")
    if var_bits not in (16, 32):
        raise ProtocolRefused("only 16- or 32-bit reads")
    op = OP_READ16 if var_bits == 16 else OP_READ32
    body = bytes([0x08, 0x00, (axis_id << 4) & 0xFF, op >> 8, op & 0xFF, HOST >> 8, HOST & 0xFF,
                  address >> 8, address & 0xFF])
    return body + bytes([checksum(body)])


def reply_length(var_bits: int) -> int:
    return 13 if var_bits == 16 else 15


def parse_read_reply(raw: bytes, axis_id: int, address: int, var_bits: int) -> int:
    """Return the value (16-bit unsigned or 32-bit signed); raise ValueError on any defect."""
    n = reply_length(var_bits)
    if len(raw) != n:
        raise ValueError(f"reply {len(raw)} bytes, expected {n}")
    if raw[0] != ACK:
        raise ValueError(f"no acknowledgement (0x{raw[0]:02X})")
    if raw[1] != n - 3:
        raise ValueError("length byte mismatch")
    if checksum(raw[1:n - 1]) != raw[n - 1]:
        raise ValueError("checksum invalid")
    if ((raw[6] << 8) | raw[7]) != (axis_id << 4):
        raise ValueError("reply from another axis")
    if ((raw[8] << 8) | raw[9]) != address:
        raise ValueError("reply for another variable")
    v = raw[10:10 + var_bits // 8]
    if var_bits == 16:
        return int.from_bytes(v, "big")
    return int.from_bytes(v[2:4] + v[0:2], "big", signed=True)


def parse_id_reply(raw: bytes) -> int:
    if len(raw) < 9 or raw[0] != ACK or raw[1] != len(raw) - 3 or checksum(raw[1:-1]) != raw[-1] \
            or not raw[3] & 0x01:
        raise ValueError("axis-ID reply invalid")
    return ((raw[7] >> 4) | (raw[6] << 4)) & 0xFF


@dataclass(frozen=True)
class Var:
    axis: str
    axis_id: int
    name: str
    bits: int
    address: int

    @property
    def frame(self) -> bytes:
        return build_read(self.axis_id, self.bits, self.address)


AXIS_IDS = {"X": 1, "Y": 2, "P": 3, "Z": 4, "F": 5, "O": 6}
#: X/Y drive status for the stage drift monitor (ADR-0040). All read on the unit by Motor Gate 9 (ADR-0030) except
#: LCOMMERRPOS (the drive program's position at a comm-error abort; 32-bit, address from variables.cfg).
_XY_STATUS = {"SRL": 0x090E, "MER": 0x08FC, "ISTATUSFLAGS": 0x03B2, "BDOAXISOFF": 0x03D1, "IIQMAX": 0x03BA,
              "IIQMIN": 0x03BB, "IQ": 0x0230, "LCOMMERRPOS": 0x03BC,
              # power-up record (ADR-0040 Â§9): homing result and the hard-stop-to-index distance (read on the unit
              # by Gate 8 rev 6, 2026-11-16)
              "HOMEDONE": 0x03B0, "LHOMEINDEXDELTA": 0x03F4,
              # runaway watchdog, shadow mode (ADR-0047): the drive's own position error, signed 16-bit (read at
              # rest on the unit by Gate 9)
              "POSERR": 0x022A}
# per-axis addresses, from each axis' variables.cfg (verified by Motor Gates 1-2)
_ADDR = {
    "X": {"ESTATEV": 0x03EA, "IINITONCEDONE": 0x03FE, "LPOSMIN": 0x03B4, "LPOSMAX": 0x03B6, "CSPDCMD": 0x03D6,
          **_XY_STATUS},
    "Y": {"ESTATEV": 0x03EA, "IINITONCEDONE": 0x03FE, "LPOSMIN": 0x03B4, "LPOSMAX": 0x03B6, "CSPDCMD": 0x03D6,
          **_XY_STATUS},
    "Z": {"ESTATEV": 0x03B2, "IINITONCEDONE": 0x03F6, "LPOSMIN": 0x03BD, "LPOSMAX": 0x03BF,
          # focus moves (ADR-0053): read on the unit by Gate 17
          "LCONVERGEBAND": 0x03D3, "MER": 0x08FC},
    "F": {"ESTATEV": 0x03B6, "IINITONCEDONE": 0x03F8, "LPOSMIN": 0x03EA, "LPOSMAX": 0x03EC,
          # cube carriage moves (ADR-0051): F's flags, errors and stored positions, all read on the unit by Gate 9 rev 3
          # and used by Motor Gates 11/12
          "ISTATUSFLAGS": 0x03D2, "MER": 0x08FC, "LCONVERGEBAND": 0x03E1, "LCUBE1POS": 0x03EE, "LCUBE2POS": 0x03F0,
          "LCUBE3POS": 0x03F2, "LCUBE4POS": 0x03F4, "LMIRROR1POS": 0x03F6},
    # objective turret (ADR-0053): position sensor, target, flags, power stage; read on the unit by Gates 17/19
    "O": {"ESTATEV": 0x03B2, "ICURRENTPOSHF": 0x03CB, "IHF": 0x03CA, "ITARGETPOS": 0x03CC, "IPENDINGMOVES": 0x03DE,
          "ISTATUSFLAGS": 0x03E4, "SRL": 0x090E, "MER": 0x08FC},
    "P": {"ESTATEV": 0x03B2},
}
_COMMON = {"APOS": (32, 0x0228), "CPOS": (32, 0x029E), "SRH": (16, 0x090F)}


def axis_vars(axis: str) -> dict[str, Var]:
    aid = AXIS_IDS[axis]
    out = {name: Var(axis, aid, name, bits, addr) for name, (bits, addr) in _COMMON.items()}
    for name, addr in _ADDR[axis].items():
        out[name] = Var(axis, aid, name, 32 if name.startswith("L") or name == "CSPDCMD" else 16, addr)
    return out


def signed16(value: int) -> int:
    """16-bit reads come back unsigned; IQ, IIQMAX and IIQMIN are signed."""
    return value - 0x10000 if value & 0x8000 else value


VARS = {ax: axis_vars(ax) for ax in AXIS_IDS}
ALLOWED_REQUESTS = frozenset({bytes([SYNC_BYTE]), ID_QUERY, *(v.frame for vs in VARS.values() for v in vs.values())})


def assert_allowed(request: bytes) -> None:
    """The last check before every write to the port: only the sync byte, the ID query and the fixed reads."""
    if request not in ALLOWED_REQUESTS:
        raise ProtocolRefused(f"request {request.hex(' ')} is not a telemetry read")


# units: counts per mm (X/Y 2,880 counts per 1.27 mm; Z 5,000 counts per 0.61 mm); F/O/P stay in counts
COUNTS_PER_MM = {"X": 2880 / 1.27, "Y": 2880 / 1.27, "Z": 5000 / 0.61}


# ---------------------------------------------------------------------------------- motion (ADR-0024)
# The ONLY writes in the app: X/Y move target, the move request, and the controlled stop. Every frame
# below was verified on the instrument by Motor Gates 3-6. Each write is acknowledged by a single 'O'.
MOTION_AXES = {"X": 1, "Y": 2}
_CPOSCMD, _ESTATE, _IPENDING, _CACCSTOP = 0x03D4, 0x03B3, 0x03CD, 0x03E8  # same on X and Y
MOVE_STATE, STOP_STATE = 8, 6
#: hard bound for any target the builder will encode, in counts (about -2 mm .. +120 mm)
TARGET_BOUNDS = (-4600, 272500)


def _short_write(axis_id: int, op: int, payload: bytes) -> bytes:
    body = bytes([0x06 if len(payload) == 2 else 0x08, 0x00, axis_id << 4, op >> 8, op & 0xFF]) + payload
    return body + bytes([checksum(body)])


def _w16(axis: str, address: int, value: int) -> bytes:
    return _short_write(MOTION_AXES[axis], 0x2000 | (address & 0x1FF), value.to_bytes(2, "big"))


def _w32(axis: str, address: int, value: int) -> bytes:
    b = int(value).to_bytes(4, "big", signed=True)
    return _short_write(MOTION_AXES[axis], 0x2400 | (address & 0x1FF), b[2:4] + b[0:2])


def move_target(axis: str, counts: int) -> bytes:
    if axis not in MOTION_AXES:
        raise ProtocolRefused(f"no motion writes for axis {axis}")
    if not TARGET_BOUNDS[0] <= counts <= TARGET_BOUNDS[1]:
        raise ProtocolRefused(f"target {counts} counts outside the encodable bounds {TARGET_BOUNDS}")
    return _w32(axis, _CPOSCMD, counts)


def move_request(axis: str) -> bytes:
    if axis not in MOTION_AXES:
        raise ProtocolRefused(f"no motion writes for axis {axis}")
    return _w16(axis, _ESTATE, MOVE_STATE)


def stop_frames(axis: str) -> tuple[bytes, bytes, bytes]:
    """Vendor StopXAxis order: IPENDINGMOVES = 1, CACCSTOP = 1.0 (vendor constant), ESTATE = 6."""
    if axis not in MOTION_AXES:
        raise ProtocolRefused(f"no motion writes for axis {axis}")
    return (_w16(axis, _IPENDING, 1), _w32(axis, _CACCSTOP, 0x00010000), _w16(axis, _ESTATE, STOP_STATE))


# ---------------------------------------------------------------------------------- speed (ADR-0032)
# CSPDCMD (0x03D6, 16.16 fixed, counts per 0.8 ms sample) is loaded by the drive program at every move start
# (STATE_8_MOVE_POS: CSPD = CSPDCMD). Power-up value 15.0 = 8.27 mm/s (Gate 5). Only these three values exist;
# nothing above the power-up speed. RAM only (VAR_INIT restores 15.0 at power-on).
_CSPDCMD = 0x03D6
SPEED_SAMPLE_S = 0.0008
SPEED_PRESETS_MM_S = {"slow": 1.0, "medium": 4.0, "fast": 15.0 / SPEED_SAMPLE_S / (2880 / 1.27)}
POWER_UP_SPEED_RAW = 0x000F0000  # 15.0


def speed_raw(preset: str) -> int:
    if preset == "fast":
        return POWER_UP_SPEED_RAW
    return round(SPEED_PRESETS_MM_S[preset] * COUNTS_PER_MM["X"] * SPEED_SAMPLE_S * 65536)


def speed_write(axis: str, preset: str) -> bytes:
    if axis not in MOTION_AXES or preset not in SPEED_PRESETS_MM_S:
        raise ProtocolRefused(f"no speed write {preset!r} for axis {axis}")
    return _w32(axis, _CSPDCMD, speed_raw(preset))


# ---------------------------------------------------------------------------------- F cube carriage (ADR-0051)
# Only the frames Motor Gates 11 and 12 sent on the instrument: LTARGETPOS = slot n (1..4) + ESTATE = 16 (the drive's
# own GOTO_CUBE, which looks up its stored LCUBEnPOS), ESTATE = 17 (GOTO_MIRR: the stored LMIRROR1POS), and F's own
# STATE_15_STOP. The PC never sends a position to F.
F_ID = AXIS_IDS["F"]
_F_LTARGETPOS, _F_ESTATE = 0x03B7, 0x03B5
F_GOTO_CUBE, F_GOTO_MIRROR, F_STOP_STATE = 16, 17, 15
F_SLOTS = (1, 2, 3, 4)
#: the drive's stored positions as read on 2026-11-13 (Gates 9/11/12); a move is refused if the drive holds others
F_EXPECTED_POS = {"slot1": 0, "slot2": 34000, "slot3": 68000, "slot4": 102000, "mirror": 128000}
F_EXPECTED_LIMITS = (0, 128000)
F_CONVERGED = 0x4000  # ISTATUSFLAGS bit: the drive's convergence window passed


def _f_w16(value: int) -> bytes:
    return _short_write(F_ID, 0x2000 | (_F_ESTATE & 0x1FF), value.to_bytes(2, "big"))


def f_slot_target(slot: int) -> bytes:
    if slot not in F_SLOTS:
        raise ProtocolRefused(f"no F cube slot {slot}")
    b = slot.to_bytes(4, "big")
    return _short_write(F_ID, 0x2400 | (_F_LTARGETPOS & 0x1FF), b[2:4] + b[0:2])


F_GOTO_CUBE_FRAME = _f_w16(F_GOTO_CUBE)
F_GOTO_MIRROR_FRAME = _f_w16(F_GOTO_MIRROR)
F_STOP_FRAME = _f_w16(F_STOP_STATE)


def f_move_frames(dest: str) -> tuple[bytes, ...]:
    """'slot1'..'slot4' or 'mirror' -> the writes, in order."""
    if dest == "mirror":
        return (F_GOTO_MIRROR_FRAME,)
    if dest.startswith("slot") and dest[4:].isdigit() and int(dest[4:]) in F_SLOTS:
        return (f_slot_target(int(dest[4:])), F_GOTO_CUBE_FRAME)
    raise ProtocolRefused(f"no F destination {dest!r}")


_F_WRITES = frozenset({F_GOTO_CUBE_FRAME, F_GOTO_MIRROR_FRAME, F_STOP_FRAME, *(f_slot_target(n) for n in F_SLOTS)})

# ---------------------------------------------------------------------------------- Z focus (ADR-0053)
# Motor Gate 18's frames (run on the unit 2026-11-17: +1 mm and +5 mm and back, 0 counts error): CPOSCMD = target,
# ESTATE = 8 (STATE_8_MOVE_POS, clamped by the drive to LPOSMIN..LPOSMAX), ESTATE = 6 (STATE_6_STOP at rest).
# Speed and acceleration are never written. Positive Z moves the objective TOWARDS the stage (Gate 18).
Z_ID = AXIS_IDS["Z"]
_Z_CPOSCMD, _Z_ESTATE = 0x03B3, 0x03B1
#: what the app may command, in counts: home (0, fully retracted) up to the drive's LPOSMAX 67,000 less 1,000 margin
Z_TARGET_BOUNDS = (0, 66000)


def z_target(counts: int) -> bytes:
    if not Z_TARGET_BOUNDS[0] <= counts <= Z_TARGET_BOUNDS[1]:
        raise ProtocolRefused(f"Z target {counts} counts outside {Z_TARGET_BOUNDS}")
    b = int(counts).to_bytes(4, "big", signed=True)
    return _short_write(Z_ID, 0x2400 | (_Z_CPOSCMD & 0x1FF), b[2:4] + b[0:2])


Z_MOVE_FRAME = _short_write(Z_ID, 0x2000 | (_Z_ESTATE & 0x1FF), MOVE_STATE.to_bytes(2, "big"))
Z_STOP_FRAME = _short_write(Z_ID, 0x2000 | (_Z_ESTATE & 0x1FF), STOP_STATE.to_bytes(2, "big"))

# ---------------------------------------------------------------------------------- O objective turret (ADR-0053)
# Motor Gate 19 rev 2's frames (full 5-step cycle on the unit 2026-11-17, stage centred, Z at 0): ITARGETPOS = 1..5,
# ESTATE = 3 (STATE_3_GOTO_POS, with the drive's own stuck detection), and the vendor's AXISOFF as the stop. States 2
# and 4, and any other target (the drive would turn for ever), cannot be built.
O_ID = AXIS_IDS["O"]
_O_ITARGETPOS, _O_ESTATE = 0x03CC, 0x03B1
O_POSITIONS = (1, 2, 3, 4, 5)


def o_target(position: int) -> bytes:
    if position not in O_POSITIONS:
        raise ProtocolRefused(f"no turret position {position}")
    return _short_write(O_ID, 0x2000 | (_O_ITARGETPOS & 0x1FF), position.to_bytes(2, "big"))


O_GOTO_FRAME = _short_write(O_ID, 0x2000 | (_O_ESTATE & 0x1FF), (3).to_bytes(2, "big"))
O_AXISOFF_FRAME = bytes.fromhex("04 00 60 00 02 66")  # vendor HaltAllMotors, O
_ZO_WRITES = frozenset({Z_MOVE_FRAME, Z_STOP_FRAME, O_GOTO_FRAME, O_AXISOFF_FRAME, *(o_target(n) for n in O_POSITIONS)})

_FIXED_WRITES = frozenset({f for ax in MOTION_AXES for f in (move_request(ax), *stop_frames(ax),
                                                              *(speed_write(ax, p) for p in SPEED_PRESETS_MM_S))}
                          | _F_WRITES | _ZO_WRITES)


#: X/Y software re-home (Motor Gate 20, ADR-0058): ESTATE = 19, STATE_19_HOME_DOUBLE, the drive's power-up homing
#: routine and PearlScope's own XYStageHome frames (run on the unit 2026-11-19: X 17.7 s, Y 10.6 s). Not in
#: is_motion_write: a port accepts one only while the stage service registers it for one re-home (`extra_writes`).
HOME_STATE = 19
XY_HOME_FRAMES = {"X": bytes.fromhex("06 00 10 21 B3 00 13 FD"), "Y": bytes.fromhex("06 00 20 21 B3 00 13 0D")}
#: ISTATUSFLAGS bit 3: the drive's homing was interrupted (XAxis.sw _H30_INTERRUPTED_SETFLAG)
ISF_HOME_INTERRUPTED = 0x0008


def home_frame(axis: str) -> bytes:
    if axis not in XY_HOME_FRAMES:
        raise ProtocolRefused(f"no home request for axis {axis}")
    assert XY_HOME_FRAMES[axis] == _w16(axis, _ESTATE, HOME_STATE)
    return XY_HOME_FRAMES[axis]


#: X/Y position-limit writes (Motor Gate 7, ADR-0022/0056): LPOSMIN 25 B4, LPOSMAX 25 B6
LIMIT_OPS = {"LPOSMIN": 0x25B4, "LPOSMAX": 0x25B6}
POWER_UP_LIMITS = (0, 50000)


def limit_write(axis: str, var: str, value: int) -> bytes:
    """Gate 7's frame, byte for byte (tests compare it with motor_gate7_xy_limits.build_limit_write): X/Y LPOSMIN or
    LPOSMAX, 32-bit, vendor word order. Not in is_motion_write: a port accepts these only while the stage service
    registers exactly the validated calibration values (transport `extra_writes`)."""
    if axis not in ("X", "Y") or var not in LIMIT_OPS:
        raise ProtocolRefused(f"no limit write for {axis} {var}")
    op = LIMIT_OPS[var]
    b = int(value).to_bytes(4, "big", signed=True)
    body = bytes([0x08, 0x00, (1 if axis == "X" else 2) << 4, op >> 8, op & 0xFF]) + b[2:4] + b[0:2]
    return body + bytes([sum(body) & 0xFF])


def is_motion_write(frame: bytes) -> bool:
    """True only for the reviewed X/Y motion writes: a fixed request/stop frame, or a move target
    whose value is within TARGET_BOUNDS (rebuilt and compared byte for byte)."""
    frame = bytes(frame)
    if frame in _FIXED_WRITES:
        return True
    if len(frame) == 10 and frame[2] == Z_ID << 4 and frame[3:5] == bytes([0x25, _Z_CPOSCMD & 0xFF]):
        v = frame[5:9]
        counts = int.from_bytes(v[2:4] + v[0:2], "big", signed=True)
        try:
            return z_target(counts) == frame
        except ProtocolRefused:
            return False
    if len(frame) == 10 and frame[3:5] == bytes([0x25, _CPOSCMD & 0xFF]) and frame[2] in (0x10, 0x20):
        axis = "X" if frame[2] == 0x10 else "Y"
        v = frame[5:9]
        counts = int.from_bytes(v[2:4] + v[0:2], "big", signed=True)
        try:
            return move_target(axis, counts) == frame
        except ProtocolRefused:
            return False
    return False
