# ADR-0011: Gate 1, the LED board bootloader version query, as an operator-run tool

- **Status:** Accepted: implemented and tested against a fake device. Run once on the instrument on 2026-11-12, operator-approved: a valid reply reporting application version 3.0.25949, with the board unchanged afterwards. The result is recorded in §6 of the evidence doc.
- **Builds on:**
  - ADR-0009: firmware safety, L3 reviewed reads;
  - [§6 Step A of the LED board static analysis](../evidence/fl1_v1.6_led_board_static_analysis.md).

## Context

Our LED board enumerates as its bootloader (`0DAD:0057`). Gate 1 asks it one question: the version query, command `0x0A`. The vendor DLL's `GetApplicationFirmwareVersion` sends exactly this request and nothing else (Strongly supported from the disassembly). The answer decides whether Gate 2 (checksum and calibration reads, which need the application running) is worth discussing at all.

The command core quarantines a board in bootloader mode (ADR-0009), and that stays true. This read therefore cannot be a core command.

## Decision

Gate 1 is an operator-run command-line tool, `python -m openevos.maintenance.led_bootloader`. It sits outside the command core and the UI.

### What it sends
- **One fixed report:** report ID `0x00`, command `0x0A`, then 63 zero bytes, with no checksum.
- **Nothing else can be built.** `build_request` refuses every command byte except `0x0A`, so neither StartApp (`0x08`) nor the Gate 2 commands (`0x59`, `0x51`) are possible here.
- **One target only:** exactly one present `0DAD:0057` interface. No other PID is ever looked up.

### Checks before any I/O
- The HID class driver must itself report VID/PID `0DAD:0057`.
- Its input and output report sizes must both be 65 bytes.
- These come from a zero-access handle, which sends nothing to the board.

### The exchange
Handled by `maintenance/win_hid.py`:
- one handle opened with `FILE_FLAG_OVERLAPPED`;
- `HidD_FlushQueue`, then one `WriteFile`, then one `ReadFile`;
- 500 ms limit on each step, with `CancelIoEx` on timeout;
- no retry, and no loop on "busy" (`0xF0`).

### Operator control
- **Dry run by default:** it finds and describes the board, prints the exact bytes, and sends nothing.
- **Sending needs both** `--send` and the exact phrase `SEND 0A TO 0DAD:0057`.

### How the reply is judged
The vendor's own rules (§6):
- byte 1 must be `0x8A`;
- BE32 at bytes 2–5 must be ≥ 2, and BE32 at bytes 6–9 ≥ 1. What these two fields mean is still a **hypothesis**.

The decoded application version is BE32 major / minor / build at bytes 22–25 / 26–29 / 30–33. Every byte that isn't decoded is kept raw.

The tool's verdict:

| Reply | Verdict |
|---|---|
| Valid, application version not blank | Evidence that an application image is present. StartApp still needs its own L4 review. |
| Valid, application version blank | StartApp must not be considered. |
| Invalid | StartApp must not be considered. |

### Evidence
Each dry run or send writes a JSON record to `openevos-scans/`, which is git-ignored because the device path identifies the unit. The record holds:
- the request bytes;
- the reply bytes;
- the decoded fields;
- timing.

### Guards
- **Registry:** `safety/firmware.py` lists the read in `REVIEWED_L3_READS`. A test checks that the tool's allowlist and target match that entry exactly.
- **Core still refuses it:** `operation_refusal` keeps refusing the same name inside the command core.
- **Not reachable from the app:** the import guard forbids `core/`, `hal/`, `ui/` and `backends/` from importing `openevos.maintenance`.
- **No firmware vocabulary:** the firmware vocabulary scan also covers `maintenance/`.

## Consequences

- **Running it is a separate operator decision.** Nothing has been sent. The recommended sequence:
  1. a dry run, which sends nothing;
  2. then one send, after the operator types the phrase.
- **Unknown risk remains.** What command `0x0A` does inside the bootloader firmware cannot be proven offline, because `Pearl_Bootloader.hex` is not in the package. The host-side evidence is the vendor's own use of it as a plain read.
- **Gate 2 is not implemented.** It stays held until Step A's result has been reviewed and a separate L4 review approves leaving the bootloader.
- **Tests:** `tests/test_maintenance_gate1.py` (fake HID backend). It shows:
  - the exact bytes;
  - exactly one flush, one write and one read;
  - nothing sent on a dry run, on any refusal, or without the exact phrase;
  - no retry on timeout or busy;
  - the reply rules and the evidence record;
  - that the real Windows enumeration opens nothing.
