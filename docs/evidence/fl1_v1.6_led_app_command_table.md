# FL Auto 1.6: LED application HID command table (RE-4, L0 static analysis)

- **Date:** 2026-11-13
- **Level:** L0. Offline analysis of files already on disk. Nothing was executed, registered or sent to the instrument.
- **Input:** `EvosGenIII_LEDBoard64.dll`, carved from `PearlUpdate_Rev1.6_31201.amg`; SHA-256 matches `fl_auto1_v1.6_inventory.json` (Verified, [LED static analysis §5](fl1_v1.6_led_board_static_analysis.md)).
- **Method:**
  1. `IGen3LedBoard` method names and slots from the DLL's embedded type library (`typelib_ledapp.txt`, §5.2 of the LED analysis).
  2. The `CComObject<CGen3LedBoard>` vtable located through MSVC RTTI (pefile only): 174 slots, of which 3–154 are `IGen3LedBoard`.
  3. Ghidra 12.2 headless, read-only project (`-readOnly -noanalysis` on the existing analysed program). A script decompiled every slot function plus its callees (depth 2) that call the transfer helpers, and the command byte was read from:
     - `FUN_18003dd70(this, reply, cmd, expectedReply, req)` (frame builder, used by most setters), 3rd argument;
     - `FUN_18003dde0(this, reply, cmd, expectedReply)` (bare-command get), 3rd argument;
     - direct `FUN_18003e1c0(this, req, reply, expectedReply)` calls: the request literal `local_b8 = 0xNN00` (byte 1 = `NN`) and the expected reply code (4th argument, always `NN | 0x80`).
  4. Non-`Sync` setters do not send: they push a 24-byte message onto a queue (`FUN_1800405b0`, `this+0x1D8`) for a worker thread. Their command byte is the literal in the queued message.
- **Kept:** method names, command bytes, byte offsets. No vendor code is reproduced.
- **Evidence labels:** Verified / Strongly supported / Probable / Unknown, as in the motor gate specs.

## 1. Framing (unchanged, Verified)

65-byte HID reports; byte 0 = report ID 0, byte 1 = command, byte 64 = two's complement of the 8-bit sum of bytes 1–63. The reply echoes `command | 0x80`; `0xF0` = busy. Proven on the board by Gate 2 (ADR-0013).

## 2. Command table

Class: **R** read-only query · **RAM** changes volatile lighting / mode state (lost at power-off; Probable) · **P** persistent memory write · **FW** firmware / bootloader · **H** host-only (no traffic).

| Cmd → reply | Class | Vendor methods (slot) | Request payload | Rating |
|---|---|---|---|---|
| `0x40` → `C0` | R | `GetFirmwareVersion` (3), `GetBoardType` (4), `GetPersistentData2AddressRange` (132) | none | Strongly supported (layout Verified for version, §5.3) |
| `0x47` → `C7` | R | `get_FilterCube` (52), `get_FilterCubePositions` (56), `GetFilterCubeTypeByPosition` (57), `GetFilterCubePosition` (59), `GetFilterCubeVoltageByPosition` (63), `get_FilterCubeVoltage` (64), `GetFilterCubeRawHf` (55), `get_ObjectiveLens` (76), `GetObjectiveRawHf` (79), `get_PhaseRingPosition` (141), `GetPhaseRingRawHf` (142); also polled by the `*Drain` setters (54, 78, 144) | none | Strongly supported |
| `0x4A` → `CA` | R | `get_EpiEnable` (86), `GetEpiDisabledDueToDisengaged` (88), `get_EpiDigipotStatus` (89), `get_EpiDigipot` (90), `GetEpiDigipotRange` (93), `GetEpiDigipotHardwareValue` (94), `GetEpiDigipotHardwareValueRange` (97), `get_EpiCurrentMode` (98), `get_PWMPeriodEpi` (100), `GetPWMPeriodRangeEpi` (102), `get_PWMWidthEpi` (103), `GetPWMWidthRangeEpi` (105) — all via helper `FUN_180037ca0` | none | Strongly supported |
| `0x5A` → `DA` | R | `GetEpiMode` (72), `GetNumEpiFixedOutputChannels` (73), `GetEpiFixedOutputChannelState` (74) | none | Strongly supported |
| `0x45` → `C5` | R | `get_PWMEnable` (33), `get_PWMPeriod` (36), `GetPWMPeriodRange` (39), `get_PWMWidth` (40), `GetPWMWidthRange` (43), `get_CurrentMode` (45) (brightfield) | none | Strongly supported |
| `0x50` → `D0` | R (samples ADC) | `RefreshVoltageReadings` (60), `RefreshRawADReadings` (61): four cube-ID voltages / raw A/D | none | Probable read-only; "Refresh" may trigger a conversion |
| `0x5D` → `DD` | R (selector) | `get_SystemMode` (11), prism getters (13, 15, 17–19, 21), `get_PearlBrightfield*` (25, 27, 29), `EVOSv2BrightfieldDigipot` getters (48, 50), `GetCachedLedOffFilterCubeTypeByPosition` (58) | sub-selector, not yet decoded | Probable |
| `0x3C` → `BC` | R | `GetBleederResistorPresent` (30), `GetBleederState` (31) | byte 2 = "get fresh value" flag | Probable |
| `0x53` → `D3` | R | `get_LcdBacklight` (138), `GetLcdBacklightRange` (140) | none | Strongly supported |
| `0x51` → `D1` | R | `GetPersistentData` (106); indirectly 108, 110, 112, 114, 116, 118, 120, 122, 123, 128, 130 | BE24 addr, count | Verified (Gate 2 Step C) |
| `0x31` → `B1` | R | `GetPersistentData2` (133), `GetPersistentData2Vector` (135) | BE24 addr, count | Verified (Step D) |
| `0x59` → `D9` | R | `GetPersistentDataChecksum` (131), `GetPersistentDataSection2Checksum` (137) | none | Verified (Step B) |
| `0x41` | RAM | `put_PWMWidth` (41, queued), `PWMWidthSync` (42) | BE16 value | Strongly supported |
| `0x42` | RAM | `put_PWMPeriod` (37, queued), `PWMPeriodSync` (38) | BE16 value | Strongly supported |
| `0x43` | RAM | `put_PWMEnable` (34, queued), `PWMEnableSync` (35) | value | Strongly supported |
| `0x44` | RAM | `put_CurrentMode` (46, queued), `CurrentModeSync` (47) | value | Strongly supported |
| `0x46` | RAM | `SetBrightfieldLighting(enable, period, width, currentMode)` (51, queued) | packed | Strongly supported |
| `0x49` | RAM | `put_EpiEnable` (87) — **turns the epi LED on/off** | byte 2 = enable | Strongly supported |
| `0x4B` | RAM | `SetEpiDigipotHardwareValue` (95, queued), `…Sync` (96) | value | Strongly supported |
| `0x4C` | RAM | `put_EpiDigipot` (91, queued), `EpiDigipotSync` (92) — **epi intensity** | value | Strongly supported |
| `0x4D` | RAM | `put_EpiCurrentMode` (99) | value | Strongly supported |
| `0x4E` / `0x4F` | RAM | `put_PWMWidthEpi` (104) / `put_PWMPeriodEpi` (101) | BE16 value | Strongly supported |
| `0x5B` | RAM | `SetEpiMode` (71), `SetEpiFixedOutputChannelState` (75) — **channel select** (bit mask `02/04/08/10` for channels 1–4 + state) | mask, state | Strongly supported |
| `0x54` | RAM | `put_LcdBacklight` (139, queued) | value | Strongly supported |
| `0x48` → `C8` | RAM / R (mixed) | simulated cube / objective / phase-ring get (65, 68, 69, 80, 82, 145, 147) **and** set (66, 67, 70, 81, 83, 146, 148) | sub-selector | Probable; refuse as a whole |
| `0x60` → `E0` | RAM / R (mixed) | `LightingEnableDelay` get (6) and put (7, sub-byte `0x08` + BE16 value) | sub-selector | Probable; refuse as a whole |
| `0x5E` → `DE` | RAM, **possibly P** | `put_SystemMode` (12), prism setters (14, 16, 26, 28, 49), `HardwareReCacheFilterCubeTypes` (62), **`SetPrismZEncoderZeroPoint` (20)** | sub-selector | Unknown persistence (encoder zero point may be stored) — treat as P |
| `0x52` | **P** | `SetPersistentData` (107); indirectly every `Set*HardwareSpecificData`, `SetSerialNumberString`, `SetHardwareIniDataStored`, `put_Optics*`, `put_*Type` | addr, data | Strongly supported — banned (ADR-0009) |
| `0x32` | **P** | `SetPersistentData2` (134), `SetPersistentData2Vector` (136), `SetHardwareSpecificData` (124) | addr, data | Strongly supported — banned |
| `0x33`–`0x36` | **FW** | `DownloadBootloaderHex` (149) | hex records | Strongly supported — banned |
| — | H | `ResetToBootloader` (8) and `SetBleederState` (32) are stubs that return `S_OK` and send nothing; `CommDebugFlags`, `MessageDrainPollTimeMs`, `*Drain` getters, `BootloaderUpdateProgressDrain`, `Get/SetUpdatedBootloaderVersion`, `ShowFlashTestDialog` | — | Strongly supported |

Slots 22–24 (`PearlBrightfieldEnable`) showed no transfer call within depth 2; Unknown.

## 3. What does not exist: LED temperature and measured current (RE-4 question)

- **No method in `IGen3LedBoard` reads an LED temperature or a measured LED current** (Verified against the type library: 149 methods, none named for temperature, current measurement or thermal state).
- `CurrentMode` / `EpiCurrentMode` are drive-mode **settings** (read back via `0x45` / `0x4A`), not measurements.
- The only analogue readings are the four filter-cube identification voltages (`0x47` bytes 11–16 as voltage, `0x50` raw A/D).
- **Consequence for M8:** LED linearity and stability must be measured optically (power meter at the stage plane, or camera mean intensity), not from board telemetry. Whether the firmware has an unexposed thermal query is Unknown (the application hex in the package is a different build, §5.6 of the LED analysis).

## 4. Reviewed read set for LED Gate 3 (`led_status_read.py`)

Four bare requests: byte 1 = command, bytes 2–63 = 0, byte 64 = checksum. Reply offsets are the bytes the vendor's readers copy (decompiled locals relative to the reply buffer). All decoded values are **CANDIDATE**.

| Request | Reply byte(s) | Field | Vendor reader | Rating |
|---|---|---|---|---|
| `0x40` | 2–5 / 6–9 / 10–13 (BE32) | application major / minor / build | slot 3 | Verified layout (§5.3); value 3.0.25949 expected |
| `0x40` | 17–24 | section-2 address range | slot 132 | Strongly supported |
| `0x47` | 2 | current filter-cube position | `get_FilterCube` | Strongly supported |
| `0x47` | 3 | objective position | `get_ObjectiveLens` | Strongly supported |
| `0x47` | 4 | number of cube positions | `get_FilterCubePositions` | Strongly supported |
| `0x47` | 11–16 | cube-ID voltage (vendor formula) | `get_FilterCubeVoltage` | Probable |
| `0x47` | 25–28 | cube type at positions 1–4 (**cube identity**) | `GetFilterCubeTypeByPosition` | Strongly supported (byte → cube enum still to map) |
| `0x47` | 31 | filter-cube position (alternate reader) | `GetFilterCubePosition` | Strongly supported |
| `0x47` | 32 | phase-ring position | `get_PhaseRingPosition` | Strongly supported |
| `0x5A` | 2 | epi mode | `GetEpiMode` | Strongly supported |
| `0x5A` | 3 | number of epi fixed-output channels | `GetNumEpiFixedOutputChannels` | Strongly supported |
| `0x5A` | 4–7 | epi channel 1–4 on/off state | `GetEpiFixedOutputChannelState` | Strongly supported |
| `0x4A` | 3 | epi digipot status (also cached by the DLL) | `get_EpiDigipotStatus` | Strongly supported |
| `0x4A` | 4 | epi digipot (**intensity**) | `get_EpiDigipot` | Strongly supported |
| `0x4A` | 5 / 6 | digipot range min / max | `GetEpiDigipotRange` | Strongly supported |
| `0x4A` | 7–8 (BE16) | epi current mode | `get_EpiCurrentMode` | Strongly supported |
| `0x4A` | 9–10 / 11–12 (BE16) | epi PWM width / period | `get_PWMWidthEpi` / `get_PWMPeriodEpi` | Strongly supported |
| `0x4A` | 21 | epi digipot hardware value | `GetEpiDigipotHardwareValue` | Strongly supported |
| `0x4A` | 24 | epi disabled because disengaged | `GetEpiDisabledDueToDisengaged` | Strongly supported |

**Why these four are read-only (Strongly supported, host side):** each vendor method that sends them builds the bare command, makes one exchange and copies reply bytes into out-parameters or a host cache; no setter shares these command bytes, and no request payload exists that could select a write sub-function. The vendor polls `0x47` continuously from its drain threads, so the firmware sees this request routinely.

**Not provable offline:** the firmware handler side for our build (25949). The package hex is build 27103 (§5.6 of the LED analysis); its handlers have not yet been traced.

**Rules (as Gate 2):** one write per command, 500 ms write limit, at most 5 passive reads within 1 s, flush before each write; BUSY, timeout or short write stops the whole run; never resend; dry run by default; exact confirmation phrase; preconditions as Step B (single `0051`, no `0057`/`0052`, StartApp record for this interface).

## 4a. Gate 3 run (2026-11-13, 15:45 UTC, operator-approved, once)

Sequence: Gate 1 `0x0A` (valid, 3.0.25949) → StartApp `0x08` (application after 1.0 s) → Gate 3 dry run → one live run. All four replies valid on the first read, no BUSY. Board afterwards: still `0DAD:0051` only. Record: `openevos-scans/led-application-gate3-status-20261113T154527-complete.json`.

| Cmd | Decoded (CANDIDATE) |
|---|---|
| `0x40` | application **3.0.25949** (matches Gate 1) |
| `0x47` | cube position byte = 5, objective = 2, cube positions = 4, **cube type by position 1–4 = 5, 1, 3, 4**, alt cube position = 1, phase ring = 0 |
| `0x5A` | epi mode = 1; bytes 3–7 suspect (see below) |
| `0x4A` | epi digipot = 1, digipot range 0–31; current mode bytes 7–8 = `FF FD`, PWM width/period = 0 |

**Finding: the firmware reuses one reply buffer and does not clear it (Strongly supported).**
- Every reply carries, in its tail, the bytes `43 F3 … 65 5D` from the bootloader's `0x0A` reply (`0x655D` = 25949).
- The `0x47` and `0x5A` replies are identical from byte 3 onward; only byte 2 differs.
- So each handler writes only *some* bytes; the rest is residue of earlier replies. A byte is meaningful only if that command's handler writes it. **Rating changes:** `0x5A` bytes 3–7 (channel count and states) drop to **Unknown** — they equal the `0x47` bytes 3–7. `0x47` byte 2 = 5 with 4 positions, and `0x4A` current mode `0xFFFD`, need the vendor's interpretation before use.
- The vendor DLL reads `0x5A` bytes 3–7 too, so either the firmware fills them for other states or the vendor also reads residue; Unknown until the handlers are traced in the hex.

## 4b. Enum mapping of the Gate 3 reply (L0, 2026-11-13)

- **Source:** the DLL's embedded type library, read with `LoadTypeLibEx(REGKIND_NONE)` through oleaut32 (resource data only; nothing registered or run). Enums: `Gen3FilterCubeType`, `Gen3ObjectiveLensType`, `Gen3PhaseRingPosition`, `EpiModeType`, `EpiCurrentModeType`, `Gen3EpiDigipotStatusType`, among others. The vendor getters return these types directly. **Strongly supported:** the byte is the enum value, because the getters copy it unchanged.
- `Gen3FilterCubeType` (excerpt): 0 SHORT, **1 NONE**, 2 DISENGAGED, **3 DAPI**, **4 GFP**, **5 RFP**, 6 CFP, 7 YFP, 8 TEXAS_RED, 9 CY5, 10–19 QD525…QD_WB, 20 MCHERRY, 22 FITC, 23 BFP, 50 WHITE, 21–52 others.
- `EpiCurrentModeType`: 0xFFFE LOW, 0xFFFF HIGH, **0xFFFD DIGIPOT**.

| Field (byte) | Raw | Meaning |
|---|---|---|
| `0x47` cube type at positions 1–4 (25–28) | 5, 1, 3, 4 | **RFP, empty, DAPI, GFP** |
| `0x47` cube in the light path (2) | 5 | **RFP** (a type, not a position; this explains "5 of 4 positions") |
| `0x47` cube position (31) | 1 | position 1, which holds RFP: consistent |
| `0x47` objective (3) | 2 | **4X** (a lens type, as identified by the board) |
| `0x47` phase ring (32) | 0 | DISENGAGED |
| `0x5A` epi mode (2) | 1 | MOVEABLE_CARRIAGE (the FL Auto's F carriage) |
| `0x4A` digipot status (3) | 0 | OK |
| `0x4A` current mode (7–8) | `FF FD` | **DIGIPOT**: a real value, not buffer residue (corrects §4a) |
| `0x4A` digipot (4), range (5, 6) | 1, 0–31 | intensity setting 1 of 31 |

- **Consistency:** the cube-in-path type matches the type at the reported position, and the current mode is a valid enum value. Together with the vendor reading bytes 25–28 per position, this makes residue an unlikely explanation for these fields. `0x5A` bytes 3–7 stay Unknown.
- **Still to check physically (M5):** at the filter-cube access position (manual p.51), whether positions 1–4 hold RFP, nothing, DAPI and GFP. Then compare with the cube list in the section-2 calibration blob (`tpco` records).
- The decoder now adds these names (`led_status_read.py`, `*_name` fields). All values remain CANDIDATE until the physical check.

## 4c. Firmware handlers in the package hex (L0, 2026-11-13)

- **Input:** `LED Board App - EVOS Gen III.hex`, carved from the decoded payload by SHA-256 (`af93682f…ee2d0ecc`, Verified). Imported into a scratch Ghidra project as `PIC-24F:LE:24:default`, where addresses = PC. Linear-sweep disassembly, read-only. **This is build 27103; the board runs 25949.**
- **Buffers:** the request is at RAM `0x19BB` + *n* and the reply at `0x197B` + *n* (reply byte 1 = `0x197C`, byte 64 = `0x19BB`). The common exit (`0x34DC`) stores the reply code, computes the checksum (`0x238C`: negated sum of `0x197C..0x19BA`) and sends 64 bytes. **The reply buffer is never cleared** (Strongly supported: no clear loop on any path).
- **Dispatcher** (`0x2438`): handler = jump-table entry (`0x244C` + 2·(cmd − `0x30`)), for commands `0x30`–`0x61`; other values get `F1`. A busy check sends `F0`.

| Cmd | Handler (PC) | Reply bytes written | Conditional |
|---|---|---|---|
| `0x40` | `0x24B0` | 1–13 (version) and following | — |
| `0x47` | `0x2884` | **2–34** | 35–39 only if epi mode (byte 34) = 2 |
| `0x5A` | `0x2F28` | **2** (epi mode) | 3–7 (channel count, channel states) only if epi mode = 2 |
| `0x4A` | `0x2B3A` | **2–24** | PWM width/period (9–12) are 0 unless system mode = 1; current mode (7–8) is set only in system modes 1, 3 and 4 |

**Checked against the live 25949 replies (§4a): exact match.** The `0x47` reply ends at byte 35 and bootloader residue starts at 36. The `0x5A` reply (mode 1) differs from `0x47` only in byte 2. The `0x4A` reply's own data ends at byte 24, and byte 25 onward equals the `0x47` bytes. So the 27103 layout holds for these three handlers on our 25949 board (Strongly supported).

**Where `0x47`'s positions come from (RE-1, RE-2, M5):**

| Field | Firmware source | Raw code (live) |
|---|---|---|
| objective (byte 3) | `0x5500`: if the simulation flag `0x1A53` is set, a host-set value; otherwise a 3-bit code read from **input port SFR `0x02DA`** (`0x54D6`, reported raw in byte 29), mapped 5→1 (2X), 7→2 (4X), 3→3, 2→4, 4→5 | 7 → **4X** |
| cube position (byte 31) | `0x4394`: simulation flag `0x1885`, else a one-hot code from **ports `0x02E2` / `0x02EA`** (`0x42DE`, raw in byte 30), mapped 1/2/4/8 → 1/2/3/4 | 1 → position 1 |
| phase ring (byte 32) | `0x5582`: simulation flag `0x1A57`, else a code from `0x5564` (raw in byte 33) | 0 → disengaged |
| cube types 1–4 (bytes 25–28) | `0x4304`: simulation, else cached per-position types at RAM `0x1886..0x188D` (two sets; probably from the cube-ID voltages) | 5, 1, 3, 4 |

- **Conclusion (Strongly supported for 27103; Probable for our build):** the board reads the objective turret, the filter-carriage position and the phase ring from **hardware position-sensor inputs**. These are not values the host wrote earlier, unless simulation is on (`0x48`, never sent by OpenEVOS). After a reset the simulation flags are most likely clear (Probable: the reset initialisation was not traced).
- **For RE-1 / RE-2:** the O turret and the P wheel have absolute position codes that can be read with `0x47` without moving anything. A homing design can check its result against them.
- The objective "type" is only a mapping of the sensor code; which physical lens is mounted at each code is a calibration question (`tpco`).
- **Decoder change:** `led_status_read.py` decodes only bytes the handler writes (`written_bytes`), gives `None` for the conditional channel bytes outside epi mode 2, and adds the raw sensor codes.

## 4d. Physical cross-check (M1 inventory, user photos, 2026-11-13)

Relayed by the stage session. The cube's serial number is kept only in the git-ignored `openevos-scans/m1-inventory-photos-20261113.json`.

| Item | Board (`0x47`) | Seen physically | Status |
|---|---|---|---|
| Cube slot 1, in the light path, F at 0 counts | type 5 = RFP, cube position 1 | label "EVOS LED Cube, RFP", REF AMEP4652, Ex 531/40, Em 593/40 | **Verified** (photo) |
| Slot 2 | type 1 = none | empty bay (user, visual) | **Verified** (user-observed) |
| Slot 3 | type 3 = DAPI | DAPI cube (user, visual; label not read) | **Strongly supported** (user-observed) |
| Slot 4 | type 4 = GFP | not seen | Candidate |
| Objective in the light path | sensor code 7 → "4X" | a Plan 4x barrel (red band) is visible on the turret; the user's notes conflict on which position is in the light path | **Unresolved**, waiting for the user |
| Other turret positions | — | the user will list them; not inferred | Open |

- **Also verified by the same photo:** F = 0 counts puts carriage slot 1 in the light path.
- **Firmware decoding verified:** the `Gen3FilterCubeType` decoding (5 = RFP) and the cube-in-path field (byte 2).

## 6. LED Gate 4: brightfield and epi on/off (RAM/IO write; review 2026-11-13)

**Request layouts** (vendor DLL setters; the firmware reads request byte *n* at RAM `0x19BB` + *n*):

| Cmd | Payload | Firmware | Effect | Reply |
|---|---|---|---|---|
| `0x43` brightfield PWM enable | BE16 at bytes 2–3, 0 = off, ≠0 = on | `0x26DE` → `0x53A2` (system mode 1) | on: output-compare mode 6 (PWM) in SFR `0x0190`, set LATB12; off: mode 0, clear LATB12 | `C3`, bytes 2–3 = read-back (`0x52F6`) |
| `0x49` epi enable | byte 2 = `Gen3EpiEnableType` (0 off, 1 on, 2 PWM) | `0x2B0A` → `0x4A7E` | drives the epi LED pin (TRISB/LATB bit 8) and RAM state `0x1A14` | `C9`, byte 2 = enable (`0x46C8`), byte 3 = digipot status, byte 4 = digipot |
| `0x41` / `0x42` | BE16 width / period | `0x53C0` (width ≥ `0x28E`) / `0x53D2` (period `0x290`–`0x6000`) | output-compare registers `0x0196` / `0x010C` | **not used by Gate 4** |
| `0x44` | current mode | `0x5304`: LATB13 | brightfield high/low current | **not used** |
| `0x4C` | epi digipot | `0x47DC`: RAM `0x1A15` plus a bus write to the digipot chip | epi intensity | **not used**: whether the digipot chip has a non-volatile wiper is Unknown |
| `0x45` read | — | `0x273A` | bytes 2–3 enable, 4–5 PWM width, 6–7 PWM period, 8–9 high-current flag, 10–17 ranges | — |

- **No flash.** Program-memory writes (`TBLWT` with NVMCON `0x0760`) exist only in the routines at `0x37BC` and `0x55EA`, used by the persistent-data write handlers, which Gate 4 never sends. The `0x43` and `0x49` paths write only I/O registers and RAM (Strongly supported, build 27103; the handler layout was confirmed on 25949 in §4c).
- **Which epi LED lights:** in epi mode 1 (moveable carriage) the board has one epi drive. It lights the cube in the light path, slot 1 = RFP (Ex 531/40, green, not UV). The DAPI (near-UV) and GFP cubes only light after the F carriage moves them into the path. That is a motor operation for the stage session, outside Gate 4.
- **Tool:** `python -m openevos.maintenance.led_light_test` ([ADR-0034](../decisions/ADR-0034-led-gate4-light-test.md)):
  - Read `0x45`, `0x4A`.
  - Brightfield `0x43` on → hold (default 3 s, max 10 s) → off.
  - Epi `0x49` on → hold → off.
  - Read `0x45`, `0x4A`.
  - Each request once. OFF must read back 0. Any failure sends one OFF to every light that may be on; if that cannot be confirmed, the instruction is to power-cycle (that returns the board to its bootloader with outputs reset).
  - Phrases: checklist `NOBODY LOOKING INTO THE LIGHT PATH, OBSERVER PRESENT`, confirm `LIGHT TEST BRIGHTFIELD AND EPI ON 0DAD:0051`.

### 6a. Gate 4 run (2026-11-13, 17:35 UTC, operator-approved, once)

**Sequence:** Gate 1 (3.0.25949) → StartApp (application after 1.0 s) → Gate 4 dry run → one live run, 3 s hold. **Outcome `complete`:** every reply was valid on the first read.

| Step | Read-back |
|---|---|
| brightfield before | enable 0 |
| brightfield ON | 1 |
| brightfield OFF | 0 |
| epi before | enable 0 |
| epi ON | 1 |
| epi OFF | 0 |
| after | both 0 |

Record: `openevos-scans/led-application-gate4-light-20261113T173549-complete.json`.

**Correction: the board runs in system mode 3/4 (PEARL / EVOSv2), not 1 (Strongly supported).**
- The `0x45` reply has bytes 4–7 = 0 and bytes 8–9 = `0x0002`. That is the handler's mode-3/4 branch: current mode from `0x4B58`, and the mode-1 PWM fields are zeroed.
- So the switching went through the PEARL routines, not the mode-1 ones above:
  - `0x43` → `0x4C6A`: brightfield state machine (`0x4C30` on, `0x4C16` off; RAM `0x189A`, `0x18A4`);
  - `0x49` → `0x4BE8`: enable 1 / 0 → `0x4B6E`, 2 → `0x4B8A`; RAM `0x18A2`.
- No flash routine (`0x37BC`, `0x55EA`) appears in these routines. Their deeper callees (`0x4B6E`, `0x4B8A`, `0x56E6`) were not traced: **Probable** no persistent write. That is acceptable for an on/off test; an intensity gate must trace them.
- The mode-1 SFR description in the table above applies only to system mode 1.
- **What the observer saw (operator, 2026-11-13):** two lights in sequence: first a bright (white) light, the brightfield, then a green light, the RFP cube's excitation (Ex 531/40). **Verified:** OpenEVOS can switch the brightfield LED and the epi LED of the cube in the light path on and off, at the board's power-on default intensity.

### 6b. All four cube slots (2026-11-13, 18:17–18:20 UTC, operator-approved)

For each slot: the stage session moved F with Motor Gate 11 (ADR-0035). This session then ran Gate 3 (status) and Gate 4 (light test, 3 s holds) once each. COM3 was never opened here.

| Slot | F target (counts) | `0x47` cube sensor raw → position | Cube in the light path (board) | Gate 4 | Observer |
|---|---|---|---|---|---|
| 1 | 0 | 1 → 1 | RFP | complete (17:35) | white, then **green** (Verified) |
| 2 | 34000 | 2 → 2 | NONE | complete | as expected (no epi light) — Verified 2026-11-16 |
| 3 | 68000 | 4 → 3 | DAPI | complete | as expected (near-UV) — Verified 2026-11-16 |
| 4 | 102000 | 8 → 4 | GFP | complete | as expected — Verified 2026-11-16 |

- **Observer (user, relayed 2026-11-13):** lights were definitely seen during the filter-set run. The colour for each slot is **pending**; the user will report it and plans to repeat the test. Slots 2–4 colours are not verified.
- **Repeat run (2026-11-15, 19:30–19:33 UTC, user-approved):** F moved by the mirror-move session (Motor Gate 11) to slots 1–4.
  - Board sensor raw 1 / 2 / 4 / 8 → RFP / NONE / DAPI / GFP: identical to the first run.
  - Gate 4 complete at every slot; both lights confirmed off each time.
  - Records `led-application-gate4-light-20261115T1930..1932*-complete.json`.
  - **Observer (user, 2026-11-16): "As expected"** for all slots. White brightfield at every slot; epi green (slot 1, RFP), none (slot 2, empty), faint violet or nothing (slot 3, DAPI, near-UV), blue (slot 4, GFP). Per-slot epi switching is **Verified**.
- F returned to slot 1 afterwards (Motor Gate 11, APOS 0). The board sensor confirmed raw 1, RFP in the light path.
- **Board sensor and motor agree at every slot** (Verified). F slot pitch = 34000 counts.
- The one-hot sensor code (1/2/4/8) confirms the §4c decoding.
- Every on/off read back correctly, and both lights were off after every run.

## 7. Brightness in PEARL mode (L0 trace, 2026-11-16; not yet sent)

The board runs in system mode 3/4 (PEARL, §6a), so brightness goes through the PEARL routines, not the mode-1 PWM (`0x41`/`0x42`, which this mode ignores).

### 7.1 Requests (vendor DLL)

| Purpose | Request | Reply / read-back | DLL slot |
|---|---|---|---|
| Epi level set | `0x4C`, byte 2 = level | `0xCC`: byte 2 status, byte 3 read-back | 92 `EpiDigipotSync` |
| Epi level read | `0x4A` | byte 4 = level (mode 3/4 path `0x4CE4`); bytes 5/6 = range 0 / 31 | 90, 93 |
| Brightfield level set | `0x5E`, **byte 2 = `0x04` only**, byte 19 = level | `0xDE` | 28 `put_PearlBrightfieldDigipot` (`FUN_1800337B0`) |
| Brightfield level read | `0x5D` | byte 35 = level; bytes 36/37 = range | 27, 29 |

- **`0x5E` byte 2 is a field mask** (firmware `0x306E` tests the word at `0x19BC`, high byte = request byte 2). The other bits are not brightness:

  | Bit | Effect |
  |---|---|
  | 0 | changes the **system mode** |
  | 1 | current mode (request bytes 17–18) |
  | 2 | brightfield level (byte 19) |
  | 5 | prism Z-encoder zero point |
  | 7 | the cube **recache** routine: switches lights off and runs long waits |

- **Only `0x04` may ever be built.** Any other bit is refused before I/O.

### 7.2 Firmware (package build 27103)

- **Brightfield** `0x4CEA`:
  - stores the level in RAM `0x189E`;
  - only while brightfield is on (state `0x189A` == 1), also calls `0x4758`.
- **Epi** `0x4C86`:
  - stores the requested level in RAM `0x189C`;
  - **caps it at 10** when the cube check `0x434C` / `0x46F2` says so (probably the UV cube);
  - only while epi is on (state == 2), calls `0x4758`.
- **`0x4758`** is the one routine that sets the light level for both lights.
  - It clamps to 0–31.
  - It sends 4 bytes over the board's **I2C** bus (SFR `0x0200`–`0x0209`): address byte `0x50` (7-bit `0x28`, write), then `0x11`, then level « 3, then `0x00`.
  - On success it stores the level in `0x1A1B`.
- **No flash or EEPROM write** of the microcontroller is on these paths. The program-memory writes are only at `0x37BC` / `0x55EA` (§6).
- **Unknown: whether the I2C chip's write is volatile.** The part is not identified, and command byte `0x11` matches no known non-volatile write pattern. Some digital potentiometers and DACs have non-volatile wiper or EEPROM commands.
  - **Mitigation for a first live test:** read the level first, set a low level, switch on briefly, switch off, restore the original level, read back.
  - **After the next power cycle:** read the power-on default again. If it changed, the chip stored it.

### 7.2a LED Gate 5 brightfield run (2026-11-16, 17:51 UTC, operator-approved, once)

**Outcome:** complete; every reply valid on the first read. Record: `openevos-scans/led-application-gate5-brightness-20261116T175124-complete.json`.

| Step | Read-back |
|---|---|
| `0x5D` before | level **3**, range 0–31 |
| `0x5E`/`0x04` level 2 | `0x5D` reads 2 |
| `0x43` on | 1 |
| `0x43` off (after 3 s) | 0 |
| `0x5E` level 3 (restore) | `0x5D` reads 3 |

- **The layout is confirmed on the board** (Strongly supported): `0x5D` byte 35 = level, bytes 36/37 = range 0/31. Setting through the `0x04` field changes exactly that byte.
- **The power-on default brightfield level is 3 of 31.** The "bright" light seen in Gate 4 was level 3, so the usable range above it is large.
- **Repeat runs** (17:52 and 17:52 UTC): level 2 again, then **level 10**. Both complete; level 3 restored and read back each time.
- **Observer (user, 2026-11-16):** level 10 was **clearly brighter** than the earlier flashes at levels 2–3.
- **Verified:** the `0x5E`/`0x04` level drives the real brightfield intensity. The app's brightness slider can map 0–100 % → level 0–31 (a follow-up to ADR-0048).

### 7.2b LED Gate 5 epi run (2026-11-16, 18:10 UTC, operator-approved; F at slot 1 via Motor Gate 11)

**Outcome:** complete. Record: `led-application-gate5-brightness-20261116T181002-complete.json`.

| Step | Read-back |
|---|---|
| `0x4A` before | epi level **1** (power-on default), range 0–31 |
| `0x47` | cube in the path = RFP |
| `0x4C` level 2 | `0x4A` reads 2 |
| `0x49` on | 1 |
| `0x49` off (after 3 s) | 0 |
| `0x4C` level 1 (restore) | reads 1 |

- **Layout confirmed on the board:** `0x4A` byte 4 = epi level.
- Observer: pending.

### 7.2c Level-0 check (2026-11-17, 09:15 UTC, operator-approved, once each)

The app now treats slider 0 % as "on at level 0" (main session, ADR-0051). Gate 5 was run at level 0 for both lights:

| Light | Original level | Level 0 read-back | On / off | Restored |
|---|---|---|---|---|
| Brightfield | 3 | 0 | 1 / 0 | 3 |
| Epi (cube in path: GFP; F had moved to slot 4) | 1 | 0 | 1 / 0 | 1 |

- The board accepts level 0 for both lights and reads it back. Records: `led-application-gate5-brightness-20261117T091518-complete.json`, `…091523-complete.json`.
- **Repeated** at 09:17 UTC (records `…091735`, `…091741`), with the same read-backs.
- **Observer (user, 2026-11-17): "No light, dark"** in both windows. **Verified (visual):** level 0 gives no visible light for brightfield or the epi LED (GFP). The app's "on at level 0" for slider 0 % is dark.
- Not verified: sub-visual emission; a camera check at level 0 would quantify it.

### 7.3 LED Gate 5 (built: `led_brightness_test.py`, ADR-0049)

1. Read `0x5D` and `0x4A` and record both original levels.
2. Brightfield:
   - set level `L` (low, e.g. 2 of 31) with `0x5E`/`0x04`;
   - `0x43` on, hold, off;
   - restore the original level;
   - read back.
3. Epi (cube in the light path, never DAPI first):
   - `0x4C` level `L`;
   - `0x49` on, hold, off;
   - restore;
   - read back.
4. Each request once, OFF-on-failure, phrases, dry run. Levels 0–31 only.
5. Then the app's slider can map 0–100 % → 0–31, once Gate 5 has passed.

## 8. Camera-measured LED sweep (ADR-0050; built 2026-11-17, not yet run)

- **Tool:** `python -m openevos.maintenance.led_camera_sweep --light brightfield|epi --levels 1,2,3,5,8,10 --shutter-ms 10`.
- **What it reuses, unchanged:**
  - LED Gate 5's frames: levels 0–31 with read-back, on/off, cube check;
  - Camera Gate 2 mono's RAM-only settings: MONO16 1296×964, auto exposure off, gain 0 dB, brightness 2 %, shutter 1, 10 or 50 ms.
- **Sequence:**
  1. Read the LED level.
  2. Camera: snapshot, which must match its Gate 1 record, then apply the settings and read them back.
  3. Dark frame with the light off.
  4. Light on. For each level: set, read back, settle 0.2 s, discard 1 frame, keep 3.
  5. Stop at the first level with more than 0.1 % saturated pixels; no higher level is sent.
  6. Light off, LED level restored and read back, camera restored and verified, closed.
- **Limits:**
  - brightfield levels up to 20 and epi up to 10 (the app's caps);
  - epi on at most 60 s in total;
  - epi refused for an empty slot or DAPI.
- **Output:**
  - mean minus dark per level, whether it increases with every level, and a straight-line fit (R²);
  - one frame per level saved as `.npy` in `openevos-scans/`.
- **Relative only.** The sensor sees the light through the current objective, cube and sample, not optical power at the stage. M8's absolute and stability parts still need a power meter.
- **Light path matters:** the mono camera must actually see the light at the current F position. Earlier, the colour camera received light at the mirror and nothing at slot 1. A first run should check that the dark-to-level-1 difference is clearly above the noise.

### 8.1 First brightfield sweep (2026-11-16, 18:47 UTC, operator-approved, once; F at slot 1, RFP cube)

**Outcome:** `complete`. Light off, LED level 3 restored, camera restored and verified. Record: `openevos-scans/led-camera-sweep-20261116T184724-complete.json`; one `.npy` frame per level.

| Level | Mean | Mean − dark (1379.7) | Per level |
|---|---|---|---|
| 1 | 12158 | 10778 | 10778 |
| 2 | 23776 | 22396 | 11198 |
| 3 | 35379 | 34000 | 11333 |
| 5 | 58250 | 56870 | 11374 |
| 8 | 65520 | **clipped** | — |
| 10 | 65520 | **clipped** | — |

- **Brightfield output is linear in the level (Strongly supported, relative).** Over levels 1–5: slope 11520 counts per level, intercept −669, **R² = 0.99998**, residuals within ±0.7 %.
  - Light reaches the mono camera at slot 1 with the RFP cube in the path, at 10 ms.
- **Tool bug found and fixed:** the camera's MONO16 full scale is **65520** (12-bit << 4), not 65535.
  - Levels 8 and 10 were fully clipped, but the 65535 threshold did not flag them, so the sweep went on to level 10 instead of stopping at 8.
  - No harm to the instrument: the light was off and everything restored afterwards.
  - `SATURATION` is now 65520, with a test.
  - The run's own "monotonic False / R² 0.86" summary is invalid because it includes the clipped levels.
- **For a full-range curve:** use the 1 ms shutter, which reaches about 10× higher levels before clipping, or more levels between 1 and 5.

### 8.2 Full-range brightfield sweep at 1 ms (2026-11-16, 19:03 UTC, operator-approved, once; F at slot 1)

**Outcome:** `complete`, no saturation. Light off, level 3 restored, camera restored and verified. Record: `led-camera-sweep-20261116T190320-complete.json`.

| Level | 1 | 2 | 3 | 5 | 8 | 10 | 12 | 15 | 20 |
|---|---|---|---|---|---|---|---|---|---|
| Mean − dark (1379.4) | 1109 | 2318 | 3529 | 5932 | 9441 | 11720 | 13949 | 17209 | 22394 |
| Per level | 1109 | 1159 | 1176 | 1186 | 1180 | 1172 | 1162 | 1147 | 1120 |

- **Monotonic over the whole app range 1–20 (Verified, relative).** A straight line through all points gives R² = 0.9991.
- **Shape:**
  - Close to proportional in the middle: 1160–1186 counts per level over levels 2–15.
  - Level 1 is about 6 % low.
  - The top droops gently: level 20 is about 7 % below the low-range slope, a slight sub-linearity at high drive.
- **A useful slider calibration:** about 1.15 × 10³ counts per level at 1 ms, through the RFP cube at slot 1. The droop is small enough that the app's linear 0–100 % → 0–20 mapping is reasonable.
- **Cross-check with the 10 ms run (§8.1):** the 10 ms / 1 ms ratio at levels 1–5 is 9.6–9.7, close to the exposure ratio of 10. Camera exposure and LED output behave consistently.
- **Not covered:** levels 21–31 (above the app cap), absolute power, stability over time (M8 30-minute test), and the epi LEDs (next: an epi sweep with RFP or GFP in the light path).

### 8.3 RFP epi sweep, no sample (2026-11-16, 19:04 UTC, operator-approved, once; F at slot 1)

**Outcome:** `complete`. Light off, epi level 1 restored, camera restored and verified. Record: `led-camera-sweep-20261116T190407-complete.json`.

| Level | 1 | 2 | 3 | 5 | 8 | 10 |
|---|---|---|---|---|---|---|
| Mean − dark (1380.4), 10 ms | −1.6 | 1.9 | 2.8 | 4.8 | 8.8 | 11.1 |

- **Expected near-null result.** The RFP cube's emission filter blocks its own excitation light, and the stage was empty, so almost nothing reaches the camera. About 1 count per level (≈0.1 % of the brightfield signal) is filter leakage or stray reflection.
- The trend is monotonic, but this measures leakage, not LED output.
- **Needed for a real epi curve:** a fluorescent target under the RFP (or GFP) cube, e.g. a fluorescent slide or a uniform dye sample, which plan M8 already lists. Then a longer shutter (50 ms) as required.

## 5. Path to M8 (not approved, for planning)

1. **Gate 3 (this spec):** read status once; confirm cube identity bytes against the cubes physically installed.
2. **Gate 4 (RAM write, needs its own review):** `0x5B` select one channel at the lowest intensity (`0x4C` at the range minimum), `0x49` enable, observe on a fluorescent card, `0x49` disable. Light-path safety: never look into the path; DAPI is near-UV.
3. **M8:** intensity sweep (`0x4C`) with a power meter at the stage plane; stability over time.

Open items: decode the `0x5D` / `0x48` / `0x5E` sub-selectors; map cube-type bytes to cube names (DLL enum); trace the `0x47`/`0x4A`/`0x5A` handlers in the 27103 hex.
