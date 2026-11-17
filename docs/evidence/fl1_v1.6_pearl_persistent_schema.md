# PearlScope persistent-data stream: schema (L0) and verification against our unit

- **Source:** decompilation (Ghidra 12.2) of the byte-exact `PearlScope64.exe` from the FL Auto 1.6 package:
  - `CHardwarePersistentDataController::DoReadHardwarePersistentData` (`FUN_14012F8A0`);
  - its readers: uEye `FUN_140327290`, PGR `FUN_1401E0490`, motor `FUN_1401BCFD0` (with base `FUN_1401BA970`), turret `FUN_14031D610`;
  - the string reader `FUN_1403208A0`;
  - and, to settle the format, the turret writer `FUN_14031BF00`.
- **Container:** see §5.5a of the LED-board static analysis. The stream is the zlib-decoded payload of the "lico" blob in the LED board's persistent-data section 2.
- **Parser:** [`openevos.maintenance.pearl_persistent`](../../src/openevos/maintenance/pearl_persistent.py). It is tested on synthetic streams only (`tests/test_pearl_persistent.py`).
- **No values from our instrument are in this repository.** They stay in the git-ignored `openevos-scans/`.

## Encoding

- **Little-endian, positional, no tags.** Fields are read in a fixed order with `IStream::Read`.
- **`int32`** = 4 bytes, **`double`** = 8 bytes (IEEE-754), **`uint16`** = 2 bytes.
- **`string`** = `int32` character count *n*, then *n* UTF-16LE code units, with no terminator.
- **Section header:** each section starts with a 4-byte ASCII marker and a `double` version that must equal **1.0**. Otherwise the reader stops.

## Layout, in stream order

| Section | Marker | Reader | Body after marker + version |
|---|---|---|---|
| uEye colour camera | `coca` | `FUN_140327290` | `double` **RedGain, GreenGain, BlueGain** |
| PGR mono camera | `moca` | `FUN_1401E0490` | `int32` *n*, then *n* × (`uint16`, `uint16`): coordinate pairs, plausibly a pixel list (not yet named) |
| Motor controller base | `mcba` | `FUN_1401BA970` | 23 settings of the motor controller (table below), 116 bytes |
| Motor controller | `moco` | `FUN_1401BCFD0` | `int32` **X/Y/Z AxisOffset**; `double` **X/Y/Z AxisMillimetersPerRevolution**; `int32` **X/Y AxisRegistration** |
| Turret controller | `tpco` | `FUN_14031D610` | 8 objective slots (below), then 5 × (`int32` slot, `int32` objective index) |

### How each name was established

- **`coca`:** the three values go to `FUN_140324A80` / `BF0` / `D60`. These are `CUEyeColorCameraController::DoSetRedGain` / `DoSetGreenGain` / `DoSetBlueGain`, named by their own log strings, and they persist ini keys `RedGain` / `GreenGain` / `BlueGain`.
- **`moco`:**
  - the integers go to `FUN_1400209F0` / `FUN_140021510` / `FUN_140022060`, which write `[PearlScopeMotorController] XAxisOffset` / `YAxisOffset` / `ZAxisOffset`;
  - the last two go to `FUN_1401BF5A0` / `FUN_1401BF900`, which write `XAxisRegistration` / `YAxisRegistration`;
  - the doubles are written directly as `X/Y/ZAxisMillimetersPerRevolution`.
- **`mcba`:** each value is passed to a method of the motor controller's COM interface, called through its vtable offset. The offsets were resolved against the **`IEvosGen3MotorController`** interface in `MotorController64.dll`'s type library:
  - the library was extracted from the package and read directly in the `MSFT` format, because Windows' loader rejects it;
  - the parameter names `lMin, lMax` and `eLocation, lMotorPos` come from the same records;
  - each setter sits next to its matching getter and its axis's `MoveComplete`, which corroborates the offset mapping.

| # | Type | Field | Setter (vtable offset) |
|---|---|---|---|
| 1–2 | `int32` | X axis position limits, lMin / lMax | `SetXAxisPositionLimits` (`+0x4B0`) |
| 3 | `int32` | X axis home torque level | `SetXAxisHomeTorqueLevel` (`+0x488`) |
| 4–5 | `int32` | Y axis position limits, lMin / lMax | `SetYAxisPositionLimits` (`+0x4C0`) |
| 6 | `int32` | Y axis home torque level | `SetYAxisHomeTorqueLevel` (`+0x498`) |
| 7–8 | `int32` | Z axis position limits, lMin / lMax | `SetZAxisPositionLimits` (`+0x660`) |
| 9–10 | `int32` | F (filter-cube carriage) axis position limits, lMin / lMax | `SetFAxisPositionLimits` (`+0x7E0`) |
| 11–12 | `double` | filter-cube positioning speed / acceleration | `SetFilterCubePositioningSpeed` / `…Acceleration` (`+0x720` / `+0x728`) |
| 13–17 | `int32` | F axis motor position for locations 1–5 | `SetFAxisLocationMotorPosition(eLocation, lMotorPos)` (`+0x7C8`) |
| 18–19 | `double` | objective turret positioning speed / acceleration | `SetObjectiveTurretPositioning…` (`+0x8B8` / `+0x8C0`) |
| 20 | `int32` | objective turret clockwise | `SetObjectiveTurretClockwise` (`+0x910`) |
| 21–22 | `double` | phase wheel positioning speed / acceleration | `SetPhaseWheelPositioning…` (`+0x820` / `+0x828`) |
| 23 | `int32` | phase turret clockwise | `SetPhaseTurretClockwise` (`+0x878`) |

- **Turret positions:** `FUN_140319EF0` writes `[ObjectiveLenses] <position>-Selection = <slot>-<index>`. So each pair is (objective slot, index of the objective within that slot's list).

**Units.** The position limits, offsets and registrations are drive position **counts**. PearlScope converts X and Y with **2,880 counts per motor revolution** (about 0.441 µm per count at 1.27 mm/rev) and Z with **5,000** (about 0.122 µm per count at 0.61 mm/rev) ([motor subsystem §3a](fl1_v1.6_motor_subsystem_l0.md)). The F carriage positions are counts with an unknown lead. Speeds and accelerations are in the drive's units.

### Turret slots (reader loop 1..8)

Each slot holds these strings, each written to `PearlScope.ini [ObjectiveLenses]`:
1. **The objective list** for that slot (`|`-separated model names with catalogue numbers).
2. **For each camera,** in order `UI146xLE-C Color Camera`, then `Sony ICX445AL (1/3" 1296x964 CCD) Camera`: `-Calibrations`, `-Parfocalities`, `-Parcentrations`.
3. **Then** `-PhaseWheel`, `-NA`, `-Immersion`, and **`-APO` only in the 1.6 format.**

The string values are `|`-separated lists, one entry per objective in the slot.

### Turret positions (reader loop 1..5)

Five pairs, each passed with its position number to `FUN_140319EF0`. Most likely the meaning is (objective slot fitted at turret position *p*, a per-position value).

## Two formats: 1.6 and pre-1.6

- **The 1.6 reader and writer** both use four tail strings per slot, including `-APO`.
- **The blob on our unit's LED board has three** (no `-APO`). The parser therefore accepts both layouts, but only on an exact, complete parse: every marker, every version, and the end of the stream at the last byte.
- **Consequence (Probable):**
  - the stored blob was written by a PearlScope **older than 1.6**, which fits the board's application build 3.0.25949;
  - PearlScope 1.6's reader, given this blob unchanged, would read the next slot's objective list as `-APO` and lose alignment.

## Verification against our unit (2026-11-12, offline, local only)

- **Input:** the Step D blob (ADR-0015), zlib-decoded to 5,660 bytes.
- **Result:** the parse is **exact** in the pre-1.6 layout. All five section markers and versions are valid, 8 objective slots, 0 PGR pairs, 5 turret positions, **0 bytes left over**. The 1.6 layout does not parse.
- **Plausibility, values not recorded here:**
  - the X/Y/Z mm-per-revolution are positive and finite, and look like common lead-screw pitches;
  - the objective lists name real AMG and Olympus objectives from 2× to 100×;
  - the turret positions reference five distinct slots.
- **Named-field consistency** (same day, values not recorded here):
  - min < max for the X, Y, Z and F position limits;
  - both home torques are non-zero;
  - the five F-axis carriage positions strictly increase;
  - every speed and acceleration is positive and finite;
  - both turret direction flags are 0.
- **Status:**
  - The structure is **Verified**, and every field is named except the PGR pair list, which is empty on our unit.
  - All values are **CANDIDATE**. Before any becomes a capability limit, its **units** must be established (counts vs µm) and it must be checked physically, for example by comparing the X/Y limits with measured stage travel.
