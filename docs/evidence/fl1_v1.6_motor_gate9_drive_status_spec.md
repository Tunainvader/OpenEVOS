# Motor Gate 9: read-only drive status and error registers (X, Y, Z)

ADR-0030. Level L3 (reads only). Module `src/openevos/maintenance/motor_gate9_drive_status.py`.

## Why

On 2026-11-13, during the Gate 8 four-cycle run, Y twice ended a move toward 0 **on its own** near 73.5 mm:

| Run | Start (counts) | Ended at (CPOS = APOS) | Moved | State afterwards |
|---|---|---|---|---|
| 4-cycle run, cycle 1 "Y home" | 168,475 | 167,005 | 0.65 mm | idle after our stop |
| return to zero | 167,005 | 166,659 | 0.15 mm | ESTATEV 0 (idle) |

The out leg just before landed 10 counts past its target (all earlier landings were 0–1 count).
A power cycle homed Y normally. No reviewed read could say why the drive gave up the move.

## Addresses (Verified)

All from each axis' `variables.cfg` in the vendor `.t.zip` (firmware F443I, PIM2401). They are identical on X, Y and Z.
Every read is 16-bit, opcode `0xB004`, replies to host `0x0011`.

| Variable | Address | What it is |
|---|---|---|
| `MER` | 0x08FC | Motion error register (latched error bits) |
| `SRL` | 0x090E | Status register, low word |
| `SRH` | 0x090F | Status register, high word (already used by Gates 4–8) |
| `MSR` | 0x0308 | Motion status register |
| `ISR` | 0x0306 | Interrupt status register |
| `INSTATUS` | 0x0908 | Digital input levels (includes the LSP/LSN limit-switch inputs) |
| `POSERR` | 0x022A | Position error (signed, counts) |
| `ERRMAX` | 0x02C5 | Position-error limit for a control error |
| `TERRMAX` | 0x02C6 | Time the error must exceed `ERRMAX` |

Axis IDs (Gate 2, Verified): X = 1, Y = 2, Z = 4. 9 variables × 3 axes = 27 fixed frames.

`setup.cfg` (Verified) shows `isLSP = 1` and `isLSN = 1`: the limit-switch inputs are enabled on these drives.

## Rev 2: X/Y drive-program variables (Verified addresses, identical in the X and Y `variables.cfg`; not valid on Z)

| Variable | Address | Size | Role in the drive program |
|---|---|---|---|
| `ISTATUSFLAGS` | 0x03B2 | 16 | program flags: bit 0 set by the over-current handler; 0x2000 oscillation detected; 0x4000 / 0x8000 check passed / window over |
| `IOPTIONFLAGS` | 0x03EE | 16 | bit 0 (set at power-up) enables AXISOFF on detected oscillation |
| `IIQMAX` | 0x03BA | 16 | highest motor current `IQ` since the last move started |
| `IIQMIN` | 0x03BB | 16 | lowest (most negative) `IQ` since the last move started |
| `IMAXPROT` | 0x0297 | 16 | software over-current limit (set to 0x42DD = 17,117 at power-up) |
| `IQ` | 0x0230 | 16 | motor current now |
| `IOSCCOUNT` | 0x03EB | 16 | settle checks found outside `LCONVERGEBAND` |
| `BDOAXISOFF` | 0x03D1 | 16 | settle/oscillation check armed |
| `LTARGETPOSV` | 0x03B8 | 32 | the program's target position |

18 more frames (9 × X/Y); opcodes 0xB004 / 0xB005. Total 45 frames.

### What the Y program does (offline decode of `YAxis.sw`, 2026-11-13; control flow Strongly supported)

- **Main loop:** every pass calls `CHECKOVERCURRENT`, then records `IIQMAX`/`IIQMIN` (`UPDATECURRENTMAXMIN`), then dispatches on `ESTATEV`.
- **Over-current handler** (`CHECKOVERCURRENT` → `_INT2_ISR` when `IQ` > `IMAXPROT` or `IQ` < −`IMAXPROT`):
  1. `ISTATUSFLAGS` |= 1;
  2. `CPOS` = `APOS`, `CPOSCMD` = `CPOS` (the target is frozen where the axis is);
  3. waits, then **`AXISOFF`** (word `0002`), waits again, `CPOS` = `CPOSCMD` = `APOS`;
  4. `BDOAXISOFF` = 0; **`MER` &= ~0x0100** (clears the over-current bit); `ISR` &= ~4;
  5. **`ESTATEV` = 0, `ESTATE` = 0.**
  This matches every observation of the Y stops: CPOS = APOS short of the target, ESTATEV 13 → 0 without a host
  stop, SRL bit 15 (axis on) cleared, and `MER` = 0.
- **Settle / oscillation check** (`CHECK_CONVERGE`, in states 0 and 13, only while `SRH` bit 9 is set): armed by every
  move and stop; for 100 passes it counts passes with |`APOS` − `LTARGETPOSV`| > `LCONVERGEBAND`; if the count
  exceeds `IOSCTHRESH` (50) and `IOPTIONFLAGS` bit 0 is set, it issues **`AXISOFF`** (no state change, no CPOS change).
- `IIQMAX`/`IIQMIN` are reset only when a move or homing starts (not by `STATE_6_STOP`, not by the handler), so
  after a stop they still hold the peak current of the move that ended.

## Bit names (Probable)

The names come from the Technosoft register descriptions; they have not yet been verified on this drive.
The evidence always keeps the raw values; the decoded names are only a reading aid.

- `MER`: 0 CAN error, 1 short-circuit, 2 invalid setup, 3 control error, 4 serial error, 5 position wrap-around,
  6 LSP active, 7 LSN active, 8 over current, 9 I2t, 10 motor over temperature, 11 drive over temperature,
  12 over voltage, 13 under voltage, 14 command error, 15 ENABLE inactive.
- `SRH`: 6 LSP event, 7 LSN event, 9 target reached / not moving (Strongly supported: the vendor's IsMoving test),
  15 fault.
- `SRL`: 10 motion complete, 15 axis on.

## Procedure

1. Dry run by default (lists the 27 frames; opens nothing).
2. With `--send --instrument-on-2min --confirm "READ DRIVE STATUS ON COMn"`: Gate 1 rev 2 transport, line sync,
   axis-ID query (must be 1), then the 27 reads. **No writes exist in this module** (the link's write allowlist
   is empty).
3. The evidence JSON (git-ignored `openevos-scans/motor-gate9-*.json`) holds raw and decoded values per axis.

## Use inside Gate 8

Gate 8 allows the X/Y status frames (reads only; its writes are unchanged). It records a snapshot:

- before any motion (`baseline.status.before`);
- after a fault stop (`baseline.status.after_fault`), taken after the stop so stopping is never delayed;
- at the end of a passing run (`baseline.status.end`).

## Related change to the shared move loop (Gate 4 `_leg`)

- Completion now also requires `CPOS` = the leg's target (a stale "complete" state can survive into the first poll).
- If the drive is not moving and `CPOS` stays frozen short of the target for 3 polls, the leg faults at once
  ("the drive ended the move…") instead of waiting for the timeout.
- The leg timeout is now bounded by wall-clock time as well as by the poll count.

## Hazards

| ID | Hazard | Control |
|---|---|---|
| G9-1 | A read is mistaken for a write | 27 fixed frames, opcode 0xB004 only; empty write allowlist; tests |
| G9-2 | Reading a latched error clears it | Not expected for plain reads (Probable); the raw value is recorded on the first read |
| G9-3 | Misreading bit names | Raw values always kept; names labelled Probable |

## Rev 3: F (cube carriage, axis 5), read-only (2026-11-13)

The status registers are at the same addresses on F (FAxis `variables.cfg`, Verified). F's drive-program variables:
`ESTATEV` 0x03B6, `IINITONCEDONE` 0x03F8, `ISTATUSFLAGS` 0x03D2, `LTARGETPOS` 0x03B7, `LTARGETPOSV` 0x03DF,
`LPOSMIN` 0x03EA, `LPOSMAX` 0x03EC, `LCUBE1POS`–`LCUBE4POS` 0x03EE/0x03F0/0x03F2/0x03F4, `LMIRROR1POS` 0x03F6,
`CSPDCMD` 0x03BE, `CACCCMD` 0x03C0, `LCONVERGEBAND` 0x03E1, `IAXOFFTIMEOUT` 0x03C2.

F's program (offline decode of `FAxis.sw`, control flow Strongly supported):
- **`STATE_16_GOTO_CUBE`:** `LTARGETPOS` = slot 1–4 selects `LCUBEnPOS`, then `STATE_2_GOTO_POS`.
- **`STATE_2_GOTO_POS`:** clamps the target to `LPOSMIN`/`LPOSMAX`, then `AXISON`, then the move. It sets `ESTATEV` 0 at once; completion = `ISTATUSFLAGS` bit 0x4000 (converged).
- **Idle:** after the convergence countdown, F switches its own axis off (flag 0x8000).
- **`STATE_15_STOP`:** `STOP`, then `AXISOFF`, `CPOS = APOS`, `ESTATEV` 0.
