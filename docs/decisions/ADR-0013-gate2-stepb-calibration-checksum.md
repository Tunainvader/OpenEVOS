# ADR-0013: Gate 2 Step B, the LED application's calibration-memory checksum query

- **Status:** Accepted: implemented and tested against a fake device (2026-11-12). Each run is a separate operator approval.

## First attempt (2026-11-12, about 15:10 UTC): wrong frame checksum, answered BUSY

- **What was sent:** one approved request after a passing dry run, with byte 64 = `0x59`. That was the plain sum; the spec at the time said "additive".
- **The reply:** `00 F0 …` with byte 64 = `0x10`, i.e. "busy"/not processed. As designed, the tool did not resend.
- **Board afterwards:** unchanged. `0051` still present, `0058` present, no `0057`.
- **Finding:** `0x10` is the two's complement of `0xF0`. Decompiling `FUN_180033110` confirmed that it returns `~sum + 1`. The board most likely rejected our frame. `0x59` is a read in any case.
- **Fix:** byte 64 is now `0xA7`. Every parsed reply, including BUSY, must pass the two's-complement check. A test pins the board's own BUSY frame as a known-good example.
- **Status of the query:** completed on the second, separately approved attempt (below).

## Second attempt (2026-11-12, 15:19 UTC): consistent

- **Context:** after a power cycle and a fresh StartApp, with a dry run first.
- **Request:** one request with the corrected frame, `00 59 … A7`.
- **Reply:** `00 D9 …`, frame checksum valid, first report read. Record: `…stepb-20261112T151928-reply.json`.

| Section | Stored | Computed | Result |
|---|---|---|---|
| 1 (`HW_DATA`) | `0x0001BF60` | `0x0001BF60` | **agree** |
| 2 | `0xC5324E58` | `0xC5324E58` | **agree** |

- **Bytes 18–63:** zero.
- **Board afterwards:** unchanged. `0051` present, no `0057`, `0058` present.
- **Verdict:** the calibration memory on the board is **internally consistent** (Verified for this query). This does not show the values are correct for this instrument. That needs Step C (reading them into a `CANDIDATE` record) and physical checks.
- **Builds on:**
  - ADR-0009: firmware safety, L3 reviewed reads;
  - ADR-0011: Gate 1;
  - ADR-0012: StartApp;
  - static analysis §5.3 and §6 Step B.

## Context

The LED board stores the instrument's stage calibration (`HW_DATA`) in its persistent memory. StartApp has been run once, and the board's application (`0DAD:0051`) is now present. The vendor application interface offers `GetPersistentDataChecksum`, command `0x59` → reply `0xD9`. It returns two numbers:
- the checksum **stored** for that memory;
- the checksum **computed** over it now.

It returns no calibration values. It is the cheapest test of whether the calibration on the board is intact, and it decides whether Step C (reading the values with `0x51`) is worth doing.

## Decision

`python -m openevos.maintenance.led_checksum` sends this one query, and nothing else.

### Target
- Exactly one `0DAD:0051` interface, with no `0057` or `0052` present.
- The driver reports `0DAD:0051` with 65/65-byte reports (zero-access handle).
- A StartApp `app_appeared` record must list this interface. That ties the query to the application OpenEVOS started.

### Request
One 65-byte report: `00 59`, 62 zero bytes, and the application-protocol frame checksum in byte 64. That checksum is the two's complement of the 8-bit sum of bytes 1–63, here `0xA7`. Only `0x59` can be built.

### Exchange
- `HidD_FlushQueue`, then one `WriteFile` (500 ms).
- Then **reads only**: at most 5 input reports within 1 s, accepting the first with reply code `0xD9` and a valid frame checksum.
- Other reports are recorded raw and ignored. (The vendor DLL discards non-matching replies the same way, but also resends; we never resend.)
- "Busy" (`0xF0`) is reported, not retried.

### Two sections in one reply
The same `0xD9` reply carries **both** persistent-data sections (decompiled, §5.3):
- section 1, which holds `HW_DATA`: stored / computed at bytes 2–5 / 6–9;
- section 2: bytes 10–13 / 14–17.

Both are decoded; the verdict below is about section 1.

### Verdict

| Result | Verdict |
|---|---|
| stored = computed | The calibration memory is internally consistent. This does **not** prove the values are right for this instrument. |
| stored ≠ computed | Inconsistent: do not trust or use `HW_DATA`, and do not go on to Step C without review. |

### Operator control
- Dry run by default.
- Sending needs `--send --confirm "SEND 59 TO 0DAD:0051"`.
- The JSON evidence goes to `openevos-scans/` (git-ignored).

### Registry and guards
- `REVIEWED_L3_READS["led_application.calibration_checksum"]` in `safety/firmware.py`; a test checks the tool matches it.
- The command core still refuses it.
- The import guard keeps maintenance tools out of the app.
- The firmware-name scan covers the module.

## Consequences

- **Running it is a separate approval.** It can run only while the board is in its application, i.e. before the next power cycle.
- **Step C stays held** until this result has been reviewed: reading the `HW_DATA` values with `0x51` into a `CANDIDATE` capability record. Writes to that memory remain banned (ADR-0009).
- **Tests:** `tests/test_maintenance_checksum.py`. It covers:
  - the exact request and its frame checksum;
  - one write, then reads only (at most 5);
  - unrelated or corrupt reports ignored without a resend;
  - busy and timeout;
  - every precondition;
  - nothing sent in a dry run or without the exact phrase;
  - the evidence record.
