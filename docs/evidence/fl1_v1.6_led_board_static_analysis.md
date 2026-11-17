# FL Auto 1.6 package: LED board, firmware files and calibration storage (L0 static analysis)

- **Date:** 2026-11-12
- **Level:** L0 (offline artefact analysis). Nothing from the vendor package was executed. No device was contacted.
- **Package:** `PearlUpdate_Rev1.6_31201.amg`, SHA-256 `5ac42b57…7d54cf0`, matches `vendor_software_analysis.md`.
- **Method:**
  - Unpacked with the Python standard library only: ZIP → 7-Zip SFX (`py7zr`) → Inno Setup 5 LZMA chunk (`lzma` module), giving one 76.6 MB solid stream.
  - PE import and export tables read with `pefile`.
  - Intel HEX records parsed and checksummed in Python.
  - Strings and INI section/key *names* scanned.

  innoextract was not used; downloading it was refused in auto mode.
- **This record keeps** names, sizes, hashes and counts. It reproduces no vendor code, firmware or calibration values.
- **The original HDD is abandoned** (owner, 2026-11-12). Per-unit host files (`PearlScope.ini` as configured, logs, the `.sw` files in use) are gone. Everything below comes from the *distribution package*.

## 1. Transport: what "COM" means (knowledge-base §5.1)

| Binary | Exports | Device-access imports | Reading |
|---|---|---|---|
| `EvosGenIII_LEDBoard64.dll` (application) | `DllGetClassObject`, `DllRegisterServer`, … | SetupAPI device interfaces; no `kernel32` file I/O imported directly | Microsoft COM in-process server. Its I/O path is not visible in its own imports (Unknown). |
| `EvosGenIII_LEDBoard_Bootloader64.dll` | same COM exports | SetupAPI device interfaces + `CreateFileA/W`, `ReadFile`, `WriteFile` | COM server that opens the device interface and exchanges reports with `ReadFile`/`WriteFile` |
| `CNControlBoardBootloader64.dll` | same COM exports | same as LED bootloader, plus `GetPrivateProfileStringA` | as above, and reads an INI |
| `CNControlBoard64.dll` | same COM exports | SetupAPI device interfaces | COM server |
| `MotorController64.dll` | same COM exports | `TML_lib.dll` (Technosoft); `RegSetValueExW` | COM server over Technosoft's library; **writes the registry** |

- **No LED- or CN-board binary imports serial-port functions** (`GetCommState`, `SetCommState`, …), `hid.dll`, WinUSB or `DeviceIoControl`.
  - **Correction (2026-11-12):** an earlier version said *no binary* does. The byte-exact `MotorController64.dll` **does** import `CreateFileW`, `Get/SetCommState` and `Get/SetCommTimeouts`, and drives the Technosoft axes over its own serial code ([motor subsystem §3a](fl1_v1.6_motor_subsystem_l0.md)).
- **"COM interface" means Component Object Model.** **Strongly supported:** the LED and CN boards are driven as HID devices through raw report I/O (`ReadFile`/`WriteFile` on the HID device interface). Windows binds both boards to `HidUsb` (first-connection record).

## 2. Firmware images in the package (knowledge-base §5.3)

Only two Intel HEX regions exist in the decoded stream. Splitting them at EOF records gives five files. **Each file's SHA-256 matches `fl_auto1_v1.6_inventory.json` (Verified):**

| File | Size | Checksums | Layout |
|---|---|---|---|
| `LED Board App - EVOS Gen III.hex` | 125,434 B | all valid | PIC24/dsPIC (phantom byte in every 4th position, 11,109/11,109); 44,436 data bytes; byte span `0x0`–`0x21FF7` with configuration words at `0x21FE0`–`0x21FF7` |
| `ChuckNorrisApplication.hex` | 380,451 B | all valid | PIC24/dsPIC family; configuration region at byte `0x1F00000` (word `0xF80000`) |
| `ChuckNorrisApplication_VERSION4.hex` | 371,989 B | all valid | same family |
| `ChuckNorrisSensorSlaveBoard.hex` | 41,539 B | all valid | same family |
| `ChuckNorrisSensorSlaveBoard_ORIGINAL.hex` | 39,588 B | all valid | same family |

- **Not in the package:** `Pearl_Bootloader.hex`, which the LED DLL names. The package can only update the LED *application*. `Firmware/Evos_Update.ini` also defines an `[EVOS Bootloader Update Command]` with target version `3.0.25949`.
- **The LED application image has no readable version string.** "Bundled package version" stays **Unknown** until a disassembly or the INI-to-version mapping is traced.
- **LED board MCU:** PIC24/dsPIC family (**Strongly supported**). The exact part number is Unknown.

## 3. Where calibration lives (knowledge-base §5.4)

### 3.1 On the LED board (HW_DATA): **Strongly supported**

`EvosGenIII_LEDBoard64.dll` defines an `HW_DATA` record with **From PIC Flash** / **To PIC Flash** operations and a *Flash Test* dialog. The record covers:

| Group | Fields (names only) |
|---|---|
| Stage geometry | X/Y/Z axis offset, X/Y axis registration, X/Y/Z mm per revolution, X/Y position min/max, X/Y home torque |
| Light path | epi cube 1/2/4 positions, epi mirror position, epi position min/max |
| Turrets | objective turret selections 1–5, objective-turret and phase-turret entries |
| Cameras | mono and colour calibrations per objective (2× … 100×), mono and colour parfocalities per objective, colour red/green gain, mono defect map |
| Filter cubes | parfocality per cube (DAPI, FITC/GFP, RFP, TexasRed, mCherry, YFP, QD545–QD705, …) |
| Prism Z encoder | firmware version, range, zero point; EEPROM read error codes |

**Reading:**
- **The board, not the PC, stores the instrument's per-unit calibration and stage travel limits.** Losing the HDD therefore probably did *not* lose that calibration.
- **A flash or HW_DATA write on this board risks it directly.** ADR-0009 now bans HW_DATA, PIC-flash, EEPROM and calibration writes as firmware-class operations.

### 3.2 On the host (package defaults only)

`PearlScope.ini` (package copy) contains:
- `[PearlScopeMotorController]`: `XAxisOffset`, `YAxisOffset`, `ZAxisOffset`, `XAxisRegistration`, `YAxisRegistration`, `X/Y/ZAxisMillimetersPerRevolution`;
- `[MotorControllerParameters]`: home torque levels;
- `[AppSettings]`: `FilterCubeParfocalityIsFunctionOfObjective`;
- `[EnvironmentChamberParameters]`: temperature, humidity and O₂-calibration settings.

`CN_parameter_update.ini` → `[Forced Parameter Updates]`: 114 keys pushed to the incubator board, including ambient offsets for chamber, glass and hose.

These are **defaults**, not this unit's values.

### 3.3 In the motor drives

The six `axis_firmware/*.sw` Technosoft setup files (X, Y, Z, F, O, P) are in the package. Whether the drives retain their setup without the host pushing these files is **Unknown** (ADR-0005, open question 3).

## 5. Disassembly of the bootloader and application interfaces (L0)

- **Inputs:**
  - Binaries carved from the payload. Inno Setup's call-instruction transform was reversed (64 KiB blocks, high-byte flip).
  - **The SHA-256 of `EvosGenIII_LEDBoard_Bootloader64.dll`, `EvosGenIII_LEDBoard64.dll` and `PearlScope64.exe` each match `fl_auto1_v1.6_inventory.json` (Verified byte-exact).**
- **Tools:**
  - Method names and vtable slots from each DLL's embedded type library, read as resource data with `LoadTypeLibEx(REGKIND_NONE)`: nothing was registered or executed.
  - Method bodies located through MSVC RTTI (`CComObject<…>` vtable) and disassembled with Capstone 5.0.7.

### 5.1 `IGen3LedBoardBootloader` (the interface that talks to `0DAD:0057`)

| Slot | Method | What the host code does | Level |
|---|---|---|---|
| 3 | `GetFirmwareVersion` | 65-byte output report `00 0A 00…` (report ID 0, command `0x0A`); reads 65 bytes; requires reply byte 1 = `0x8A` | L3 |
| 4 | `GetBoardType` | report exchange | L3 |
| 5 | `StartApp(bWaitForApp, eBoardType)` | 65-byte output report `00 08 00…` (command `0x08`), **no reply read**; then internal clean-up; if waiting, polls SetupAPI for `Vid_0dad&Pid_0051`/`0052` with a 30,000 ms limit | **L4** |
| 6, 7 | `DownloadHex`, `VerifyHex` | multi-report hex transfer | **L4** |
| 8 | `GetApplicationFirmwareVersion` | **Same request as slot 3** (`00 0A`). One `WriteFile` + one `ReadFile`, no other calls. Reply byte 1 must be `0x8A`; BE32 at bytes 2–5 ≥ 2 and 6–9 ≥ 1, else `E_NOTIMPL`. Version = BE32 major/minor/build at bytes 22–25 / 26–29 / 30–33 | L3 |
| 11 / 12 | `GetEraseRange` / `SetEraseRange` | | L3 / **L4** |
| 13 / 14, 15 | `GetCN5TestState` / `SetCN5TestEnabled`, `SetRunCN5TestUnconditionally` | switches a board test mode | L3 / **L4** |

- **Precondition:** a connected flag must be set.
- **Two handles:** separate write and read handles (`[this+0xD8]`, `[this+0xE0]`).
- **`ReadFile` has no timeout:** a silent board would block the caller forever. Any OpenEVOS implementation must use overlapped I/O with a timeout.

**Host-side safety proof for `GetApplicationFirmwareVersion` (Strongly supported):** it sends exactly one `0x0A` request and invokes no `StartApp`, `DownloadHex` or reset path. **Not provable offline:** what command `0x0A` does inside the bootloader firmware, because `Pearl_Bootloader.hex` is not in the package.

### 5.2 `IGen3LedBoard` (application interface, `0DAD:0051`; 149 methods)

| Read (L3 candidates) | Write (L4, banned by ADR-0009) |
|---|---|
| `GetHardwareSpecificData(eDataType,…)`, `GetPersistentData`, `GetPersistentData2[Vector]`, `GetPersistentData2AddressRange` | `SetHardwareSpecificData`, `SetConsecutive{Long,Double,String}HardwareSpecificData`, `SetPersistentData`, `SetPersistentData2[Vector]` |
| `GetPersistentDataChecksum`, `GetPersistentDataSection2Checksum` (stored vs computed) | `SetHardwareIniDataStored` |
| `GetHardwareIniDataStored`, `GetHardwareIniDataDefinitionsVersion`, `GetHardwareDataSectionSupported` | `ResetToBootloader`, `DownloadBootloaderHex`, `SetUpdatedBootloaderVersion` |

- **Calibration (`HW_DATA`) is reachable only through the application interface.** The bootloader interface has no calibration methods (Verified from the type library).
- **The safest first look** at the calibration, once the application runs, is the checksum pair (stored vs computed). It tests integrity without reading values.

### 5.3 Application protocol, decompiled (Ghidra 12.2 headless, SHA-256-verified release)

`EvosGenIII_LEDBoard64.dll` does use `WriteFile`/`ReadFile`/`Sleep`. The earlier "no kernel32 imports" note was a listing error in the L0 import script.

**Framing:**
- 65-byte HID reports: byte 0 = report ID 0, byte 1 = command.
- Byte 64 = the **two's complement** of the 8-bit sum of bytes 1–63, so that bytes 1–64 sum to zero. `FUN_180033110` returns `~sum + 1`. This is Verified on the board: its reply `00 F0 … 10` satisfies it. An earlier version of this document said "additive", which was wrong.
- A reply echoes the command with bit 7 set and carries the same checksum.

| Method | Command → reply | Request payload | Reply payload |
|---|---|---|---|
| `GetFirmwareVersion` (application) | `0x40` → `0xC0` | none | BE32 major/minor/build at bytes 2–5 / 6–9 / 10–13 |
| `GetPersistentData(addr, n)` | `0x51` → `0xD1` | BE24 address at bytes 2–4, count at byte 5 | count at byte 5, data from byte 6 |
| `GetPersistentDataChecksum` | `0x59` → `0xD9` | none | stored BE32 (bytes 2–5), computed BE32 (bytes 6–9) |

**Persistent-data sections, decompiled (2026-11-12, Strongly supported):**
- **One query, two sections.** `GetPersistentDataChecksum` (slot 131) and `GetPersistentDataSection2Checksum` (slot 137) send the **same** request `0x59` → `0xD9`. Slot 131 reads section 1 from reply bytes 2–5 / 6–9 (stored / computed). Slot 137 reads section 2 from bytes **10–13 / 14–17**. There is no separate section-2 command.
- **`HW_DATA` is in section 1.** Both `HW_DATA` field readers call slot 106 `GetPersistentData` (`0x51`). Section 2 has its own read family (`GetPersistentData2*`, slots 132–136), whose address range slot 132 reads from bytes 17–24 of the `0x40` reply. So the section-1 checksum is the one that covers `HW_DATA` (Probable: the coverage is inferred from naming and the shared read path).
- **`0x51` read sizes.** Slot 106 sends a BE24 address in bytes 2–4 and the count in byte 5, and copies `reply[5]` bytes from `reply[6]`. The host enforces **no maximum**; a reply has room for 58 bytes. The vendor's field readers request **4 or 8 bytes per field**, plus one 1-byte read at `0x1469` followed by a **16-byte** read at an address taken from that byte (probably the string fields). A Step C tool should stay within these vendor-used sizes and read field by field.
- **Frame checksum.** The transfer helper `FUN_18003DD70` sets byte 1 = command and byte 64 = `FUN_180033110` over bytes 1–63, i.e. the two's complement of their sum (see Framing above).
- **Correction to an outside review.** `UpdateFirmware` does **not** pass the ini version to `DownloadHex`: `iData` is an output pointer, initialised to 0 (§5.6). Whether the running application is the package's 3.0.27103 build can be settled only by the application's own `0x40` reply, which is not yet queried.

**Transfer routine (`FUN_18003e1c0`):**
- Runs under a critical section, only when the state flag `[this+0x358] == 1`.
- **Resends the request** after a failed `WriteFile` (500 ms sleep, up to 20 times).
- Up to 20 `ReadFile`s per write, discarding replies with a bad checksum or wrong command echo.
- On a **`0xF0` (busy)** reply it waits 10 ms and **repeats the whole exchange, up to 100 times**.

This retry policy is acceptable for a read but unsafe for a write. OpenEVOS must not copy it: one request per operator-approved read, with explicit timeouts.

### 5.4 PearlScope writes calibration to the LED board

`PearlScope64.exe` contains `CHardwarePersistentDataController`. Its `DoWriteHardwarePersistentData` logs:
- "saved MotorController / PGRCameraController / TurretPositionController / UEyeCameraController peristent data";
- "**wrote peristent data to LEDBoard**";
- "wrote out hardware persistent data for serial number %s".

User-facing messages include "**The XY calibration data has been stored**" and "**The Z calibration data has been stored**". A matching `DoReadHardwarePersistentData` reads it back ("successfully read in hardware persistent data").

**Strongly supported:**
- The original software writes per-unit calibration to the LED board's persistent memory, at least after its calibration procedures.
- It reads that calibration back when it initialises.

**Call graph** (Ghidra 12.2 full analysis of the byte-exact `PearlScope64.exe`, about 13 min):

| Caller of `DoWriteHardwarePersistentData` | Context strings | Reached from |
|---|---|---|
| `FUN_140020790` | "problems writing microscope specific persistent data" | UI handler (virtual / MFC message dispatch; no direct callers) |
| `FUN_14019e0f0` | "problems saving microscope specific data … contact Life Technologies" | `FUN_1401a0090` → `FUN_1401a40e0`. The same dispatcher drives the *Restore* handler (`FUN_14019e310`), so this looks like a service Save/Restore screen. |

- **Where "XY/Z calibration data has been stored" lives:** in calibration-wizard handlers (`FUN_14001f900` "Is the calibration point centered in the crosshairs?", `FUN_1400200d0` "…focused on the calibration slide?"), also reached only through UI dispatch.
- **`DoReadHardwarePersistentData`** is reached from the Restore handler and from another UI-dispatched handler ("problems reading … persistent data").

**Conclusion: Probable.** PearlScope writes calibration to the LED board only on operator actions (save/restore, calibration wizards) and reads it during normal use. A static call graph cannot follow MFC message-map dispatch, so a start-up route through those handlers is not fully excluded. Either way, OpenEVOS reproduces none of these writes (ADR-0009).

### 5.5 `HW_DATA` layout

76 of 118 fields are mapped to persistent-memory addresses `0x141D`–`0x15BE`: [`fl1_v1.6_hwdata_address_map.md`](fl1_v1.6_hwdata_address_map.md). Both field readers call slot 106 `GetPersistentData` (`0x51`) and copy bytes unswapped. Fields are little-endian `int32` / `double`. The protocol's own version fields are big-endian.

### 5.5a Where PearlScope actually keeps the calibration: a blob in section 2 (L0, 2026-11-12, Strongly supported)

Prompted by Step C, which found every mapped `HW_DATA` field zero although the section-1 checksum is non-zero.

- **The DLL path is direct.** `GetHardwareSpecificData` (slot 123) passes the mapped address straight to `GetPersistentData` (slot 106, command `0x51`), with no offset and no host-side cache. So Step C read the right addresses: on this unit those fields are empty.
- **Other section-1 items read by the DLL:**

  | Address | Size | Item | Method |
  |---|---|---|---|
  | `0x1400` | 8 | optics multiplier | slot 108 |
  | `0x140B` | 16 | serial number string | slot 116 |
  | `0x17F0` | 2 | "hardware ini data stored" flag | slot 128 |

  `GetHardwareDataSectionSupported` (slot 122) probes `eDataType` `0x1F`.
- **PearlScope keeps the calibration in one serialized blob**, managed by `CHardwarePersistentDataController`:
  - **Read** (`FUN_14012F8A0`): `FUN_1401B06C0` fetches the blob from the LED board, then the uEye camera, PGR camera, **MotorController** and **TurretPositionController** handlers each read their part.
  - **Write** (`FUN_14012F590`): each controller adds its part, and `FUN_1401B02E0` "wrote persistent data to LEDBoard".
- **The blob is in persistent-data section 2**, read through `GetPersistentData2Vector` (slot 135, `+0x438`) and written through `SetPersistentData2Vector` (slot 136):

  | Address | Size | Content |
  |---|---|---|
  | `0x15713` | 4 | magic `0x6F63696C` (bytes `6C 69 63 6F`, "lico"); the read aborts unless it matches |
  | `0x15717` | 8 | header value (probably a timestamp) |
  | `0x1571F` | 4 | size passed to the decoder (probably the uncompressed length) |
  | `0x15723` | 4 | stored length *N* |
  | `0x15727` | *N* | the compressed serialized blob |

  - The writer compresses the data (its output bound is `size × k + 12`, zlib-like; not yet confirmed) and refuses *N* ≥ `0x88DA` (35,034 bytes).
- **Wire protocol for section 2:** command **`0x31` → `0xB1`**, the same framing as `0x51` (BE24 address, count in byte 5, data from byte 6, two's-complement checksum). `GetPersistentData2Vector` reads in chunks of **58 bytes** (`0x3A`), the reply capacity. A full blob is therefore up to about 605 vendor-style reads.
- **Consequence:**
  - Our zero `HW_DATA` fields are an unused legacy interface on this software generation, not lost calibration.
  - Section 2's checksum is non-zero and consistent, so a blob is likely present (Probable until the magic is read).
  - Reading it would be a new reviewed tool, "Step D":
    1. the 20-byte header with the vendor's 4/8/4/4-byte reads;
    2. stop unless the magic is `lico` and *N* < 35,034;
    3. *N* bytes in 58-byte `0x31` chunks, failing fast;
    4. decode offline.
  - Writes (`SetPersistentData2*`) stay banned.
  - **Codec and inner format (decompiled, Strongly supported):**
    - **Decompressor.** `FUN_140380280` is zlib's `uncompress()`: `inflateInit` with window bits 15 (zlib-wrapped stream) and `inflate(Z_FINISH)`, into a buffer sized from `0x1571F`. PearlScope links zlib ("deflate 1.2.7", "inflate 1.1.3").
    - **Magic.** The check compares the 4 bytes as a little-endian `uint32` with `0x6F63696C`, i.e. bytes `6C 69 63 6F`.
    - **Inner format.** The decoded bytes become a `CMemoryStream`. Each controller reads **fixed-size fields in a fixed order** with `IStream::Read`, so there are no tags and no `CArchive`. The order is uEye camera, PGR camera, MotorController, TurretPositionController.
    - **MotorController** reads 4, 8, 4, 4, 4 bytes, then three doubles, which it writes into `PearlScope.ini` as `[PearlScopeMotorController] X/Y/ZAxisMillimetersPerRevolution`, then more 4-byte fields.
    - **TurretPositionController** reads 4 + 8 bytes, then strings, and writes `[ObjectiveLenses]` entries, including "-Calibrations" and "-Parfocalities".
    - **So the board blob is the master copy:** PearlScope regenerates these ini sections from it at start-up. The `PearlScope.ini` lost with the HDD can in principle be rebuilt from the blob.
    - **Decoding plan:** parse the decoded stream with the same field sequence. Mapping the remaining fields needs the uEye/PGR readers (`FUN_140327290`, `FUN_1401E0490`) and the rest of the motor and turret readers decompiled.
  - **Step D run (16:26 UTC, [ADR-0015](../decisions/ADR-0015-gate2-stepd-section2-blob.md)):**
    - header "lico", version-like double 1.0, *N* = 1,305, decoded size 5,660;
    - the blob was read in 23 chunks and zlib-decoded to exactly 5,660 bytes;
    - it contains the objective table (models, per-objective calibration and parfocality rows);
    - Step B was unchanged afterwards.

    **The master calibration is present and readable on this unit (Verified).**
  - **Parsed exactly, offline:** the full stream schema is in [`fl1_v1.6_pearl_persistent_schema.md`](fl1_v1.6_pearl_persistent_schema.md). Our blob parses with zero bytes left over in the **pre-1.6** layout (no `-APO`), i.e. it was written by older PearlScope software. The values remain CANDIDATE.

### 5.6 Version embedded in `LED Board App - EVOS Gen III.hex` (L0, offline)

- **Method.** The HEX piece was taken from the decoded payload and checked against the inventory SHA-256 (`af93682f…ee2d0ecc`, 125,434 B). It was then turned into PIC24 program words (HEX byte address = 2 × PC, three data bytes and one phantom byte per word; 11,109 words, PC `0x000000`–`0x010FFA`). Instruction literals were decoded by hand-coded patterns (`MOV #lit16`, `MOV.B #lit8`).
- **The application's own version reply (command `0x40` → `0xC0`, Strongly supported).** The handler at PC `0x0024B0` is reached from a jump table. It writes constants into the reply buffer at RAM `0x197C`–`0x1988`: `C0`, `00 00 00 03`, `00 00 00 00`, `00 00 69 DF`. That is BE32 major / minor / build = **3 / 0 / 27103**, in exactly the layout §5.3 gives for the application's `GetFirmwareVersion`.
- **A second copy of the build, at the end of the image.** The last program words are `0x010FF0` = `000001`, `0x010FF2`–`0x010FF6` = 0, **`0x010FF8` = `0069DF` (27103)**, `0x010FFA` = 0. This looks like a stamp a bootloader could read (Probable; the bootloader image is not in the package, so its read path cannot be confirmed).
- **25949 (`0x655D`) appears nowhere in the image:**
  - not as an instruction literal (`MOV #0x655D`, or `MOV.B #0x65` next to `MOV.B #0x5D`);
  - not as a 16- or 32-bit value in either byte order;
  - not as ASCII.
- **Conclusion:**

  | Claim | Rating |
  |---|---|
  | The package hex is application **3.0.27103** | Strongly supported |
  | The board's application, as its bootloader reports it, is **3.0.25949** | Gate 1 |
  | The application image on our board is **not** the image in the FL Auto 1.6 package | Strongly supported, on the reasonable assumption that the bootloader's version field reports the application's build |
  | The board's build is **older** (25949 < 27103) | Probable: assumes build numbers increase |
  | The board matches the version the package's own update file declares (3.0.25949) | Strongly supported (correction below) |
- **Correction (same day, from the StartApp review):** the package's `Firmware\EVOS_Update.ini` declares `hex_file=LED Board App - EVOS Gen III.hex` with **`version_major=3`, `version_minor=0`, `version_build=25949`**, exactly what our board reports.
  - **How PearlScope decides to flash (Strongly supported, decompiled):** `GetUpdateFirmware` flashes only if the board's current version is *lower* than the ini's. So PearlScope 1.6 would **not** have flashed our board. An earlier version of this section said it likely would; that was wrong.
  - **The package is internally inconsistent (Probable):** its ini says 25949, its hex embeds 27103. The bootloader's number does not come from the host: `DownloadHex(pHex, uiLength, pResult, iData)` receives no version, and PearlScope passes only the hex bytes. So the board's 25949 comes from the image actually in its flash. The most likely history is that the board was last flashed from an earlier package whose hex and ini both said 25949, and the 1.6 package later shipped a newer hex without updating its ini.
- **Implications:**
  - We do not hold a copy of the exact application the board runs. If that application were ever damaged, the only image available is a different build. The flashing ban stands.
  - See [`fl1_v1.6_startapp_l4_review.md`](fl1_v1.6_startapp_l4_review.md) for what this means for leaving the bootloader.
  - The `0x0A` reply's header fields (2 and 1) do not obviously correspond to the stamp words (`1`, `0`, …), so their meaning stays a hypothesis.

## 6. Draft L3 read specification (for review; NOT approved)

Nothing here may be sent to the board until this section is reviewed and the operator explicitly approves a specific read (ADR-0009). Commands not listed here are never sent.

| Step | Board must be | Request (65-byte HID output report) | Accept reply only if | Purpose |
|---|---|---|---|---|
| A | bootloader `0DAD:0057` | `[0]=0x00, [1]=0x0A`, rest `0x00` | 65 bytes, `[1]==0x8A`, BE32 `[2..5]`≥2 and `[6..9]`≥1 | bootloader version (BE32 fields) and application version (BE32 at 22–25 / 26–29 / 30–33) |
| B | application `0DAD:0051` | `[1]=0x59`, `[64]=(-sum([1..63])) & 0xFF` (= `0xA7`) | `[1]==0xD9`, checksum valid | stored vs computed checksum of the calibration memory (integrity, no values) |
| C | application `0DAD:0051` | `[1]=0x51`, BE24 address at `[2..4]`, count at `[5]` (vendor sizes 1/4/8/16), two's-complement checksum at `[64]` | `[1]==0xD1`, checksum valid, `[5]`==requested count | read raw `HW_DATA` bytes |

- **Request `0x0A` is the bootloader version query.** `0x8A` is only its acknowledgement.
- **Bootloader requests carry no checksum.** The disassembly shows the 65-byte buffer zeroed except bytes 0–1; only the application protocol (`0DAD:0051`) appends the byte-64 additive checksum.

### Approval gates (separate decisions)

**Gate 1: Step A only.** It works on the board as it is (`0DAD:0057`) and needs no mode change. **Status (2026-11-12):** implemented as `python -m openevos.maintenance.led_bootloader` ([ADR-0011](../decisions/ADR-0011-gate1-led-bootloader-version-query.md)), tested against a fake device, and **run once on the instrument** (result below). Its result decides whether Gate 2 is even worth discussing:
- If the reply validates and carries plausible application version numbers (bytes 22–33 not all `0x00`/`0xFF`), that is evidence an application image is present.
- If the header checks fail, or the application version is blank, **`StartApp` must not be considered**.
- What the two BE32 header fields (bytes 2–5 ≥ 2, bytes 6–9 ≥ 1) mean is a **hypothesis**, for example "protocol revision" and "application present/valid". The vendor code checks them; it does not name them.

**Gate 1 result (run 2026-11-12, operator-approved, one send):**
- **Reply:** `0x8A`, 65 bytes, first and only attempt. The dry run beforehand confirmed exactly one `0DAD:0057` interface with 65/65-byte reports.
- **Header fields:** `header_a` (bytes 2–5) = 2, `header_b` (bytes 6–9) = 1. Both pass the vendor's checks exactly at their minimums.
- **Application version: 3.0.25949** (BE32 at bytes 22–25 / 26–29 / 30–33 = 3 / 0 / 25949). Not blank.
- **Undecoded bytes:** 10–21 are non-zero; their meaning is Unknown. They are kept only in the git-ignored evidence file, in case they identify the unit. Bytes 34–64 are zero.
- **Board unchanged:** a read-only enumeration afterwards still shows exactly one `0DAD:0057`, no `0051`/`0052`, and the CN board `0058` present.
- **Verdict:** valid reply with a non-blank application version. This is **evidence that an application image is present** on the board (Probable: the field's meaning comes from the vendor DLL's decoding, not from the board firmware). It is **not** evidence that the application would start cleanly, nor that `HW_DATA` is intact.
- **Compared with the package hex (L0, same day): they differ.** `LED Board App - EVOS Gen III.hex` is build **27103**, the board reports build **25949**. Details in §5.6.

**Gate 2: Steps B and C.** Held until Step A's result has been reviewed **and** a separate L4 review approves leaving the bootloader. That review was approved, and StartApp was run once on 2026-11-12: the application `0DAD:0051` appeared after about 1 s, with 65/65-byte reports ([review §8](fl1_v1.6_startapp_l4_review.md)). **Gate 2 Step B result (15:19 UTC):** section 1 (`HW_DATA`) stored = computed = `0x0001BF60`, and section 2 stored = computed = `0xC5324E58`. The calibration memory is internally consistent ([ADR-0013](../decisions/ADR-0013-gate2-stepb-calibration-checksum.md)). The first attempt had used a wrong frame checksum and was answered BUSY. **Step C result (16:06 UTC, [ADR-0014](../decisions/ADR-0014-gate2-stepc-hwdata-read.md)):**
- All 76 mapped fields were read: valid replies, each echoing its address. **Every value is 0.**
- Yet the section-1 checksum is non-zero (`0x0001BF60`).
- So the calibration is **not** at the mapped addresses as we address them, or it was never stored on this board. The open question is recorded in ADR-0014; there are no usable values yet. Until the next power cycle the board is in its application; Gate 1 (bootloader-only) cannot run until then. Approving an L3 read never implicitly approves `StartApp` (`0x08`).

### Windows HID handling for any approved read

1. Open the HID interface path with `CreateFileW(GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, …, FILE_FLAG_OVERLAPPED)`. The vendor uses two handles; one overlapped handle is enough.
2. Call `HidD_FlushQueue` immediately before the `WriteFile`, so a report queued at enumeration is never mistaken for the reply.
3. Wait on an event with a 500 ms limit for each of `WriteFile` and `ReadFile`. On timeout, `CancelIoEx`, close the handle and report "no reply". Never retry automatically.

**OpenEVOS rules for any of these reads** (deliberately unlike the vendor DLLs):
1. Open the HID interface with `FILE_FLAG_OVERLAPPED`. Every `WriteFile`/`ReadFile` waits at most 500 ms, then `CancelIoEx`.
2. Send exactly one request per operator approval. No automatic resend, and no loop on `0xF0` (busy): report "busy" and stop.
3. Allowlist only the commands `0x0A`, `0x59` and `0x51`. Every other byte value is refused before any I/O.
4. Log the raw request and reply bytes and the decoded values as evidence. Calibration values go into a `CANDIDATE` capability record, never `VERIFIED`, until they're checked against physical behaviour.

## 4. The tension this creates

`HW_DATA` is exposed by the **application** interface (`0051`). Our board currently enumerates in its **bootloader** (`0057`). Reading the calibration would therefore first need the board to run its application, which today means `StartApp`: an L4 operation, disabled by ADR-0009.

The path forward is still L0, not hardware:
- trace in a disassembler what `StartApp`, `GetApplicationFirmwareVersion` and the HW_DATA read actually send;
- check whether PearlScope ever writes HW_DATA during routine start-up (it must not be reproduced);
- only then design a reviewed read-only query (L3) and a separately reviewed maintenance step.
