# Motor Gate 1: read-only first contact with the Technosoft drives (specification, for review)

- **Status:** **Revision 2, for review.**
  - Rev 1 was implemented (ADR-0016) and run once on 2026-11-12: it stopped at line sync with no reply (§11). Its port setup misread the vendor's.
  - Rev 2 (§4, §5 step 1, §6, §7 M1/M1a/M1b, §8, §9) copies the vendor's sequence exactly and adds a send-nothing `--inspect-port` step.
  - Rev 2 is implemented. `--inspect-port` ran twice (§12, §12a; no bytes sent).
  - The **full run completed** on 2026-11-13 (§13): all six drives are idle and homed at 115,200 baud, their versions equal `MotorFirmware.ini`, and `CPOS` = 0. Under ADR-0009 every motor-drive command stays refused until the operator approves a specific procedure, and it has been implemented and tested against a fake device.
- **Date:** 2026-11-12.
- **Basis:** [`fl1_v1.6_motor_subsystem_l0.md`](fl1_v1.6_motor_subsystem_l0.md) §3a, built from:
  - the byte-exact `MotorController64.dll`;
  - `PearlScope64.exe`;
  - the six hash-verified `axis_firmware` files.

## 1. Purpose

Learn, without changing anything on the drives:
1. **Are the drives present and answering on the expected port?**
2. **Have they finished power-up homing?** `ESTATEV` = 0, and `IINITONCEDONE` = 12345 on X, Y, Z and F.
3. **Which script version does each drive run (`IVERSION`)?** This settles whether PearlScope 1.6 would have rewritten them (it does so when the version is below X 29, Y 29, Z 21, F 29, O 7, P 6).
4. **Where is each axis (`CPOS`)?** Raw counts, converted offline to candidate mm/µm.

## 2. What is already known (offline)

| Fact | Label |
|---|---|
| **Drives:** PIM2401-CAN ×6 on one serial link. Axis IDs X = 1, Y = 2, P = 3, Z = 4, F = 5, O = 6 | Strongly supported |
| **Link:** PearlScope's own serial code (not `TML_lib`) at 9,600 baud 8N2, then 115,200 after a baud-change command | Strongly supported |
| **Power-up:** X, Y, Z and F home on their own; O and P stay idle | **Verified** 2026-11-13: X/Y stage movement seen by the operator (focus not observed); X/Y/Z/F report `IINITONCEDONE` = 12345 (§13) |
| **The read frames below are exactly the vendor's own** (X `CPOS` probe, Y/Z version reads) | Strongly supported (byte-identical) |
| **Units:** X, Y 2,880 counts/rev at 1.27 mm/rev; Z 5,000 counts/rev at 0.61 mm/rev. **Cross-check:** the stored limits convert to spans of 119.1 × 78.1 mm (X × Y) and 8.4 mm (Z) | Strongly supported |
| **Serial port:** FTDI FT232R `0403:6001` on the microscope's USB hub (`COM3` at first connection) | Verified (inventory) |
| Exact semantics of the gateway-ID query (opcode `0xB000`) | Probable |
| Word order of 32-bit replies | **Verified (vendor code, 2026-11-13):** low word first, each word big-endian, i.e. the tool's `i32_word_swapped` |

## 3. Preconditions (any failure: nothing is sent)

1. **The instrument has been on for at least 2 minutes**, so power-up homing has had time to finish. The operator confirms that no one is about to power-cycle it.
2. **Exactly one FTDI `0403:6001` port** is present under the microscope's USB hub, identified by its PnP instance path (as recorded at first connection), not just by its COM number.
3. **The port opens exclusively.** If another program holds it (for example a stray PearlScope or EasyMotion Studio), the tool refuses.
4. **The operator types the exact phrase** `READ MOTOR DRIVES ON COMn` (with the actual port number).
5. **The default is a dry run:** identify the port, print every frame, open nothing.

## 4. Port configuration (revision 2: copying the vendor's `FUN_180041560` exactly)

Revision 1 used a zero-filled DCB, which disabled DTR and RTS. That was a misreading, and its only run got no answer (§11). Revision 2 follows the vendor's code line by line.

- **Open:** `CreateFileW("\\.\COMn", GENERIC_READ|GENERIC_WRITE, share 0, OPEN_EXISTING)`, unchanged.
- **Read the current settings first:** zero a 0x1C-byte DCB, set `DCBlength = 0x1C`, then `GetCommState`. **Every field read back is recorded** in the evidence before anything is changed (flags decoded: `fBinary`, `fDtrControl`, `fRtsControl`, `fOutxCtsFlow`, `fOutxDsrFlow`, `fOutX`/`fInX`, …).
- **Driver-state check (new, before anything is sent).** Revision 1 called `SetCommState` with DTR/RTS disabled, and the FTDI driver may have kept that. The tool **refuses** if the read-back DCB has `fDtrControl ≠ DTR_CONTROL_ENABLE` or `fRtsControl ≠ RTS_CONTROL_ENABLE`, or any flow control enabled. It then asks the operator to reset the driver state by power-cycling the instrument (which re-enumerates the FTDI chip on the microscope's hub) and waiting 2 minutes. The tool never sets these flags itself: the goal is the vendor's state, not our guess at it.
- **Change only four fields** of the read-back DCB, as the vendor does: `BaudRate`, `ByteSize = 8`, `Parity = NOPARITY`, `StopBits = TWOSTOPBITS`. Then `SetCommState`.
- **Timeouts, as the vendor's:** first `GetCommTimeouts`, then `ReadIntervalTimeout = 1000`, `ReadTotalTimeoutMultiplier = 10`, `ReadTotalTimeoutConstant = 10` (the write timeouts are left as read). A 1-byte sync read therefore waits ≤ 20 ms and a 32-byte reply read ≤ 330 ms.
- **Two baud passes, in the vendor's order:**
  1. **Pass 1, 115,200 baud:** line sync only.
  2. **Pass 2, 9,600 baud:** only if pass 1 got no `0D`.

  The tool continues at whichever rate answered and records it. **Neither side's baud rate is ever changed by a command:** the vendor's `0x0820` (`SCIBR`) is never sent, so drives found at 9,600 stay at 9,600.

## 5. Exchange (fixed sequence, each request sent once)

### Frame format
Technosoft binary: `len | dest (2 bytes, ID<<4) | opcode (2) | sender 0x0011 | variable address | checksum`, where the checksum is the 8-bit sum of the preceding bytes. Multi-byte fields are big-endian, as in the vendor's frames.

### Sequence

| # | Request | Bytes | Accept only if |
|---|---|---|---|
| 1 | **Line sync** (rev 2: `FUN_18002C430` exactly) | single `FF` byte, then a 1-byte read; up to **15** attempts with 10 ms between them, per baud pass (§4) | the reply byte is `0D`. If neither pass syncs: stop |
| 2 | **Gateway axis ID** | `08 00 00 B0 00 00 01 00 00 B9` | `O` (`4F`), then a frame with a valid checksum. The ID it reports must be **1**, else stop. **Never** send the vendor's `0x0801` axis-ID write |
| 3 | **Readiness, version and position** of every axis: the 22 frames in §5.1, in table order (X, Y, P, Z, F, O) | as listed | `O`, then a frame whose length byte matches, whose checksum is valid, and whose echoed variable address matches the request |

- **Rev 2, as the vendor:** every request (the ID query and each read) is preceded by its own line sync (step 1, at the rate found), as `FUN_18003F300` and the probes do. Then: one 10-byte write, `Sleep(50 ms)`, and one `ReadFile` of up to 32 bytes under the §4 timeouts.
- Before each sync the input buffer is purged (`PurgeComm` RX only). This sends nothing.
- **Each request frame is written once and never resent.** Only the `FF` sync byte repeats, within its 15-attempt bound. A sync failure before a later request stops the run.

### 5.1 The only frames that can be built (constants)

| Axis (ID) | Read | Variable | Frame |
|---|---|---|---|
| X (1) | 16-bit | `ESTATEV` `0x03EA` | `08 00 10 B0 04 00 11 03 EA CA` |
| X (1) | 16-bit | `IINITONCEDONE` `0x03FE` | `08 00 10 B0 04 00 11 03 FE DE` |
| X (1) | 16-bit | `IVERSION` `0x03EF` | `08 00 10 B0 04 00 11 03 EF CF` |
| X (1) | 32-bit | `CPOS` `0x029E` | `08 00 10 B0 05 00 11 02 9E 7E` |
| Y (2) | 16-bit | `ESTATEV` `0x03EA` | `08 00 20 B0 04 00 11 03 EA DA` |
| Y (2) | 16-bit | `IINITONCEDONE` `0x03FE` | `08 00 20 B0 04 00 11 03 FE EE` |
| Y (2) | 16-bit | `IVERSION` `0x03EF` | `08 00 20 B0 04 00 11 03 EF DF` |
| Y (2) | 32-bit | `CPOS` `0x029E` | `08 00 20 B0 05 00 11 02 9E 8E` |
| P (3) | 16-bit | `ESTATEV` `0x03B2` | `08 00 30 B0 04 00 11 03 B2 B2` |
| P (3) | 16-bit | `IVERSION` `0x03E8` | `08 00 30 B0 04 00 11 03 E8 E8` |
| P (3) | 32-bit | `CPOS` `0x029E` | `08 00 30 B0 05 00 11 02 9E 9E` |
| Z (4) | 16-bit | `ESTATEV` `0x03B2` | `08 00 40 B0 04 00 11 03 B2 C2` |
| Z (4) | 16-bit | `IINITONCEDONE` `0x03F6` | `08 00 40 B0 04 00 11 03 F6 06` |
| Z (4) | 16-bit | `IVERSION` `0x03E2` | `08 00 40 B0 04 00 11 03 E2 F2` |
| Z (4) | 32-bit | `CPOS` `0x029E` | `08 00 40 B0 05 00 11 02 9E AE` |
| F (5) | 16-bit | `ESTATEV` `0x03B6` | `08 00 50 B0 04 00 11 03 B6 D6` |
| F (5) | 16-bit | `IINITONCEDONE` `0x03F8` | `08 00 50 B0 04 00 11 03 F8 18` |
| F (5) | 16-bit | `IVERSION` `0x03BC` | `08 00 50 B0 04 00 11 03 BC DC` |
| F (5) | 32-bit | `CPOS` `0x029E` | `08 00 50 B0 05 00 11 02 9E BE` |
| O (6) | 16-bit | `ESTATEV` `0x03B2` | `08 00 60 B0 04 00 11 03 B2 E2` |
| O (6) | 16-bit | `IVERSION` `0x03E8` | `08 00 60 B0 04 00 11 03 E8 18` |
| O (6) | 32-bit | `CPOS` `0x029E` | `08 00 60 B0 05 00 11 02 9E CE` |

- **Opcode allowlist:** `0xB000` (ID query), `0xB004` (16-bit read) and `0xB005` (32-bit read) only. The opcode is the 2 bytes after the destination.
- **Refused by construction:**
  - any other opcode, including `0x0801` (axis-ID write) and `0x0820` (baud);
  - any TML text, `RESET`, `ENDINIT` or `AXISON`;
  - motion, `spi` writes, and `TS_*` calls.
- **Self-check:** the X `CPOS` frame and the Y/Z version frames are byte-identical to the vendor's (`FUN_18002C2C0`, `Y/ZAxisGetScriptVersion`). A test pins them.

### 5.2 Reply decoding
- A 16-bit read returns 13 bytes: `O`, then the frame `len | dest | opcode | sender | var | value(2) | cs`. The value is big-endian at offsets 10–11, as the vendor decodes it (`CONCAT11(b10, b11)`).
- A 32-bit read is expected to return 15 bytes. The tool records the raw bytes and decodes the value in **both** word orders until one is confirmed, for example by `CPOS` agreeing with the stored limits.

## 6. Abort and safety rules

- **Fail fast.** Any timeout, bad frame, unexpected ID or address mismatch stops the whole sequence. The port is closed, and nothing is retried except the bounded line sync of step 1.
- **Rev 2, driver state wrong: stop before sending.** If the read-back DCB fails the §4 driver-state check, nothing is written. The port is closed, and the tool reports the flags it found.
- **Not idle means stop.** If an axis reports `ESTATEV` ≠ 0 or `IINITONCEDONE` ≠ 12345 (X, Y, Z, F), the tool records it and **reads no further axes**. It reports "drives not idle: wait for power-up homing to finish". Reads cannot move anything, but continuing gives no benefit.
- **No retry loops.** The vendor retries; we do not.
- **The incubator is a separate device** (CN board `0DAD:0058`, HID) and is never touched.
- **The LED board** may be in either mode. It is not on this serial link.

## 7. Hazards

| # | Hazard | Control |
|---|---|---|
| M1 | Opening or configuring the port changes a control line in a way that resets or affects a drive | **Rev 2:** the vendor's exact sequence: `GetCommState`, then only the four line fields change, so DTR/RTS stay as the driver set them, which is what the drives see in every PearlScope session. Wrong driver state is refused, not corrected. *(Rev 1's control, "zero-filled DCB like the vendor", was wrong: its one run drove DTR/RTS off with no visible effect and no reply.)* |
| M1a | An `FF` byte at 115,200 reaches drives listening at 9,600 and arrives as a corrupted byte | The vendor's first pass does exactly this on every connection. The Technosoft sync procedure exists to recover framing; with no `0D` the tool moves on to 9,600. No frame is sent until sync succeeds |
| M1b | Revision 1 left the FTDI driver with DTR/RTS disabled | Rev 2 checks the read-back state and refuses if it is not the driver default. The operator resets it by power-cycling, which never happens under program control |
| M2 | A read frame has a side effect in the drive firmware | The frames are byte-identical to the vendor's own probes, used every session. Allowlist of three read opcodes |
| M3 | The wrong port (another FTDI device) | PnP instance-path identity check, exactly one candidate, exclusive open |
| M4 | The drives are still homing during the reads | Reads only, the 2-minute precondition, and the stop-on-not-idle rule |
| M5 | Line contention with another program | Exclusive open; refuse if busy |
| M6 | A reply is misread (word order) | Raw bytes kept; values are CANDIDATE |

## 8. Evidence record

A git-ignored JSON file in `openevos-scans/` holding:
- the port identity (instance path);
- **rev 2:** the DCB and COMMTIMEOUTS as read back before any change (raw bytes and decoded flags), the result of the driver-state check, and the baud pass that answered (or neither);
- every request and reply in raw hex, with timings;
- the decoded values per axis, with the 32-bit values in both word orders;
- a comparison of the versions with `MotorFirmware.ini`;
- the `CPOS` values converted to candidate mm/µm with the §2 units.

## 9. What approval would and would not cover

- **Would cover:** implementing `python -m openevos.maintenance.motor_gate1` exactly as above (dry run by default, tested against a fake serial responder); then, as a **separate** approval, one run on the instrument.
- **Rev 2 adds an inspect-only mode, `--inspect-port`:** open the port exclusively, `GetCommState` and `GetCommTimeouts`, record them, close. **No write, no `SetCommState`.** Phrase: `INSPECT MOTOR PORT COMn`. This tells us, before any byte is sent, whether rev 1 left the driver in the wrong state. It is a separate, smaller approval, run before the full rev 2 run.
- **Would not cover:**
  - the baud change or the axis-ID write;
  - any write, motion, enable, homing, reset or firmware/EEPROM operation;
  - letting the desktop app's command core talk to the drives.

  A motor backend is future work with its own reviews.

## 10. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement the §5 tool (fake device only) | **Approved and implemented** (`maintenance/motor_gate1.py`, ADR-0016); tested against a fake chain only | operator | 2026-11-12 |
| Run it once on the instrument | **Approved; run once: stopped at line sync** (§11) | operator | 2026-11-12 |
| **Rev 2:** revise §4 to the vendor's exact port sequence (spec) | **Revision drafted** at the operator's request; for review | operator | 2026-11-12 |
| Rev 2: implement it (fake device only), including `--inspect-port` | **Approved and implemented**; 718 tests (717 passed, 1 skipped), fake device only | operator | 2026-11-12 |
| Rev 2: run `--inspect-port` once (no bytes sent) | **Approved; run once (§12): driver still holds DTR/RTS disabled** | operator | 2026-11-12 |
| Rev 2: run the full sequence once | **Held** until a later inspect shows the driver's normal state. Condition met at 18:28 UTC (§12a); still needs its own approval | operator | 2026-11-12 |
| Rev 2: second `--inspect-port` after the power cycle | **Approved; run once (§12a): driver state normal** | operator | 2026-11-12 |
| Rev 2: full sequence | **Approved; run once (§13): complete** | operator | 2026-11-13 |

## 13. Full revision 2 run (record `motor-gate1-20261112T183434-complete.json`, UTC stamp): complete

- **Before:** about 6 minutes after the power cycle. The read-back driver state was normal (DTR/RTS ENABLE).
- **Baud:** pass 1 at **115,200 synced on the first `FF`** (`0D` in 16 ms). The 9,600 pass was never needed. No baud or axis-ID command was sent.
- **Traffic:** 253 bytes written in total: 23 syncs, each answered on its first try, plus the 23 fixed frames, each once. Every reply was accepted. Each reply read took about 375–406 ms (50 ms wait + read timeout).
- **Gateway axis ID: 1.** Reply `4F 0A 00 01 B4 04 00 10 00 00 79 80 CC`: opcode `B404` ("take data"), sender `0x0010` (axis 1), byte 3 bit 0 set. The 16-bit value `0x7980` that follows has an unknown meaning.
- **Reply layout (Verified):** for example X `ESTATEV`, `4F 0A 00 11 B4 04 00 10 03 EA 00 00 D0`:
  - destination `0x0011` (host), opcode `B404`, sender axis<<4, the variable echoed, then the value and the checksum;
  - 32-bit replies are 15 bytes (`0C` length), as expected.

| Axis | `ESTATEV` | `IINITONCEDONE` | `IVERSION` (ini) | `CPOS` |
|---|---|---|---|---|
| X (1) | 0 | 12345 | 29 (29) | 0 |
| Y (2) | 0 | 12345 | 29 (29) | 0 |
| P (3) | 0 | — | 6 (6) | 0 |
| Z (4) | 0 | 12345 | 21 (21) | 0 |
| F (5) | 0 | 12345 | 29 (29) | 0 |
| O (6) | 0 | — | 7 (7) | 0 |

### What this establishes
- **Verified:**
  - all six drives are present on COM3 with the expected axis IDs;
  - the binary read protocol and frames work as decoded;
  - the run was read-only, with no state change requested.
- **Verified: X, Y, Z and F completed power-up initialisation** (`IINITONCEDONE` = 12345, idle). The operator also **saw the X/Y stage move by itself after power-on**, a small movement. Whether the focus (Z) or the filter-cube carriage (F) moved was not seen. This is the first direct observation of the autonomous homing in §2:
  - **Verified** for the stage: seen and read back;
  - **Verified by read-back only** for Z and F.
- **Verified: every drive's script version equals `MotorFirmware.ini`.** PearlScope 1.6 would not have rewritten any drive.
- **`CPOS` = 0 on all axes** after homing. This is consistent with homing defining position 0 (Strongly supported). *(Update, same day: the 32-bit word order is now **Verified offline from the vendor's code**. The low word comes first and each word is big-endian, so Gate 1's `i32_word_swapped` field is the vendor's decode. See motor L0 §3a, "32-bit values on the wire".)*
- **The drives answer at 115,200 straight after power-up.** So the L0 assumption "drives answer at 9,600 after reset" is **wrong for this instrument**. Most likely their setup's `SCIBR` sets 115,200, but autobaud cannot be ruled out. PearlScope's connect therefore normally succeeds at 115,200 on its first pass and **never sends its baud-change write**.
- **Revisiting §11:** revision 1 tried only 9,600, and that alone explains its silence. DTR/RTS being disabled is no longer needed to explain it, and whether the drives need those lines is **Unknown**. Revision 2's driver-state check stays: it keeps us identical to the vendor at no cost.

## 12. `--inspect-port` (2026-11-12, 18:24:54 UTC): the driver kept revision 1's settings

- **Done:** COM3 opened exclusively, then `GetCommState` and `GetCommTimeouts`, then closed. **No `SetCommState`, `PurgeComm` or byte written.**
- **Record:** `openevos-scans/motor-gate1-20261112T182454-inspected.json` (git-ignored).
- **Read back:** 9,600 baud, 8 data bits, no parity, 2 stop bits, which is exactly what revision 1 set. Flags: `fBinary = 1`, **`fDtrControl = 0`, `fRtsControl = 0`**, and every flow-control flag 0.
- **Meaning:** the FTDI driver keeps the last DCB across opening and closing the port (**Verified** for this driver). Revision 1's run therefore left DTR/RTS disabled, and they stay disabled until the device re-enumerates. A full run now would have been refused by the §4 driver-state check.
- **Not established:** whether DTR/RTS feed the drives' RS-232 interface. It remains the Probable cause of §11's silence, but has not been shown.
- **Next:**
  1. The operator re-plugs the microscope's USB cable, or power-cycles the instrument, which also lets them watch the power-up homing.
  2. Run `--inspect-port` once more (send nothing). It must read `fDtrControl = 1` and `fRtsControl = 1`.
  3. Only then, the full sequence, under its own approval.

## 12a. Second `--inspect-port` after the re-plug (2026-11-12, 18:28:49 UTC): driver state normal

- **What the operator did (corrected from the operator's own account):** **power-cycled** the microscope and the incubator, switching them off and on. The USB cables were not moved. Power-cycling the microscope re-enumerated its internal FTDI chip, and the drives went through power-up again. OpenEVOS sent nothing to the incubator.
- **Port:** still COM3, with the same FTDI serial number. Nothing was written.
- **Record:** `openevos-scans/motor-gate1-20261112T182849-inspected.json`.
- **Read back:**
  - **`fDtrControl = 1`, `fRtsControl = 1`** (ENABLE), `fBinary = 1`, and all flow control 0;
  - line settings **1,200 baud, 7 data bits, even parity, 1 stop bit**, the driver's defaults after re-enumeration.
- **Meaning:**
  - **Verified:** re-enumeration (here caused by power-cycling the microscope) resets the FTDI driver's cached DCB.
  - The vendor and rev 2 change only those four line fields (to 115,200 or 9,600 8N2), so the DTR/RTS state the drives see now matches PearlScope's.
- The driver-state check passes. The full sequence is still held pending its own approval.

## 11. First run (2026-11-12, 18:15:49 UTC): no reply to line sync

- **Port:** COM3, FTDI `0403:6001`, the only candidate; opened exclusively without error.
- **Sent:** 5 × `FF` at 9,600 8N2 with the zero-filled DCB (DTR and RTS control disabled), about 200 ms apart. **Nothing else was sent:** no axis-ID query and no reads. The port was closed.
- **Received:** nothing, not even a single byte, within 200 ms of each attempt.
- **Record:** `openevos-scans/motor-gate1-20261112T181551-stopped.json` (git-ignored).

### Why: re-reading the vendor's port-open routine (`FUN_180041560`) found two errors in §4
1. **DCB.** The vendor zero-fills the DCB and then calls `GetCommState`, which overwrites it with the port's current settings. Only BaudRate, ByteSize, Parity and StopBits are then changed. So the vendor keeps the driver's DTR/RTS settings, normally asserted on an FTDI port, whereas this tool **turned DTR and RTS off**. If the drives' RS-232 interface takes its power or enable from those lines, it could not answer (Probable cause; not proven).
2. **Baud order.** The vendor's first pass is 115,200, then 9,600. Drives left at 115,200 by an earlier PearlScope session, without a power cycle since, would not answer at 9,600. This is less likely, because the instrument has been power-cycled since PearlScope last ran, as far as is known.

The frames, the sync byte (`FF`, then `0D` within 15 tries at 10 ms) and the axis-ID query match the vendor exactly (re-checked).

### Status
- No retry was made. Any change to the port configuration needs a revised spec and a new approval.
- Hazard M1's control was based on the wrong DCB and needs re-review.
