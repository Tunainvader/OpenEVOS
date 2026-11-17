# FL Auto 1.6 motor subsystem: Technosoft drives (L0, offline)

- **Date:** 2026-11-12.
- **Scope:** static analysis only. **No serial port opened, nothing sent to any drive.**
- **Sources:**
  - files carved from the decoded FL Auto 1.6 installer payload, each **SHA-256-verified** against `fl_auto1_v1.6_inventory.json`;
  - `MotorController64.dll`, after reversing Inno Setup's call-instruction transform: byte-exact, `87ae582d…`;
  - the `axis_firmware/` folder: `MotorFirmware.ini`, plus `<axis>.sw` and `<axis>.t.zip` for each of X, Y, Z, F, O and P.

The extracted files stay in the git-ignored scratch area. This note records structure only.

## 1. Hardware and roles (Strongly supported)

- **Drives.** Each axis is a **Technosoft PIM2401-CAN** intelligent drive (`setup.cfg`: `Description = PIM2401-CAN`, `FirmwareID = F443I`, `ProductID = P035.001.E102`).
- **Motor and feedback.** A closed-loop **stepper** (`IsStepMotor = 1`, `IsClosedPosition = 1`) with a motor-side **incremental encoder** with index (`isIndex = 1`), and **positive and negative limit switches** (`isLSP`, `isLSN`).
- **Six axes**, matching `IEvosGen3MotorController`:

  | Axis | Role | Drive program version (`MotorFirmware.ini`) |
  |---|---|---|
  | X, Y | stage | 29 |
  | Z | focus | 21 |
  | F | filter-cube carriage | 29 |
  | O | objective turret | 7 |
  | P | phase wheel | 6 |

## 2. What runs where (Strongly supported)

- **Each drive stores and runs its own TML program from its EEPROM.** `setup.cfg` has `LoadToE2ROM = 1`, `RunFromE2ROM = 1` and `TMLStartE2ROM = 4000`. The `.sw` file is the text image of that EEPROM (an address, then hex words), starting at `0x4000`.
- **The program is a state machine for initialisation and homing.** Labels in `variables.cfg` include `VAR_INIT`, `INIT_ONCE`, `STARTUPWAIT`, `INIT_HOME_NO_LSN`, `INIT_HOME_DONE`, `STATE_7_HOME`, `STATE_16_HOME_AVE`, `STATE_19_HOME_DOUBLE`, `STATE_22_HOME_LSN`, `HOMETOHARDSTOP`, `CLAMPTOLIMITS` and `FINDLSN`.
- **It has variables** such as `HOMEDONE`, `HOME_TORQUE_LEVEL`, `HOMEPOS`, `HOMESPD`, `INUMHOMESTOAVERAGE`, `LHOMEINDEXDELTA`, `BHOMEINTERRUPT` and `IINITONCEDONE`.
- **The host DLL reports "Autorun Enabled" as a drive status.**

## 3. How the PC talks to the drives (Strongly supported)

`MotorController64.dll` imports exactly nine functions from `TML_lib.dll`:

| Function | Role |
|---|---|
| `TS_OpenChannel` / `TS_CloseChannel` | open and close the link |
| `TS_SelectAxis` | address one drive on the bus |
| `TS_LoadSetup` / `TS_SetupAxis` / `TS_DriveInitialisation` | host-side setup objects and drive init |
| **`TS_Execute`** | send TML commands as text. **All motion and all parameter writes go through this.** |
| **`TS_DownloadSwFile`** | write a `.sw` image into a drive's EEPROM |
| `TS_GetLastErrorText` | error text |

- The strings include "CANbus error", consistent with CAN between the drives.
- **Inferred (Probable):** a typical Technosoft topology is one PC serial link to the first drive, which relays commands over CAN to the others. The FTDI FT232R found at first connection (`COM3`, on the microscope's USB hub) would be that link. The port, baud rate and axis IDs are still to be read from `TS_OpenChannel`'s call site.

## 3a. Decompiled paths (Ghidra 12.2, byte-exact DLL; Strongly supported)

### `TML_lib.dll` is used only to update drive firmware
- `TS_OpenChannel("COMn", 0, hostID 1, 115200)` is called by `FUN_18002E470`. The per-axis setup `FUN_18002E230` does `TS_LoadSetup(<axis>.t.zip)` → `TS_SetupAxis` → `TS_SelectAxis` → `TS_DriveInitialisation`.
- **Axis IDs:** X = 1, Y = 2, P = 3, Z = 4, F = 5, O = 6.
- Every caller of these functions, of `TS_Execute`, and of `TS_DownloadSwFile` (`FUN_180035D70`) belongs to one of two **firmware-update** routes:
  - **`UpdateMotionControllerFirmware`** (COM vtable slot 23, `FUN_180036C70`) → `FUN_18003FCF0`, which sends `iUpdateTmp = 0x4000; (iUpdateTmp),spi = 0x4000;`, then `RESET`, download, `…spi = 0x649C;`, `RESET`;
  - **a per-axis update routine** `FUN_180029B50`, reached from a UI handler (`FUN_18000C360`). For each selected axis it opens the channel, initialises the axis, sends `RESET`, downloads the `.sw`, then `RESET` again.
- The download routine first writes a patched copy of the axis image, `Fixed.sw`.
- **`spi = …` writes the drive's SPI EEPROM** at `0x4000`, the first program word. `0x649C` is exactly the second word of the shipped `XAxis.sw` image (the first is the address `4000`). Writing `0x4000` there evidently disables the stored program (autorun) during the update, and `0x649C` restores it.

### Ordinary operation uses the DLL's own serial code
- `FUN_180041560` opens `\\.\COMn` (`CreateFileW`, exclusive) at **9,600 baud, 8 data bits, no parity, 2 stop bits**. It then exchanges messages with the drives (`FUN_180042330`, `FUN_18002C430`, `FUN_18002C2C0`, …) and switches the port to **115,200 baud**. This is the usual Technosoft pattern: drives answer at 9,600 after reset, and the host raises the speed. *Correction (Motor Gate 1 §13, 2026-11-13): **on this instrument the drives answer at 115,200 straight after power-up**. PearlScope's first pass therefore succeeds, and its baud-change write is normally never sent.*
- **Opening the port therefore sends messages to the drives.** Their exact bytes are not yet decoded.
- `MotorControlIsSupported` (slot 7) reaches this port-open routine, so **even a capability check talks to the drives.**
- Six small functions do the framed `ReadFile`/`WriteFile` exchanges. This is a native implementation of Technosoft's binary serial protocol.

### What connecting sends (port-open `FUN_180041560`, decompiled; Strongly supported unless marked)

**Frame format.** Technosoft's RS-232 binary protocol: `length | axis address (2 bytes, ID << 4) | opcode (2 bytes) | data words | checksum` (8-bit sum of the preceding bytes). A drive acknowledges with `O` (`0x4F`), and replies to reads with a framed answer whose checksum the host verifies (`FUN_180025A60`).

**The sequence, for each candidate COM port (`FUN_180027560` enumerates them), trying 115,200 and then 9,600 baud.** *Correction (2026-11-12, after Motor Gate 1's first run): an earlier version said 9,600 first. The loop's first pass sets 115,200 (`iVar7 == 0`). The DCB is zeroed and then filled by `GetCommState`, so the driver's current flags, including DTR/RTS control, are kept; only BaudRate, ByteSize = 8, Parity = none and StopBits = 2 are changed. Timeouts: ReadIntervalTimeout 1000 ms, ReadTotalTimeoutMultiplier 10, ReadTotalTimeoutConstant 10.*

| # | Message | Bytes | Effect |
|---|---|---|---|
| 1 | Line sync (`FUN_18002C430`) | single `FF`, up to 15 × at 10 ms, until a drive answers `0D` | none (synchronisation) |
| 2 | Gateway axis-ID query (`FUN_18003F300`) | `08 00 00 B0 …`, a "give me data" request to address 0 (variable address not fully reconstructed) | **read** |
| 3 | **Only if the gateway reports an ID ≠ 1:** `FUN_180042330(id, 1)` | `06 \| id \| 08 01 \| 00 01 \| cs` | **write: most likely `AXISID 1`, reassigning the drive's axis ID** (Probable; whether the change persists is Unknown) |
| 4 | Presence probes, one per axis (`FUN_18002C2C0`, inline copies) | `08 \| 00 n0 \| B0 05 \| 00 11 \| 02 9E \| cs`, e.g. `08 00 10 B0 05 00 11 02 9E 7E` for axis 1 | **read** of 32-bit variable `0x029E` = **`CPOS`** (command position, from `variables.cfg`) on axes 1, 4, 5 and 3, replies sent to host address `0x0011` |
| 5 | **Only when connected at 9,600:** baud change | `06 00 10 08 20 00 04 cs` | **write of the drive's serial speed**: opcode `0x0820`, value 4, then the PC switches to 115,200 at once, i.e. almost certainly `SCIBR` = 115,200 (Strongly supported by context) |

**Classification:**
- The connect path sends **no motion, enable (`AXISON`), homing, `RESET`, EEPROM or program-start command.**
- It writes at most two settings: **the serial baud rate** (5), and **conditionally, the gateway's axis ID** (3).
- Everything else is synchronisation or reads.
- Whether a drive is already **moving on its own** at power-up is separate: it depends on the drive's EEPROM program (§6 item 2).

**Consequence for a future OpenEVOS L3 probe.** A minimal read-only first contact is possible:
- line sync, the axis-ID read and `CPOS` reads, **at 9,600 baud only**;
- **without** the baud change and **never** the axis-ID write.

It must stay held until the drives' power-up behaviour is known, and then be reviewed like the LED board's Gate 1.

### Homing
Homing is exposed as explicit methods (`XYStageHomeXAxis` / `HomeYAxis`, `HomeZAxis`, …, plus homing-torque and averaging settings). That is consistent with homing happening only when the host asks (Probable; not yet traced end to end).

### PearlScope updates the drive firmware automatically at start-up (Strongly supported)
- **Where.** In `CPearlScopeDialog::OnInitDialog` (`FUN_1401A43D0`), after the colour-camera initialisation, when a motor controller is present, PearlScope:
  1. calls `CPearlScopeMotorController::GetUpdateFirmwareAxes` (`FUN_1401BDE70`);
  2. **if the returned list is not empty**, shows the "Update Motor Firmware" progress dialog and calls `CPearlScopeMotorController::UpdateFirmware` (`FUN_1401BE5D0`). **There is no operator prompt.**
- **The rule.** `GetUpdateFirmwareAxes` reads `MotorFirmware.ini` (X 29, Y 29, Z 21, F 29, O 7, P 6) and each drive's current script version (`X/Y/Z/F/O/PAxisGetScriptVersion`, vtable `+0x78`…`+0xA0`). It lists every axis whose **current version is lower**. This is the same pattern as the LED board (§5 of the LED analysis).
- **On our unit:** the drives' current script versions are **Unknown**, so whether running PearlScope 1.6 here would have rewritten drive EEPROMs is Unknown. This is a further reason the vendor software must not be run on this instrument.

### Units, all closed-loop axes (Strongly supported)
- **X:** exactly the Y formulas (`FUN_140034110`: `2880 / (XmmPerRev × 1000)`; `FUN_1400D7970`: `round(864 / XmmPerRev)`). So **2,880 counts/rev**, and at 1.27 mm/rev **1 count ≈ 0.441 µm**.
- **Z:** `FUN_1400FF980` computes `value × 5000 / (ZmmPerRev × 1000)` (constants `5000.0`, `1000.0`; default `ZmmPerRev` = 0.61). So **5,000 counts/rev**, and at 0.61 mm/rev **1 count ≈ 0.122 µm**.
- **F, O, P:** F counts have an unknown lead. O and P are open-loop; their drive programs count turret ticks with `LTICKSPERTURRETREV`.

### Read frames for a future reviewed motor read (draft; nothing sent)

All are Technosoft binary frames, 8-bit additive checksum, replies addressed to host `0x0011`.

| Purpose | Frame (hex) | Source |
|---|---|---|
| Line sync | `FF`, repeated until the reply `0D` | `FUN_18002C430` |
| Gateway axis-ID query | `08 00 00 B0 00 00 01 00 00 B9` | `FUN_18003F300`, reconstructed from the instruction stores (meaning Probable) |
| Read `CPOS` (32-bit, `0x029E`), axis *n* | `08 \| 00 n0 \| B0 05 \| 00 11 \| 02 9E \| cs`, e.g. X: `08 00 10 B0 05 00 11 02 9E 7E` | port-open probes |
| Read `IVERSION` (16-bit), axis *n* | `08 \| 00 n0 \| B0 04 \| 00 11 \| addr \| cs`; Y: `…03 EF…`, Z: `…03 E2…` | `Y/ZAxisGetScriptVersion` (slots 16, 17) |

**Per-axis variable addresses** (from each `variables.cfg`):

| Axis | `ESTATEV` (state) | `IINITONCEDONE` (12345 once homed) | `IVERSION` | `CPOS` / `APOS` |
|---|---|---|---|---|
| X, Y | `0x03EA` | `0x03FE` | `0x03EF` | `0x029E` / `0x0228` |
| Z | `0x03B2` | `0x03F6` | `0x03E2` | same |
| F | `0x03B6` | `0x03F8` | `0x03BC` | same |
| O, P | `0x03B2` | — (no power-up homing) | `0x03E8` | same |

- **Readiness check.** Reading `ESTATEV` = 0 and `IINITONCEDONE` = 12345 would show that power-up homing has finished before any position is trusted.
- **Version check.** `IVERSION` is exactly what PearlScope compares with `MotorFirmware.ini`, so reading it would also tell us whether PearlScope 1.6 would have rewritten our drives.

**Cross-check of units and calibration fields (local):** the stored `mcba` position limits converted with these units give travel spans of **X 119.1 mm, Y 78.1 mm, Z 8.4 mm**. That is plausible for a well-plate stage and a focus drive, and it supports the field names and both conversions together.

**Read-only first contact:** specified for review in [`fl1_v1.6_motor_gate1_spec.md`](fl1_v1.6_motor_gate1_spec.md).

### Units (superseded by the section above)
- **The formulas.** PearlScope converts with **2,880 counts per motor revolution**:
  - `FUN_140034180` returns `2880 / (YAxisMillimetersPerRevolution × 1000)` = counts per µm;
  - `FUN_1400D79E0` returns `round(864 / YAxisMillimetersPerRevolution)` = the counts for 0.3 mm (864 = 2,880 × 0.3), used by `YAxisMoveToPosAgainstSpring`;
  - the default mm/rev is 1.27.
- **The result for Y:** at 1.27 mm per revolution, **1 count = 1.27 mm / 2,880 ≈ 0.441 µm**.
- **Still to confirm:** whether X and Z use the same 2,880 (Z has 0.61 mm/rev).
- **Encoders.** The `setup.cfg` files confirm X, Y, Z and F are closed-loop with a motor-side incremental encoder. 2,880 counts per revolution would be a 720-line encoder read ×4 (Probable).
- **O and P** (objective turret, phase wheel) are **open-loop** steppers with no encoder (`IsStepperOpenLoop = 1`).

### What each drive does on its own at power-up (from the `.sw` images)

**Method.**
- **Layout.** Each `<axis>.sw` image was laid out by address: blocks of an address line then hex words. Program words start at `0x4000`, the header word is `0x649C`, and `_PROG_RUN_ADDR` = `0x4000`.
- **Annotation.** The words were annotated with the label and variable addresses from that axis' `variables.cfg`.
- **What can be read and what is inferred.** The control flow is readable from the address operands, so it is structural. The opcode meanings are **inferred** from the operands (no Technosoft encoding table was used):
  - `7400 a` = jump to *a*;
  - `7401 a` = call *a*;
  - `0404` = return;
  - `74xx v a` = conditional jump on variable *v*;
  - `21xx n` = assign *n* to the variable at `0x03xx`;
  - `2966 v` / `4966 n` = load *v* / subtract *n*.

**X and Y (stage), identical:**
1. `0x4000`: header, then `WAIT_VDC`, which loops until the supply-voltage flag allows.
2. **`0x400D: 21EA 0001` sets `ESTATEV` (`0x03EA`), the state variable, to 1.**
3. Jump to `MAINLOOP`, which dispatches on `ESTATEV`: 0 → `STATE_0_IDLE`, **1 → `STATE_1_INIT`**, then MOVE_REL, STOP, HOME, MOVE_POS, HALT, … SHIPLOCK, HOME_LSN …
4. **`STATE_1_INIT`:** if `IINITONCEDONE` ≠ 12345 (true after power-up), **call `INIT_ONCE`**; then the state goes back to 0 (idle).
5. **`INIT_ONCE`:**
   - call `VAR_INIT`;
   - `ILOOPCOUNT = 30000`;
   - **`STARTUPWAIT` only counts `ILOOPCOUNT` down to 0. It is a delay, not a wait for the host.**
   - then, depending on an input read into `VAR_I2` (most likely the negative limit switch), call **`_HOME_LSN_DOUBLE`** or **`_HOME_DOUBLE`**;
   - `INIT_HOME_DONE` sets `IINITONCEDONE = 12345` and returns.

**Z (focus) and F (cube carriage):** the same pattern. The initial state is set to 1 at `0x400F` (Z, variable `0x03B2`) and `0x400D` (F, variable `0x03B6`). `STATE_1_INIT` → `INIT_ONCE` → `STARTUPWAIT` → `STARTUPWAITDONE` → **`_HOME_DOUBLE`**, using `FINDLSN` and `HOMEZ_LSN` (Z) or `HOMEEPI_LSN` (F).

**O (objective turret) and P (phase wheel):** the initial state is set to **0**. Their `STATE_1_INIT` only resets the state to idle; no homing is called. These open-loop axes find position later, in `STATE_2_FIND_POS`, when commanded.

**Conclusion** (Strongly supported for the control flow; the opcode semantics are inferred):
- **Whenever the EVOS is powered on, the X, Y, Z and F drives start homing by themselves after a short delay, with no PC involvement.** They home towards the limit switches and the index, double homing, with a hard-stop variant (`HOMETOHARDSTOP`) in the program.
- **The objective turret and phase wheel stay still.**
- **Physical confirmation (2026-11-13):** the operator saw the X/Y stage move by itself after power-on; the focus (Z) and the cube carriage (F) were not watched. Motor Gate 1 then read `IINITONCEDONE` = 12345 and `CPOS` = 0 on X/Y/Z/F (gate spec §13). **Verified.**

### 32-bit values on the wire: low word first, each word big-endian (Verified from vendor code, 2026-11-13)

**Reads.** Every vendor routine that reads a 32-bit variable (opcode `B005`) receives the full 15-byte reply. `FUN_1800333b0` copies it, `O` included, into the caller's buffer, which then combines bytes 10–13 as

`value = b12<<24 | b13<<16 | b10<<8 | b11`   (`CONCAT31(CONCAT21(CONCAT11(b12,b13),b10),b11)`)

The same expression appears in four independent routines:
- `FUN_18003AD70` `XYStageXAxisGetControllerTargetPos`, which sends exactly Gate 1's X `CPOS` frame `08 00 10 B0 05 00 11 02 9E 7E`;
- the generic `FUN_1800367F0`;
- `GetXAxisPositionLimits` `FUN_18002C850`;
- the reads of `CPOSCMD`/`CSPDCMD`/`CACCCMD` in the drive-reset path, after `FUN_180029A90`.

So **bytes 10–11 are the low word and 12–13 the high word, each big-endian.** In Gate 1's evidence, this is the `i32_word_swapped` decode.

**Writes.** `SetXAxisPositionLimits` (`FUN_180034EB0`) packs a 32-bit value in the same order. The frame is `08 | 00 10 | 25 B4 | v[15:8] v[7:0] | v[31:24] v[23:16] | cs`: a 32-bit immediate write to `0x03B4` (`LPOSMIN`), then `25 B6` to `0x03B6` (`LPOSMAX`). Each write is followed by a `B005` read-back of the same variable. *Documented here; OpenEVOS builds no write frame.*

**Side findings (for the later stop/move review; Strongly supported):**
- **Position limits are stored in the drives.** They are written to the user variables `LPOSMIN`/`LPOSMAX` and read back from there. Whether the `.sw` program enforces them is still to be checked.
- **The PC also checks targets.** `XYStageXAxisMoveToPos` (`FUN_18003B0A0`) rejects a target outside the cached `LPOSMIN`/`LPOSMAX`, or a speed or acceleration outside the cached ranges. It then only queues the move for a worker thread; the move frames are sent elsewhere, in a routine not yet decompiled.
- **`CPOS` is the controller's target position.** The vendor method that reads `0x029E` is named `GetControllerTargetPos`; the start-up path also reads `CPOSCMD` (`0x03D4`), `CSPDCMD` (`0x03D6`) and `CACCCMD` (`0x03D8`).

### Stop, halt and move: how the vendor commands motion (decoded offline, 2026-11-13; nothing sent)

**Model (Strongly supported).** For X/Y/Z/F the PC never sends raw motion instructions. The objective turret is the exception: it is moved with raw TML (see below). It writes parameters into the drive program's user variables, then requests a state by writing **`ESTATE`**. The drive's `.sw` state machine does the rest; the PC follows progress through `ESTATEV`. Write frames have no sender field and get no data reply. *Correction (2026-11-13): they are **not** fire-and-forget. The common exchange routine `FUN_1800333B0`, called with "no reply expected" (`param_4` = 0), still reads exactly **1 byte** and treats anything other than `O` (`0x4F`) as a link error, marking the connection failed. So every write is acknowledged by a single `O`, and a write tool must read and check it.*

Write encodings seen (the address lives in the opcode's low 9 bits plus a page bit):
- **16-bit immediate:** `06 | 00 n0 | 21 xx | value(2) | cs` writes `0x03xx`. Example: `06 00 10 21 B3 00 09 F3` = X `ESTATE` = 9.
- **32-bit immediate:** `08 | 00 n0 | 25 xx | lo-word | hi-word | cs` writes `0x03xx`.
- **Fixed-point** values (speeds, accelerations) use the same 32-bit form: the integer part in the high word, the fraction × 65,536 in the low word.

| Vendor method | What it sends (X shown; Y uses `00 20`) | Meaning |
|---|---|---|
| `XYStageStopXAxis` (`FUN_18003A7C0`) | `06 00 10 21 CD 00 01 05` (`IPENDINGMOVES` = 1); a 32-bit fixed write of `CACCSTOP` (`0x03E8`); then `ESTATE` = 6 | Controlled stop: set the stop deceleration, request **`STATE_6_STOP`**. It also purges queued moves on the PC |
| `XYStageXAxisCoastToStop` (queued, type 0x11 → `FUN_180031E80`) | `06 00 10 21 F0 00 01 28` | `BCOASTTOSTOP` = 1: a flag the move states check (`STATE_8_MOVE_POS` clears it on entry) |
| `HaltAllMotors` (`FUN_18002DAA0`) | to each drive in turn (Z, F, X, Y, then P and O): `04 00 n0 00 02 cs`; then, for Z/F/X/Y, `ESTATE` = HALT (Z/X/Y **9**, F **12**, each `STATE_*_HALT` in that axis' program) | `00 02` is **`AXISOFF`** (Verified, see "Remaining stop/move items"): it switches off the power stage on all six |
| `XYStageXAxisMoveToPos` → queue type 1 → `FUN_180030510(pos, speed, accel)` | re-checks the PC-side ranges; then, **only if changed since the last move**: `CPOSCMD` (`25 D4`, 32-bit counts), `CSPDCMD` (`25 D6`, fixed), `CACCCMD` (`25 D8`, fixed); then `ESTATE` = 8 | Request **`STATE_8_MOVE_POS`** |

**What the drive does with a move (X `.sw`, `STATE_8_MOVE_POS` at `0x4350`; Strongly supported):**
1. Clears `BCOASTTOSTOP`.
2. Loads `CPOSCMD`, **calls `CLAMPTOLIMITS`** (`0x4405`), which bounds the target to `LPOSMIN` ≤ target ≤ `LPOSMAX`, and uses the clamped value as the new `CPOS`.
3. Loads `CSPDCMD`/`CACCCMD`, issues `AXISON`/`UPD`, and moves to `ESTATEV` = 13 (`STATE_13_MOVEPOSIDLE`), which waits for the move to complete.

**So position limits are enforced inside the drive, as well as on the PC.**

**Safety consequences for any future OpenEVOS motor work:**
- A **STOP** (controlled stop) is two writes and a state request on one axis. **HALT** switches all six drives off (Probable) and parks the four state machines in HALT. Leaving HALT goes through `STATE_*_UNHALT`, not yet decoded.
- **The drive's own `LPOSMIN`/`LPOSMAX` clamp is a hardware-side backstop.** Its values are the `mcba` limits PearlScope writes at start-up. OpenEVOS must read them back and check them before any move tool exists.
- Moves are **asynchronous**: completion means `ESTATEV` returning to idle, as read by Gate 1. The PC also caches the last speed, acceleration and target and skips unchanged writes, so a fresh connection must not assume the drive's `CSPDCMD`/`CACCCMD`.
- **None of these frames is built by OpenEVOS.** They are documentation for a future, separately reviewed stop-first tool.

### Remaining stop/move items (decoded offline, 2026-11-13; nothing sent)

**`00 02` is `AXISOFF` (Verified from the drive program).** In X's `.sw`, `_OSCDETECTED` (`0x416D`) branches over a single word `0002` to a label the vendor named **`_OSCNOAXISOFF`**. `STATE_11_UNHALT` begins with `0102`, i.e. `AXISON`. So `HaltAllMotors` really does **switch off the power stage of all six drives** before parking X/Y/Z/F in HALT.

**STOP, HALT and UNHALT inside the drive (X `.sw`; Strongly supported):**
- **`STATE_6_STOP`:** load `CACCSTOP` as the deceleration, set the new target to the current actual position (`LTARGETPOSV` = `APOS`), `UPD`. Then it clears the counters and returns `ESTATEV` to 0 (idle). This is a controlled deceleration, and the power stage stays on.
- **`STATE_9_HALT`:** clear `BCOASTTOSTOP`, load `CACCSTOP`, instruction `01C4` (Probable: TML `STOP`), set `CPOS` = `APOS`, then `ESTATEV` = 10.
- **`STATE_10_HALTED`:** stays halted until the host writes `ESTATE` = **11**.
- **`STATE_11_UNHALT`:** `AXISON`, clear `BHOMEINTERRUPT`, `ESTATEV` = 0.
- **Recovery after `HaltAllMotors`** is therefore `ESTATE` = 11 per axis on X/Y/Z, with `AXISON` coming from the drive itself. F's numbering differs (`STATE_12_HALT`, `13_HALTED`, `14_UNHALT`), and O/P only received `AXISOFF`.

**Drive-side clamps (Verified by label and code):**
- X and Y use `CLAMPTOLIMITS` in both `STATE_8_MOVE_POS` and `STATE_5_MOVE_REL`. A relative move clamps `APOS + CPOSCMD`.
- Z uses it in `STATE_8_MOVE_POS` (`CLAMPTOLIMITS` at `0x4321`), and F in `STATE_2_GOTO_POS` (`0x44E0`).
- **O and P have `LPOSMIN`/`LPOSMAX` variables but never reference them:** no clamp.

**Variable addresses differ per axis.** For example, `CPOSCMD` is at `0x03D4` on X/Y but `0x03B3` on Z; `LPOSMIN` is at `0x03B4` on X/Y, `0x03BD` on Z/O/P and `0x03EA` on F. Every frame must be built from that axis's own `variables.cfg`, never copied from another axis.

**PC-side positioning methods:**

| Method | Behaviour |
|---|---|
| `XYStageXAxisMoveToPos` (queue type 1) | PC checks the target, speed and acceleration ranges; then `CPOSCMD`/`CSPDCMD`/`CACCCMD` if changed; then `ESTATE` = 8 |
| `XYStageXAxisMoveRelative` (queue type 7 → `FUN_180030F10`) | **No PC-side range check:** it writes the delta into `CPOSCMD`, then `ESTATE` = 5 (`STATE_5_MOVE_REL`). Only the drive's clamp bounds it |
| Y absolute (`FUN_180030B70`) | as X with dest `00 20`; `06 00 20 21 B3 00 08 02` |
| `FAxisMoveToPos` (`FUN_180028E20`) | checks speed and acceleration against constants, **not the position**; writes 32-bit `LTARGETPOS` (`25 B7` → `0x03B7`), then `06 00 50 21 B5 00 02 2E` (`ESTATE` = 2, `STATE_2_GOTO_POS`). The drive clamps the target. Completion is polled from F's 16-bit status `0x03D2` (bit flags) |
| `OAxisMoveToRelativePos` (`FUN_180030160`) | Reads O `ESTATEV` (`0x03B2`) and refuses unless it is 0. **Then raw TML, bypassing the state machine:** `06 00 60 2C 9E 02 B2 E4` (`CPOS` = `TPOS`), `08 00 60 3C 9E lo hi cs` (`CPOS` += delta), `04 00 60 01 02 67` (`AXISON`), 500 ms, `04 00 60 01 08 6D` (`UPD`, start). **No limit on PC or drive.** Completion is polled from `0x03DE` = 0 |
| `OAxisMoveToPos`, `PAxisMoveToPos`, `PAxisMoveToRelativePos` | stubs returning `E_NOTIMPL` (`0x80004001`) |
| Z (`ZAxisMoveToPos`, `ZAxisMoveToRelativePos`) | not decompiled; the method layout mirrors X |

**Consequences for a future stop-first tool:**
- The **controlled stop** for X/Y/Z is `CACCSTOP` + `ESTATE` = 6. Its effect is limited to one axis, and the power stays on.
- **HALT** cuts power on all six drives and needs `ESTATE` = UNHALT afterwards. A sagging or freewheeling axis after `AXISOFF` would be a hazard to assess.
- **The objective turret is the least protected axis.** It has no limits anywhere, and moves are raw TML. Keep it out of any early move tool.
- Relative X/Y moves have **no PC-side check**, so any OpenEVOS move tool must add its own, and not rely on the vendor's pattern.

### Move completion, status and units (decoded offline, 2026-11-13)
- **After a move, X sits in `ESTATEV` = 13** (`STATE_13_MOVEPOSIDLE`), not 0. It checks convergence and coast-to-stop there.
  - From 13, `ESTATE` = 8 goes to `MOVEPOSAGAIN` and `ESTATE` = 5 to `MOVERELAGAIN`; both are clamped.
  - Any other request is taken as a new state.
  - `ESTATE` = 6 (stop) returns it to 0.
- **The vendor's "is moving" test** (`XYStageXAxisIsMoving`) reads `SRH` (`0x090F`): moving while bit 9 is clear.
- **Convergence:** `CHECK_CONVERGE` compares \|`APOS` − `LTARGETPOSV`\| with `LCONVERGEBAND` (`0x03CF`).
- **Units** (constants read from the DLL):
  - speed is sent as value × **0.0008**: a 0.8 ms sampling period, with speed in counts/s. This is **Strongly supported**: in Gate 5, X stopped at 4.80 mm after ≈ 0.58 s, matching the predicted ≈ 8.3 mm/s for the power-up `CSPDCMD` = 15;
  - acceleration scale 1/3,600,000 (not yet explained);
  - the vendor's stop deceleration constant = **1.0**;
  - F's speed range is 0.001–100 and its acceleration range 0.001–0.1.

### Where 115,200 baud comes from (partly settled, 2026-11-13)

- **Not the motion programs (Verified):** no `.sw` image contains the TML instruction `SCIBR = n` (opcode word `0x0820`, the one the vendor sends as `06 00 10 08 20 00 04`), nor a reference to `SCIBR_table` (`0x098D`).
- **Not `setup.cfg` (Verified):** each `.t.zip` holds only `setup.cfg` (drive and motor description: `ProductID P035.001.E102`, `FirmwareID F443I`, ports, homing modes) and `variables.cfg`. Neither has a baud setting.
- **The `.sw` images also load a setup table** (`0x5E54`, 348 words, for X/Y/Z/F; `0x5E89`, 295 words, for O/P; a pointer to it at `0x5FFF`) and an identity block (`0x5FBF`, the ASCII text "P035001E102F443I"). The startup baud rate is **Probable** a field of that setup table, but its layout is not decoded, so the field cannot be named.
- **Autobaud cannot be ruled out offline, but it fits the evidence poorly.** Rev 1's 9,600 attempt got no answer. That fits a fixed 115,200, but also a drive that needs DTR/RTS, because rev 1 had them disabled. The two cannot be separated from what we have.
- **Practical consequence (Verified by the Gate 1 run):** after power-up the drives answer at 115,200. PearlScope's connect succeeds on its first pass and never sends its baud-change write.

## 4. Safety consequences

- **`TS_DownloadSwFile` is firmware-class (ADR-0009).** It overwrites a drive's program and configuration. Its name is now **banned** in `safety/firmware.py`, alongside setup-save variants, and covered by `tests/test_firmware_safety.py`. OpenEVOS will never download `.sw` files to the drives.
- **Motion can come from the drives themselves.** Because the drives run EEPROM programs with initialisation and homing states, **opening a channel or initialising a drive may trigger motion**: homing to limit switches or to a hard stop. **Unknown** for now:
  - whether homing starts at power-up, at `TS_DriveInitialisation`, or only on an explicit host command (`STARTUPWAIT` suggests the drive waits for something);
  - what `MotorController64.dll` sends right after opening the channel.

  **No motor-controller connection is to be attempted until both are settled offline.**

- **Power-up homing is outside OpenEVOS's control.** Powering the EVOS homes X, Y, Z and F (§3a). So:
  - **power the instrument on only with the stage clear** (no vessel that could collide during homing; the objective path free), as with the vendor software;
  - a first OpenEVOS contact must come **after** homing has finished, and must not assume the axes are where they were left.
- **Rules for any future OpenEVOS motor backend** (derived from §3a):
  1. Never call `UpdateMotionControllerFirmware` or the per-axis update, and never send TML containing `spi` writes, `RESET` or `ENDINIT`. These change or restart the drive programs.
  2. Never use `MotorControlIsSupported`, or any port open, as a harmless probe: it talks to the drives.
  3. The first connection must be a reviewed L3 read procedure, like the LED board's Gates, after the drive's power-up behaviour is known.

## 5. Units, for the calibration schema

The `mcba` limits and carriage positions and the `moco` offsets and registration are drive-side **position counts**. Converting them to micrometres needs:
- the encoder resolution (lines per revolution, counted ×4);
- and possibly a microstep factor;

together with `X/Y/ZAxisMillimetersPerRevolution`. These values are still to be located: in the `.sw` setup block or in `MotorController64.dll`'s conversion code.

## 6. Next offline steps

1. ~~Decompile the start-up path~~ **Done (§3a).** Still open there:
   - ~~decode the bytes sent at port open~~ **Done (§3a, "What connecting sends").** Still to settle: the exact variable read in step 2, and the meaning and persistence of opcode `0x0801`;
   - find which method PearlScope calls first;
   - ~~check whether PearlScope updates the drive firmware automatically~~ **Yes, at start-up, for any axis whose version is lower (§3a).**
2. ~~Decode what a drive does at power-up~~ **Done (§3a):** X, Y, Z and F home on their own at power-on; O and P stay idle. ~~Confirm by watching the next power-on~~ **Seen and read back (2026-11-13).**
4. ~~32-bit word order~~ **Done (§3a): low word first, each word big-endian.**
5. Startup baud: **partly done (§3a).** Still open: decode the setup table at `0x5E54`/`0x5E89` to name the baud field.
6. ~~Decode the stop, coast, halt and move frames and the `.sw` limit check~~ **Done (§3a, "Stop, halt and move" and "Remaining stop/move items").** Still open: decode Z's PC-side move methods and the F/O status bit meanings; confirm `01C4` = TML `STOP`; decode the setup table (baud field).
3. ~~Find the counts-per-revolution constant~~ **2,880 counts/rev for Y (§3a).** Still open: confirm X and Z, and then apply the units to the calibration schema's position fields.
