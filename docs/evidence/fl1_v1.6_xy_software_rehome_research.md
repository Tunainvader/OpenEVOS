# X/Y software re-home during a stage lock: research (offline, nothing sent)

- **Date:** 2026-11-19. **Status:** research for review. **Nothing was sent to any drive; COM3 was not opened.**
- **Asked by the operator:** "i want to be able to re home (as in powercycle) during stage lock (of course with a
  notification about stage lock rehoming), and then reach to the old position".
- **Why:** the drift monitor (ADR-0040) clears a lock only after X and Y are seen to re-home, which today means a power
  cycle. The 2026-11-18 locks were a Y over-current trip at rest (`stage-monitor-20261118T174259-overcurrent_trip`,
  Y `ISTATUSFLAGS` 0x1001, `SRL` 0x0400 = power stage off, `IIQMIN` −17,715, `MER` 0) and a 12-count Y landing
  error at target 0 (`stage-monitor-20261118T183517-landing_error`, `MER` 0). Neither is drift-releasable (ADR-0057).
- **Next step if approved:** Motor Gate 20 (§8), a dry-run-by-default maintenance tool; then the app action.

## 1. Sources (all byte-exact)

| Artefact | How obtained | Check |
|---|---|---|
| `XAxis.sw`, `YAxis.sw` (drive programs), `XAxis.t.zip`, `YAxis.t.zip` (`variables.cfg`, `setup.cfg`) | `PearlUpdate_Rev1.6_31201.amg` → ZIP → 7-Zip SFX (`TheUpdate.exe`, archive at offset 152,064) → Inno Setup `setup.exe` → LZMA chunk at 114,176. Unpacked with a stdlib-only Python reader; no vendor code executed | SHA-256 each matches `fl_auto1_v1.6_inventory.json` (`ea6c1227…`, `4b9bc1d9…`, `4e7f755f…`, `2afd012b…`) |
| `MotorController64.dll` (PearlScope's motor COM server) | same chunk, then Inno's call-instruction transform reversed (64 KiB blocks, high-byte flip) | SHA-256 `87ae582d…` matches the inventory and the L0 note |

Ghidra 12.2 was **not** used this time: it needs JDK 25 and only JDK 21 is on this PC. The DLL findings below come from
pattern-scanning its `.text` for the way the vendor builds frames (§4), which is exact for the constants it shows.
The scratch files are outside the repository (git-ignored scratchpad) and are not reproduced here.

## 2. The X/Y drive program's start-up and homing (from `XAxis.sw`; Strongly supported)

X and Y run the **same program**: the two images differ only in the axis-name word (`0x400C`) and the setup table
(`0x5E54…`); `variables.cfg` is identical. Label addresses are from `variables.cfg`; the opcode reading follows the
L0 note (`7400` jump, `7401` call, `0404` return, `21xx n` 16-bit assign to `0x03xx`, `74C0/74A0` branch on zero /
non-zero, `0102` `AXISON`, `01C4` `STOP`).

**Main-loop dispatch** (`MAINLOOP` 0x408E): `ESTATEV` 0, 1, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 21, 22,
23, 24 → `STATE_0_IDLE`, `STATE_1_INIT`, `_5_MOVE_REL`, `_6_STOP`, **`_7_HOME`**, `_8_MOVE_POS`, `_9_HALT`,
`_10_HALTED`, `_11_UNHALT`, `_12_MOVERELIDLE`, `_13_MOVEPOSIDLE`, `_16_HOME_AVE`, `_17_MAX`, `_18_MAX_AVE`,
**`_19_HOME_DOUBLE`**, `_20_RESET_VARINIT`, `_21_SHIPLOCK`, `_22_HOME_LSN`, `_23_HOME_HSX2`, `24_FORCEHOMELSN`.
`STATE_0_IDLE` copies a host-written `ESTATE` into `ESTATEV` and clears `ESTATE` (0x411B), as for moves.

**Power-up** (0x4000): wait for the supply flag, `AXISON` (0x4008), `ESTATEV` = 1, … → `STATE_1_INIT`.

**`STATE_1_INIT`** (0x4121): if `IINITONCEDONE` ≠ 12,345, call `INIT_ONCE`; then `ESTATEV` = 0.

**`INIT_ONCE`** (0x40FA):
1. call `VAR_INIT` (0x4027): `HOMEDONE` 0, home torque level, `IPENDINGMOVES` 0, `BHOMEINTERRUPT` 0, settle and
   oscillation parameters, `ISTATUSFLAGS` 0 then |= 0x1000, `ESTATE` 0, **`LPOSMIN`/`LPOSMAX` 0/50,000**,
   `CACCDEC`/`CACCSTOP` 1.0, `CPOSCMD` 0, **`CSPDCMD` 15.0**, `CACCCMD` 1.0, `LCOMMERRPOS` −1,000,000;
2. `STARTUPWAIT`: count `ILOOPCOUNT` 30,000 → 0 (a delay);
3. read an input bit into `VAR_I2` and call **`_HOME_LSN_DOUBLE`** (0x465F) or **`_HOME_DOUBLE`** (0x428C);
4. `INIT_HOME_DONE` (0x4111): **`IINITONCEDONE` = 12,345**. This is the **only** place in the program that writes
   `IINITONCEDONE`; nothing in the program ever lowers it.

**`STATE_19_HOME_DOUBLE`** (0x427A): the **same** input test and the **same** call to `_HOME_LSN_DOUBLE` or
`_HOME_DOUBLE` as `INIT_ONCE` step 3 (word for word: `6167 7096 0002 74A0 0367 …`), then back to the main loop.
It skips `VAR_INIT`, the delay and the `IINITONCEDONE` marker.

**`_HOME_DOUBLE`**: `ISTATUSFLAGS` &= 0x1FFE (clears bit 0 over-current and bits 13–15), `LTARGETPOSV` 0,
`IIQMIN`/`IIQMAX` 0, `HOMEDONE` bit 0 cleared; `HOMETOHARDSTOP` twice (fast, then slow index seek); `CPOS` 0,
`ESTATEV` 0, `BCOASTTOSTOP` 0, **`HOMEDONE` |= 1**, `BHOMEINTERRUPT` 0. `_HOME_LSN_DOUBLE` has the same shape
using the negative limit switch.

**`HOMETOHARDSTOP`** (0x444D) switches the power stage on itself (`AXISON` at 0x445E; `_HOME_LSN_DOUBLE` at 0x4672),
drives into the hard stop under the home torque limit, backs off, then finds the encoder index (homing mode 30)
and sets home. It checks over-current (`CHECKOVERCURRENT`) and `BHOMEINTERRUPT` throughout; either ends homing at
`_HOME_INTERRUPTED` (`STOP`), sets `ISTATUSFLAGS` bit 3 (0x0008; bit 0 too for over-current) and leaves `HOMEDONE`
bit 0 clear.

**The other candidates do not fit:**
- `STATE_7_HOME`: a single (not double) hard-stop home; not what power-up does.
- `STATE_20_RESET_VARINIT` (0x43FE): despite the name, only `ISTATUSFLAGS` &= 0xEFFF and `ESTATEV` 0. It does not call
  `VAR_INIT` or home.
- `STATE_17/18` (find the far hard stop), `21` (shipping lock), `22/24` (forced limit-switch homes), `23`: not the
  power-up path.

## 3. Ways to make the drives re-run power-up homing

| # | Frames per axis | What runs | Vendor uses it? | Lock clears by |
|---|---|---|---|---|
| **A** | `ESTATE` = 19 | `_HOME_DOUBLE` / `_HOME_LSN_DOUBLE`, exactly the homing power-up runs | **Yes**: `XYStageHomeXAxis(AndWait)` / `…YAxis…` (§4) | a new monitor rule on `HOMEDONE` (§6) |
| **B** | `IINITONCEDONE` = 0, then `ESTATE` = 1 | the whole `INIT_ONCE`: `VAR_INIT`, delay, the same homing, marker 12,345 | **No**: PearlScope never writes `IINITONCEDONE` and never sends `ESTATE` 1 | the existing `IINITONCEDONE` rule, unchanged |
| C | TML `RESET` | drive firmware reboot; autorun from EEPROM, as at power-on | only inside its firmware-update routines, as TML text through `TML_lib.dll` | the existing rule |

**Option C is rejected:** its binary encoding is not in any file we have (the vendor sends the word `RESET` as text
through Technosoft's compiler, used only in its firmware-update paths, `FUN_18003FCF0` / `FUN_180029B50`); **X is the
RS-232 gateway** (Gate 1: gateway ID 1), so resetting X drops the link to every axis mid-sequence; and a reset sits
next to the EEPROM program loader that `safety/firmware.py` bans. Not worth the risk when A and B reach the same
homing code.

## 4. What PearlScope sends to home X and Y (`MotorController64.dll`, Strongly supported)

The DLL builds every short write on the stack the same way: `mov word [f], 0x0006` (length 6, byte 1 = 0),
`mov dword [f+3], <opcode hi, opcode lo, value hi, value lo>`, `mov byte [f+2], <destination>`,
`mov byte [f+7], <checksum>`. Listing every such `21 B3` (X/Y `ESTATE`) store gives the X/Y states the vendor
requests: 0, 5, 8, 9, 11, 17, 18, 21 and **19**. The 19 frames, each built twice per axis (the plain and the
`…AndWait` method), with the checksum the DLL itself stores:

| Axis | Frame | VA of the stores |
|---|---|---|
| X | `06 00 10 21 B3 00 13 FD` | `0x18003995F`, `0x180039A1F` |
| Y | `06 00 20 21 B3 00 13 0D` | `0x180039B1F`, `0x180039BDF` |

The DLL's exports and type-library strings name the methods `XYStageHomeXAxis`, `XYStageHomeXAxisAndWait`,
`XAxisGetHomeDone`, `XAxisInterruptHomeRoutine`, `GetXAxisHomeWasInterrupted`, `GetXAxisHomeIndexCaptureError`
(and the Y equivalents). Their exact bodies were not decompiled (no Ghidra run, §1), so which store belongs to
which method is **Probable**, not Verified. **No `21 FE` store (`IINITONCEDONE`) exists** in the DLL: the vendor reads
the marker (`AxisVariableInitIsComplete`, `WM_APP_AXIS_RESET_OCCURRED`) but never writes it. How
`…InterruptHomeRoutine` builds its frame was not found by this scan (the address is not an immediate): **Unknown**.

Both frames are exact copies of OpenEVOS's already-verified Gate 3–6 `ESTATE` write (only the value differs), so
`protocol._short_write` builds them byte for byte.

## 5. What a software re-home does and does not reproduce of a power cycle

| Power cycle | A (`ESTATE` 19) | B (`IINITONCEDONE` 0 + `ESTATE` 1) |
|---|---|---|
| Double hard-stop + index home, power stage switched on by the drive | same routine | same routine |
| Clears over-current (`ISTATUSFLAGS` bit 0), `IIQMIN`/`IIQMAX` | yes (`_HOME_DOUBLE` start) | yes |
| `VAR_INIT` (limits 0/50,000, speed 15.0, settle and oscillation parameters, `LCOMMERRPOS`) | **no**: calibrated limits and speed stay | yes: the app's ADR-0056 path reloads the calibrated limits once X/Y sit at home on the power-up box |
| `IINITONCEDONE` leaves 12,345 and returns | **no** | yes |
| Drive firmware reboot (clears latched `MER` hardware faults, re-reads setup) | **no** | **no** |
| Other axes (Z, F, O, P) | untouched | untouched |

So **neither A nor B clears a latched drive fault** (`MER` ≠ 0, short-circuit, I²t, control-error). Both recent locks
had `MER` 0. A software re-home must refuse when `MER` ≠ 0 or `SRH`/`SRL` show a fault, and say "power-cycle".

## 6. Effect on the stage monitor (ADR-0040)

- Today the lock clears only when each axis' `IINITONCEDONE` is seen ≠ 12,345 and then 12,345 again
  (`StageMonitor.status`). **Option B uses that path unchanged.**
- **Option A** never touches the marker, so it needs a second, narrower clear rule: *a software re-home the app itself
  started* — for each axis, the app saw `HOMEDONE` bit 0 go to 0 after it sent the frame, then back to 1 with
  `ESTATEV` 0, `SRH` bit 9 set, `ISTATUSFLAGS` bits 0 and 3 clear, `MER` 0 and `APOS` = `CPOS` = 0 (±5). `HOMEDONE` is
  already a reviewed read (`0x03B0`, power-up record). The latch file then records "software re-home (Gate 20
  frames)".
- During homing the axis moves with no OpenEVOS move command, so the fast drift checks and the runaway watchdog
  (shadow mode, ADR-0047) must treat it as a commanded motion (as `begin_move` does), or they would latch again.

## 7. Risks

| # | Risk | Mitigation proposed |
|---|---|---|
| R1 | The stage drives to the home corner (negative hard stop), like power-up, with the objective wherever Z is | Refuse unless Z is at its home (0 ± 5 counts); the app offers to lower Z first with its reviewed Z frames. The turret is never moved |
| R2 | **The controlled stop does not work while homing**: the drive's main loop is inside the homing call, so `ESTATE` = 6 is not read until homing ends | The operator keeps the power switch in reach (as Gates 4–8). The drive-level abort is `BHOMEINTERRUPT` = 1 (`06 00 10 21 FD 00 01 35` / `06 00 20 21 FD 00 01 45`), which the program polls; it is **not** a vendor-confirmed frame (§4), so the gate would hold it as an emergency-only write, off by default |
| R3 | The homing itself trips over-current (Y did at rest on 2026-11-18) | The drive stops, sets bits 0 and 3 and leaves `HOMEDONE` 0. The gate reports fail, the lock stays, and the advice is a power cycle. No retry |
| R4 | A latched hardware fault (`MER` ≠ 0) blocks `AXISON`; homing would wait for torque that never comes | Refuse before sending (§5). Bounded wait (homing timeout), then report |
| R5 | X and Y homing together | One axis at a time: X, wait for done, then Y (standing rule: no combined X/Y moves). Power-up homes both at once; the vendor's methods are per axis |
| R6 | Option B leaves an axis "not initialised" if the `ESTATE` 1 write is lost after `IINITONCEDONE` 0 | Every write is acknowledged (`O`); on a missing acknowledgement send nothing more; the app already refuses moves while unhomed; recovery is a power cycle |
| R7 | Option B resets the limits to 0–50,000; returning to a position beyond 22 mm needs the calibrated limits | The existing ADR-0056 reload runs first (Gate 7 frames, read back); the return move waits for it. Option A keeps them |
| R8 | Returning to the old position after homing | Ordinary reviewed moves (Gates 4/6 frames), X then Y, inside the calibrated limits, with the usual pre-move and landing checks |
| R9 | The cause of the Y trips is unknown; re-homing may hide a real fault | Every re-home writes evidence (before/after reads, `IIQMIN`/`IIQMAX`, timing). The over-current lock text keeps recommending investigation |

## 8. Proposal for review

**Recommended: option A**, the vendor's own homing frames, one axis at a time, as **Motor Gate 20**
(`maintenance/motor_gate20_xy_rehome.py`), then an app action built on the same frames.

- **New writes (two constants), RAM only, no EEPROM / setup / HW_DATA / calibration write:**
  - X: `06 00 10 21 B3 00 13 FD` (`ESTATE` = 19, `STATE_19_HOME_DOUBLE`)
  - Y: `06 00 20 21 B3 00 13 0D`
- **Reads:** only the existing reviewed X/Y/Z reads (`ESTATEV`, `IINITONCEDONE`, `HOMEDONE`, `ISTATUSFLAGS`, `SRH`,
  `SRL`, `MER`, `APOS`, `CPOS`, `LPOSMIN`, `LPOSMAX`, `IIQMIN`, `IIQMAX`, Z `APOS`).
- **Gate procedure:** dry run by default; app closed (COM3 single owner); preconditions (both axes idle, `MER` 0, no
  `SRH`/`SRL` fault, Z at home, stage clear); confirm phrase; then for X: send, require `O`, poll until `HOMEDONE`
  bit 0 falls and returns with `ESTATEV` 0 (timeout to be set from the first run, proposed 120 s), check the pass
  criteria (§6); then the same for Y. No return move in the gate. Evidence JSON for every run, including dry runs
  and refusals.
- **Wiring:** `REVIEWED_MAINTENANCE["motor.xy_rehome"]` in `safety/firmware.py`; the two frames enter the transport
  only as `extra_writes` for the duration of one re-home (as ADR-0056 does for the limit writes), never in
  `is_motion_write`.

**Alternative: option B**, if the operator prefers the lock to clear through the existing `IINITONCEDONE` path with
no monitor change and wants `VAR_INIT` re-run as at power-on. Its frames would be X `06 00 10 21 FE 00 00 35` then
`06 00 10 21 B3 00 01 EB`; Y `06 00 20 21 FE 00 00 45` then `06 00 20 21 B3 00 01 FB`. These are new to this
instrument and to the vendor software, so B carries more unknowns than A.

**Neither option replaces a power cycle for a latched drive fault** (§5); the app keeps that advice for `MER` ≠ 0.

**Decision (operator, 2026-11-19): option A approved.** Implemented as Motor Gate 20 (§10), ADR-0058.

## 10. Motor Gate 20: how to run it

Module `src/openevos/maintenance/motor_gate20_xy_rehome.py`, `REVIEWED_MAINTENANCE["motor.xy_rehome"]`. Every run writes
`openevos-scans/motor-gate20-<time>-<outcome>.json`, dry runs and refusals included.

1. Close the OpenEVOS app (COM3 has one owner). The instrument has been on ≥ 2 minutes.
2. Lower Z to 0 (the gate refuses otherwise). Stage travel to the home corner clear; hand near the power switch.
3. Dry run (opens nothing):
   ```
   python -m openevos.maintenance.motor_gate20_xy_rehome
   ```
4. Only with the operator's explicit approval in chat:
   ```
   python -m openevos.maintenance.motor_gate20_xy_rehome --send --instrument-on-2min --stage-clear-watching --confirm "REHOME X AND Y ON COM3"
   ```
   `--axes X` or `--axes Y` homes one axis (phrase `REHOME X ON COM3` / `REHOME Y ON COM3`).

What it does: sync, gateway ID 1, readiness on all six axes, X/Y baseline (`MER`, `SRH`, `ISTATUSFLAGS`, `HOMEDONE`,
limits, speed, currents) and Z position; refuses on §7's conditions. Then for X: send H1, require `O`, poll
`ESTATEV`/`HOMEDONE`/`APOS`/`SRH` every 0.2 s until started (≤ 3 s) and done (≤ 120 s), final reads and checks;
then Y the same, only if X passed. On a fault or timeout it sends nothing more, reads the state and reports.

## 11. First run on the instrument (2026-11-19, operator-approved in chat: "Yes, run X then Y"): PASS

- **Evidence:** `openevos-scans/motor-gate20-20261118T193502-pass.json` (UTC stamp; dry run `…193351-dry_run.json`).
- **Conditions:** OpenEVOS and PearlScope closed; COM3 present; all six axes idle; Z at 0; X and Y at home (0, 0) on the
  power-up limits 0/50,000 (the calibrated limits were not loaded); `MER` 0; `ISTATUSFLAGS` 0xD000. **Not a locked
  state:** the drives were healthy, so this run proves the frames and the homing, not recovery from a trip.
- **Sent:** exactly the two frames, X then Y, each acknowledged `O`. Nothing else was written (1,624 bytes in total,
  all syncs and reviewed reads).

| | X | Y |
|---|---|---|
| Started (`ESTATEV` 19, `HOMEDONE` 0) at first poll | 1.6 s | 1.6 s |
| Done (`ESTATEV` 0, `HOMEDONE` 1) | **17.7 s** | **10.6 s** |
| Path seen in `APOS` | out to +7,726, back past 0 into the hard stop (−2,221), index, 0 | out to +8,462, hard stop (−745), index, 0 |
| `LHOMEINDEXDELTA` before → after | 2,183 → 2,182 | 738 → 739 |
| Peak current `IIQMAX`/`IIQMIN` | 1,623 / −2,689 | 2,137 / −2,929 |
| Final | `APOS` = `CPOS` = 0, `ISTATUSFLAGS` 0xD000, `MER` 0, `SRL` 0xC400, limits and speed unchanged, `IINITONCEDONE` 12,345 | same |

All six axes idle afterwards. The hard-stop-to-index distance repeated to 1 count on both axes.

**Answers to the open questions:**
1. A double home takes 11–18 s here; the 120 s timeout is ample.
2. The drives answer reads while homing (Verified).
3. `ESTATEV` 19 and `HOMEDONE` 0 were visible at the first poll and throughout. Note: one poll of four reads takes
   about 1.8 s on this link (each read has its own line sync), not the nominal 0.2 s.
4. No over-current during homing on either axis.

**Still unproven on the unit:** re-homing an axis whose power stage the over-current handler switched off
(the 2026-11-18 Y state). The program decode says homing switches it back on (§2); the first such lock will show it.

## 12. Open questions for the first gate run (answered in §11)
1. How long a double home takes on this unit (sets the timeout).
2. Whether the drive answers reads while homing (expected: yes, the firmware serves the link independently of the
   TML program; Probable).
3. Whether `HOMEDONE` reads 0 for long enough to be seen at the poll rate (the gate polls at ≥ 5 Hz; `ESTATEV` = 19
   during homing is a second witness).
4. Whether Y over-current trips during homing (R3).
