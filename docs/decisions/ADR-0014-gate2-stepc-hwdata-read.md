# ADR-0014: Gate 2 Step C, field-by-field `HW_DATA` read from the LED application

- **Status:** Accepted: implemented and tested against a fake device (2026-11-12). Running it is a separate operator approval.
- **Builds on:**
  - ADR-0009: L3 reviewed reads, and the `HW_DATA` write ban;
  - ADR-0012: StartApp;
  - ADR-0013: Step B, both persistent-data sections consistent;
  - [`fl1_v1.6_hwdata_address_map.md`](../evidence/fl1_v1.6_hwdata_address_map.md).

## Decision

`python -m openevos.maintenance.led_calibration_read` reads the 76 mapped `HW_DATA` fields, and nothing else.

### Field list
- `maintenance/hwdata_fields.py`, generated from the address map: `0x141D`–`0x15BE`.
- Each field is read at the vendor reader's own size: 4 bytes as LE `int32`, 8 bytes as LE `double`.
- `build_request` refuses any command except `0x51`, and any (address, size) pair not in the map. That rules out bulk reads and the unmapped `0x1469` 1-byte/16-byte pair.

### Request
`00 51`, BE24 address, count, zeros, and the two's-complement frame checksum in byte 64. Bytes 1–64 sum to zero.

### Preconditions
- Exactly one `0DAD:0051` interface, with no `0057`/`0052`.
- The driver reports `0DAD:0051` with 65/65-byte reports.
- A Step B record for the same interface, at most 30 minutes old, with **both** sections consistent.
- The exact phrase `READ 76 HW_DATA FIELDS FROM 0DAD:0051`.
- Dry run by default.

### Exchange, per field
- Flush, then **one** write (500 ms).
- Then up to 3 passive reads within 1 s, accepting `0xD1` with a valid frame and `reply[5] == count`.
- Unrelated reports are kept raw.

### Fail-fast
On any write failure, timeout, BUSY or missing match, the **whole sequence stops**. No field is ever requested twice.

### Output
A git-ignored JSON file containing:
- the raw requests and replies;
- the decoded values;
- plausibility checks: doubles finite, axis min < max, mm/rev > 0, count of 0/−1 fields.

Values are **CANDIDATE** data with **unknown units**: the `int32` limits and offsets are probably encoder counts. This tool does not turn them into capability limits. That needs the units established and physical checks.

### Guards
- `REVIEWED_L3_READS["led_application.calibration_read"]`; a test checks the tool matches it.
- The firmware-name scan, the import guard, and the core's continued refusal all apply as for ADR-0011 to ADR-0013.

## First run (2026-11-12, 16:06 UTC): every mapped field is zero

- **Context:** after a fresh, consistent Step B (`…stepb-20261112T160450-reply.json`, identical to 15:19) and a passing dry run.
- **Requests:** all 76 sent, one request each, sequence `complete`. Record: `…stepc-20261112T160614-complete.json`.
- **Replies:** every one is `0xD1` with a valid frame checksum, the requested count echoed, and **the requested address echoed in bytes 2–4** (76 of 76). Bytes after each payload hold leftovers of the earlier `0x59` reply, so the firmware appears to reuse one reply buffer.
- **Values:** **every field is exactly 0**: 69 `int32` and 7 `double`, all raw bytes `00`. So every plausibility check fails (min < max, mm/rev > 0).
- **Board afterwards:** unchanged. `0051` present, `0058` present.
- **The contradiction:** section 1's stored and computed checksum is `0x0001BF60`, which is **non-zero**. Section 1 therefore holds data somewhere, but not at these 76 addresses as we addressed them. Possible explanations, **none established**:
  1. The address argument of `GetPersistentData` is transformed before use (a base offset, or word rather than byte addressing). Against this: the board echoes our address unchanged.
  2. The address map from `GetHardwareSpecificData` is right, but this unit's calibration was never stored on the board. PearlScope may have kept it in the host-side `PearlScope.ini` (lost with the HDD); compare `GetHardwareIniDataStored`, slot 128.
  3. The calibration sits in a region these accessors don't use.
- **Status:** **no usable calibration values.** Nothing goes into a capability record.
- **Resolved offline (same day, static analysis §5.5a):**
  - Explanation 1 is ruled out: the address goes to the board unchanged.
  - PearlScope keeps the calibration (camera, motor-controller and turret data) in a **compressed serialized blob in persistent-data section 2**. It starts at `0x15713` with the magic "lico", and is read with command `0x31` in 58-byte chunks.
  - The `HW_DATA` fields are an unused legacy interface on this software generation.
  - Reading the blob needs a new reviewed tool ("Step D").

## Consequences

- **After a run**, re-run Step B to confirm the checksums are unchanged. Reads should not change them.
- **The 42 unmapped fields stay unread** (strings, tables, turret settings) until they are mapped.
- **Tests:** `tests/test_maintenance_calibration_read.py`. It covers:
  - the map and the framing;
  - only mapped reads can be built;
  - one write per field;
  - noise tolerated without resending;
  - fail-fast on BUSY and on timeout;
  - count-echo and frame checks;
  - the plausibility checks;
  - every precondition, and nothing sent in a dry run.
