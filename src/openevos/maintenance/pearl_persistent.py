"""Parse PearlScope's decoded persistent-data stream (the LED board's section-2 blob, ADR-0015).

Schema: docs/evidence/fl1_v1.6_pearl_persistent_schema.md, derived from the decompiled readers in
`PearlScope64.exe` (CHardwarePersistentDataController::DoReadHardwarePersistentData and the
controllers it calls). The stream is positional: fixed-size little-endian fields and strings
(int32 character count + UTF-16LE), in reader order, with no tags. Each section starts with a
4-byte marker and a version double that must be 1.0:

    coca  uEye colour camera      Red/Green/BlueGain (double)
    moca  PGR mono camera         int32 n, n x (uint16, uint16)
    mcba  motor controller base   23 settings of IEvosGen3MotorController: X/Y/Z/F position limits,
                                  X/Y home torque, filter-cube carriage positions 1-5, speeds and
                                  accelerations, turret/phase-wheel direction
    moco  motor controller        X/Y/Z AxisOffset, X/Y/Z mm-per-revolution, X/Y AxisRegistration
    tpco  turret controller       8 objective slots x strings, then 5 x (slot, objective index)

Turret slot strings: objective list; per camera (colour, mono) -Calibrations, -Parfocalities,
-Parcentrations; then -PhaseWheel, -NA, -Immersion and, only in the PearlScope 1.6 format, -APO.
The parser accepts a layout only if every marker and version matches and the stream ends exactly
at the last byte. Values are CANDIDATE data: this module never claims what they mean physically.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = 1.0
CAMERAS = ("UI146xLE-C Color Camera", "Sony ICX445AL (1/3\" 1296x964 CCD) Camera")
PER_CAMERA_KEYS = ("-Calibrations", "-Parfocalities", "-Parcentrations")
TAIL_KEYS_PRE16 = ("-PhaseWheel", "-NA", "-Immersion")
TAIL_KEYS_16 = (*TAIL_KEYS_PRE16, "-APO")
TURRET_SLOTS = 8  # reader loop 1..8
TURRET_POSITIONS = 5  # reader loop 1..5
# Field names come from where each value goes (schema doc): mcba values are passed to methods of the
# motor controller's COM interface IEvosGen3MotorController (MotorController64.dll type library);
# moco and coca values to PearlScope setters that write the named PearlScope.ini keys.
MCBA_FIELDS = (
    ("i", "XAxisPositionLimits.lMin"), ("i", "XAxisPositionLimits.lMax"), ("i", "XAxisHomeTorqueLevel"),
    ("i", "YAxisPositionLimits.lMin"), ("i", "YAxisPositionLimits.lMax"), ("i", "YAxisHomeTorqueLevel"),
    ("i", "ZAxisPositionLimits.lMin"), ("i", "ZAxisPositionLimits.lMax"),
    ("i", "FAxisPositionLimits.lMin"), ("i", "FAxisPositionLimits.lMax"),
    ("d", "FilterCubePositioningSpeed"), ("d", "FilterCubePositioningAcceleration"),
    ("i", "FAxisLocationMotorPosition[1]"), ("i", "FAxisLocationMotorPosition[2]"),
    ("i", "FAxisLocationMotorPosition[3]"), ("i", "FAxisLocationMotorPosition[4]"),
    ("i", "FAxisLocationMotorPosition[5]"),
    ("d", "ObjectiveTurretPositioningSpeed"), ("d", "ObjectiveTurretPositioningAcceleration"),
    ("i", "ObjectiveTurretClockwise"),
    ("d", "PhaseWheelPositioningSpeed"), ("d", "PhaseWheelPositioningAcceleration"),
    ("i", "PhaseTurretClockwise"),
)
MOCO_FIELDS = (("i", "XAxisOffset"), ("i", "YAxisOffset"), ("i", "ZAxisOffset"),
               ("d", "XAxisMillimetersPerRevolution"), ("d", "YAxisMillimetersPerRevolution"),
               ("d", "ZAxisMillimetersPerRevolution"), ("i", "XAxisRegistration"), ("i", "YAxisRegistration"))
COCA_FIELDS = ("RedGain", "GreenGain", "BlueGain")


class ParseError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.d):
            raise ParseError(f"stream ends at {len(self.d)}; need {n} bytes at {self.pos}")
        b = self.d[self.pos:self.pos + n]
        self.pos += n
        return b

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def string(self) -> str:
        at = self.pos
        n = self.i32()
        if not 0 <= n <= 4096:
            raise ParseError(f"implausible string length {n} at {at}")
        return self.take(2 * n).decode("utf-16le")

    def section(self, marker: bytes) -> None:
        at = self.pos
        got = self.take(4)
        if got != marker:
            raise ParseError(f"expected section {marker.decode()!r} at {at}, found {got!r}")
        if self.f64() != VERSION:
            raise ParseError(f"section {marker.decode()!r} version is not 1.0")


@dataclass
class TurretSlot:
    index: int
    objectives: str
    per_camera: dict[str, dict[str, str]]
    tail: dict[str, str]


@dataclass
class PersistentData:
    layout: str  # "pre-1.6 (no -APO)" | "1.6"
    length: int
    uEye_color_gains: dict[str, float]  # RedGain / GreenGain / BlueGain
    pgr_pairs: list[tuple[int, int]]  # meaning not yet named (empty on our unit)
    motor_base: dict[str, float | int]  # IEvosGen3MotorController settings (limits, torques, speeds)
    motor: dict[str, float | int]  # [PearlScopeMotorController] offsets, mm/rev, registration
    turret_slots: list[TurretSlot] = field(default_factory=list)
    # (turret position, objective slot, index of the objective within that slot's list):
    # PearlScope writes "<position>-Selection = <slot>-<index>" under [ObjectiveLenses]
    turret_positions: list[tuple[int, int, int]] = field(default_factory=list)
    capability_source: str = "CANDIDATE"


def _parse(data: bytes, tail_keys: tuple[str, ...], layout: str) -> PersistentData:
    r = _Reader(data)
    r.section(b"coca")
    ueye = {name: r.f64() for name in COCA_FIELDS}
    r.section(b"moca")
    n = r.i32()
    if not 0 <= n <= 100_000:
        raise ParseError(f"implausible PGR pair count {n}")
    pairs = [(r.u16(), r.u16()) for _ in range(n)]
    r.section(b"mcba")
    base = {name: (r.f64() if t == "d" else r.i32()) for t, name in MCBA_FIELDS}
    r.section(b"moco")
    motor = {name: (r.f64() if t == "d" else r.i32()) for t, name in MOCO_FIELDS}
    r.section(b"tpco")
    slots = []
    for k in range(1, TURRET_SLOTS + 1):
        objectives = r.string()
        per_camera = {cam: {key: r.string() for key in PER_CAMERA_KEYS} for cam in CAMERAS}
        tail = {key: r.string() for key in tail_keys}
        slots.append(TurretSlot(k, objectives, per_camera, tail))
    positions = [(p, r.i32(), r.i32()) for p in range(1, TURRET_POSITIONS + 1)]
    if r.pos != len(data):
        raise ParseError(f"{len(data) - r.pos} bytes left over after the turret section")
    return PersistentData(layout, len(data), ueye, pairs, base, motor, slots, positions)


def parse(data: bytes) -> PersistentData:
    """Try the 1.6 layout, then the older one without -APO; accept only an exact, full parse."""
    errors = []
    for keys, layout in ((TAIL_KEYS_16, "1.6"), (TAIL_KEYS_PRE16, "pre-1.6 (no -APO)")):
        try:
            return _parse(data, keys, layout)
        except ParseError as exc:
            errors.append(f"{layout}: {exc}")
    raise ParseError("no known layout parses this stream exactly: " + " | ".join(errors))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.pearl_persistent",
                                description="Parse a decoded PearlScope persistent-data stream (offline).")
    p.add_argument("decoded", type=Path, help="the -decoded.bin file written by led_section2_read")
    p.add_argument("--out", type=Path, help="write the parsed JSON here (keep it git-ignored)")
    a = p.parse_args(argv)
    try:
        pd = parse(a.decoded.read_bytes())
    except ParseError as exc:
        print(f"[not parsed] {exc}", file=sys.stderr)
        return 1
    text = json.dumps(asdict(pd), indent=2, ensure_ascii=False)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(f"[parsed] layout {pd.layout}: {pd.length} bytes, no leftover; {len(pd.turret_slots)} objective slots, "
          f"{len(pd.pgr_pairs)} PGR pairs" + (f"; written to {a.out}" if a.out else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
