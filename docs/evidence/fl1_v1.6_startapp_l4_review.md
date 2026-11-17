# L4 review: `StartApp` on the LED board bootloader (`0DAD:0057`)

- **Status:** paper review, **for decision. Not approved; nothing sent.** Under ADR-0009, `StartApp` remains refused everywhere until the operator approves a specific procedure and it is implemented and tested.
- **Date:** 2026-11-12.
- **Purpose:** decide whether OpenEVOS may send the one command that moves our LED board from its bootloader to its application. Gate 2 (the checksum and calibration reads in [§6 of the static analysis](fl1_v1.6_led_board_static_analysis.md)) needs the application running.
- **Sources (all offline):**
  - Ghidra 12.2 decompilation of the byte-exact (SHA-256-verified) `EvosGenIII_LEDBoard_Bootloader64.dll` and `PearlScope64.exe` from the FL Auto 1.6 package;
  - both DLLs' type libraries, read as resource data;
  - the package's `Firmware\EVOS_Update.ini`;
  - the Gate 1 result.

## 1. What `StartApp` does on the wire (Strongly supported)

`IGen3LedBoardBootloader::StartApp(bWaitForApp, eBoardType)` is vtable slot 5, `FUN_180008D30`.

1. **Precondition:** the "connected" flag must be set (`+0x70`), else `E_FAIL`.
2. **One `WriteFile` of 65 bytes:** `00 08` followed by 63 zero bytes. There is **no checksum and no `ReadFile`**; the board's answer is never read.
   - `eBoardType` is **not** in the report.
3. **PC-side clean-up only:**
   - `FUN_1800066D0` frees buffers and closes both handles;
   - `FUN_180008CB0` resets the handle and buffer fields.
   - Neither sends anything.
4. **If `bWaitForApp`:** `FUN_180009400` polls SetupAPI once a second, for up to 30,000 ms, until a device whose hardware ID contains `Vid_0dad&Pid_0051` appears (`Pid_0052` for board types 4 and 5).
   - The polling function `FUN_1800061E0` uses only SetupAPI enumeration and registry-property reads. It opens nothing and sends nothing.
5. **The DLL never verifies the application.** "Success" means only that a `0051` device enumerated within 30 s.

So `StartApp` is exactly one 65-byte output report. Everything else is host-side.

## 2. How the vendor software uses it (Strongly supported)

`CPearlScopeDialog::OnInitDialog` (`FUN_1401A43D0`), when `AppSettings/AllowIllumination` is set:

- **Every start-up (normal path):**
  - `FUN_1401B1910` calls `FUN_1401B16B0(…, 1)`. That function creates the **bootloader** COM object and calls **`StartApp(1, 7)`**, ignoring the result.
  - It then creates the **application** COM object, retrying up to 100 × 200 ms.
  - The GUIDs it uses were checked against both type libraries:

    | GUID | Type-library name |
    |---|---|
    | CLSID `{48469D15-F764-4A7D-AF4D-1ADC8C1B781C}` | `Gen3LedBoardBootloader` |
    | IID `{D7C615EF-27C3-419B-8D82-02A9FBD34FFA}` | `IGen3LedBoardBootloader` |
    | CLSID `{F9A087AB-72C3-4D61-ADB6-EF13FF9C0BF1}` | `Gen3LedBoard` (the application) |
- **Update path (only if `GetUpdateFirmware` says so):**
  - the application object's `+0x40` (back to bootloader);
  - open the bootloader object;
  - `UpdateFirmware`, which calls `DownloadHex` with the package hex;
  - `StartApp(1, 3)`;
  - reconnect without another `StartApp`.
  - `GetUpdateFirmware` flashes only when the application's own reported version is lower than the version declared in `EVOS_Update.ini`, which is 3.0.25949. Our board reports 3.0.25949, so this path would not have run (see the §5.6 correction in the static analysis).

**Consequence:** `StartApp` is not a recovery operation. Every PearlScope session on this instrument sent it once, before any other LED-board traffic.

A board that enumerates as `0DAD:0057` after power-on, which is what we observe, is therefore most likely its **normal power-on state** (Probable). The host is expected to start the application each session.

## 3. What we know about the board right now

| Fact | Source | Label |
|---|---|---|
| Board enumerates as `0DAD:0057`, HID, 65/65-byte reports | first connection, Gate 1 dry run | Verified |
| The bootloader answers `0x0A` correctly; the vendor header checks pass (2, 1) | Gate 1 | Verified |
| Application version recorded in the board: 3.0.25949 (not blank) | Gate 1 | Verified (meaning of the field: Probable) |
| That version equals what the package's update file declares | `EVOS_Update.ini` | Strongly supported |
| The application image is intact and will start | — | **Unknown** |
| What the bootloader firmware does with `0x08` (any image check before jumping) | `Pearl_Bootloader.hex` is not in the package | **Unknown** |
| What the application does on start with no host commands (LED state, outputs) | — | **Unknown** |
| A power cycle returns the board to its bootloader | inferred from §2 and our observation | Probable |
| `HW_DATA` calibration is intact | — | **Unknown** (Gate 2 Step B would tell) |

## 4. Hazards and how a procedure would control them

| # | Hazard | Likelihood | Effect | Control |
|---|---|---|---|---|
| H1 | The application image is corrupt; the bootloader jumps into it and the board hangs | Low: valid version record; the vendor jumps into it every session | Board unresponsive until power cycle; no `0051` appears | Wait only (enumeration, 30 s). Never retry `0x08` and never send anything else. Report; the operator power-cycles the instrument when convenient and we confirm `0057` returns |
| H2 | The application drives outputs on start (e.g. an LED on) | Unknown | Light on the sample; eye exposure if a cube path is open | No sample on the stage, light path covered or lid closed, operator watching the instrument. The procedure sends nothing to the application |
| H3 | The application writes its own flash on start (e.g. initialises `HW_DATA`) | Low: it does so on every vendor start-up anyway | Calibration changed | Accepted residual risk: identical to every historical PearlScope session. Gate 2 Step B (checksum) afterwards would detect inconsistency, not prevent it |
| H4 | We send the wrong bytes, or more than once | Controlled by design | Unknown bootloader behaviour | A constant report `00 08 00…`, an allowlist of exactly `0x08`, one `WriteFile`, no loop. Tests with a fake device as for Gate 1 |
| H5 | The wrong device gets the report (e.g. the CN board `0058`, or an application `0051`) | Controlled by design | Unknown | Exactly one `0DAD:0057` interface; the driver-reported VID/PID and 65/65 report sizes are checked before opening; refuse if any `0051`/`0052` is already present |
| H6 | Windows treats `0051` as a new device | Expected | A driver-install pause | `0051` is a standard HID device (inbox `HidUsb`); allow the 30 s window; record the result |
| H7 | OpenEVOS later sends application commands without review | Controlled | Unknown | The procedure ends at "`0051` present". The command core keeps a physical device with unknown firmware mode fail-closed (ADR-0009). Gate 2 reads remain a separate approval |
| H8 | The application cannot be restored if damaged later | Pre-existing | Board unusable | We hold no exact copy of 3.0.25949 (§5.6). The flashing ban stands; this review does not change it |

## 5. Proposed procedure (if approved)

A second operator-run maintenance tool, analogous to Gate 1 (ADR-0011): `python -m openevos.maintenance.led_startapp`.

### Preconditions (all checked; any failure means nothing is sent)
- Exactly one present `0DAD:0057` interface, and no `0DAD:0051`/`0052` present.
- The driver-reported VID/PID is `0DAD:0057` and the report sizes are 65/65 bytes.
- A Gate 1 evidence record from the same session (≤ 30 min old) with a valid reply and a non-blank application version.
- The operator confirms the physical checklist: no sample on the stage, lid closed or light path covered, someone watching the instrument. They then type the exact phrase `START LED APP ON 0DAD:0057`.

### Action
- Open overlapped; **do not** flush or read.
- One `WriteFile` of `00 08` and 63 zero bytes, with a 500 ms limit. Close the handle.

### Afterwards (no I/O to the board)
- Poll SetupAPI once a second for up to 30 s for `0DAD:0051`/`0052`, and record when it appears.
- Record whether `0057` disappeared.
- Write the JSON evidence: request bytes, timings, before/after device lists.

### Never
- Retry `0x08`.
- Send anything to `0051`.
- Read or write `HW_DATA`.
- Use the application's "back to bootloader" (`+0x40`).

### Return path
A power cycle of the instrument. This is expected to bring back `0057` (Probable; to be confirmed and recorded the first time).

## 6. Recommendation

**Approve, with the controls in §4–5.**

The decisive evidence is §2: `StartApp` is the first thing the vendor software sends to this board on every start-up. The board's recorded application version matches what its own update package declares, and the bootloader's header checks pass. The residual unknowns (H1–H3) are the same ones every historical PearlScope session accepted. The procedure adds what the vendor lacks: one attempt, no follow-up commands, a physical safety checklist and an evidence record.

**What approval would not cover:**
- Gate 2 reads (`0x59` checksum, `0x51` calibration): a separate decision after `0051` is seen.
- Any application command, any flash, or the application's return-to-bootloader. These stay banned or unreviewed.
- Letting the command core talk to the LED board. A native application backend that reports `firmware_mode` is future work, with its own review.

## 7. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement the §5 tool (tests against a fake device only) | **approved; implemented** as `python -m openevos.maintenance.led_startapp` ([ADR-0012](../decisions/ADR-0012-led-startapp-maintenance-tool.md)) | operator | 2026-11-12 |
| Run it once on the instrument | **approved; run once**: the application appeared (§8) | operator, with the physical checklist confirmed | 2026-11-12 |

## 8. Result of the first run (2026-11-12, about 14:56 UTC)

### Sequence
1. **Fresh Gate 1** (`…gate1-20261112T145525-reply.json`): a valid reply, byte-for-byte identical to the first Gate 1 reply that day. Application 3.0.25949.
2. **StartApp dry run** (`…startapp-20261112T145535-dry_run.json`): every precondition held; nothing sent.
3. **One send**, after the operator confirmed the checklist (stage empty, lid closed, observer present). Record: `…startapp-20261112T145644-app_appeared.json`.
   - One `WriteFile` of `00 08` and 63 zero bytes; 65 bytes written. No read, no retry.
   - About **1 s** later, `0DAD:0057` was gone and **`0DAD:0051` was present**.
   - Windows bound it to the inbox `HidUsb` driver as "HID-compliant vendor-defined device", status OK. No driver prompt.
   - The CN board `0DAD:0058` stayed present and unchanged.
4. **Driver-held description of `0051`**, read through a zero-access handle (nothing sent): VID/PID `0DAD:0051`, device version `0x0001`, **65-byte input and output reports**. This matches the application protocol in §5.3 of the static analysis.

Nothing has been sent to the application.

### What this settles

| Item | Label |
|---|---|
| `StartApp` works on our unit, and the application starts within about 1 s | Verified |
| The application enumerates with the report size Gate 2 expects | Verified |
| The bootloader is the board's normal power-on state | Strongly supported (§2, now consistent with behaviour) |

### Still open
- **Did anything visible happen at start? No.** A second StartApp was run at 15:16:46 UTC, after the power cycle, specifically so the operator could watch. Record: `…startapp-20261112T151646-app_appeared.json`.
  - The application again appeared after about 1 s.
  - The operator, watching the instrument with the stage empty and the lid closed, **noticed no change**: no LED, sound, carriage movement or fan change.
  - So the application starts with no observable output when no host commands follow (operator observation, one trial). This lowers concern about hazard H2.
- **Power-cycle return path: confirmed** (about 15:15 UTC, same day). After the operator power-cycled the EVOS:
  - `0DAD:0057` was present again, with no `0051`/`0052`, and `0058` unchanged;
  - a fresh Gate 1 reply was byte-for-byte identical to the earlier ones (`…gate1-20261112T151559-reply.json`).

  So the board returns to its bootloader at power-on (**Verified**).
- **`HW_DATA` integrity:** Unknown. This is Gate 2 Step B (`0x59`), which needs its own approval and tool.
- **The application's own version reply** (`0x40`): not queried. That would be an application command, not covered by this approval.
