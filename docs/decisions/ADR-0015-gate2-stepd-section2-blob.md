# ADR-0015: Gate 2 Step D, PearlScope's calibration blob from LED persistent-data section 2

- **Status:** Accepted: implemented and tested against a fake device (2026-11-12). Running it is a separate operator approval; the header-only run and the full blob read are separate approvals.
- **Builds on:**
  - ADR-0009, and ADR-0012 to ADR-0014;
  - static analysis §5.5a, which found that PearlScope keeps camera, motor-controller and turret data as one compressed, serialized blob in section 2.

## Decision

`python -m openevos.maintenance.led_section2_read` reads that blob, and nothing else.

### What can be built
- **Only command `0x31`** (`GetPersistentData2Vector`'s wire command; reply `0xB1`). The framing is as for `0x51`: BE24 address, count in byte 5, two's-complement checksum.
- **The four header reads**, at the vendor's own sizes: `0x15713`/4 (magic), `0x15717`/8, `0x1571F`/4 (decoded size), `0x15723`/4 (stored length *N*).
- **Blob chunks** of at most 58 bytes at `0x15727 + k·58`, never past the vendor's limit (*N* < 35,034).

### Preconditions
The same as Step C:
- one `0051` interface, no `0057`/`0052`;
- 65/65-byte reports;
- a Step B record under 30 minutes old with both sections consistent;
- the exact phrase `READ SECTION 2 BLOB FROM 0DAD:0051`.

Dry run by default.

### Sequence
1. **Header.** Read the 4 fields. Stop unless the magic is "lico" and 0 < *N* < 35,034. `--header-only` stops here deliberately: a small first step (4 requests) before about *N*/58 more.
2. **Blob.** Read ⌈*N*/58⌉ chunks, each requested once, failing fast on any timeout, BUSY, bad frame, wrong count or address mismatch. A partial blob is kept as evidence.

Per request: flush, one write, then up to 3 passive reads within 1 s.

### Output and decoding
- Everything is git-ignored: the JSON, the blob as `.bin`, and the offline decode result.
- Decoding is attempted offline only, as zlib, raw deflate or gzip. If none fits, the tool says so rather than guessing.
- The contents are **CANDIDATE** data and are not interpreted by this tool.

### Guards
- `REVIEWED_L3_READS["led_application.section2_blob_read"]`.
- The firmware-name scan, the import guard and the core's refusal apply as before.
- `SetPersistentData2*` (writes) stay banned.

## First run (2026-11-12, 16:26 UTC): the calibration blob is present and decodes

### Sequence (operator-approved; the full read was approved conditionally on the header passing)
1. **Fresh Step B** (16:25:58): both sections consistent, unchanged (`0x0001BF60` / `0xC5324E58`).
2. **Header-only read**, 4 requests. Record: `…stepd-20261112T162559-header_only.json`.

   | Field | Value |
   |---|---|
   | magic | `6C 69 63 6F` "lico" ✓ |
   | header value | `00 00 00 00 00 00 F0 3F` = double **1.0**, most likely a format version |
   | decoded size | **5,660** bytes |
   | stored length *N* | **1,305** bytes, i.e. 23 chunks |

3. **Full read:** 4 + 23 requests, each sent once, all accepted. Record: `…stepd-20261112T162611-complete.json`, with `-blob.bin` and `-decoded.bin`, all git-ignored.
   - Offline `zlib` decode succeeded, and the output is exactly the declared 5,660 bytes.
4. **Final Step B:** checksums unchanged. Board unchanged: `0051` and `0058` present.

### What the decoded stream contains
Structure only. The values stay in the git-ignored files.
- 80 UTF-16LE text fields, laid out as PearlScope's **objective table**, one group per magnification (2×, 4×, 10×, 20×, 40×, 50×).
- Each group has a `|`-separated list of objective models with catalogue numbers, then several rows of per-objective numbers:
  - one row falls with magnification, which looks like µm per pixel;
  - signed integer rows, plausibly X/Y and focus offsets;
  - an NA-like row and type-code rows.
- This matches what the TurretPositionController reader writes to `[ObjectiveLenses]` ("-Calibrations", "-Parfocalities").
- Binary fields between the strings belong to the camera and motor readers, including the X/Y/Z mm-per-revolution doubles. Their layout needs the full reader decompilation before any value is parsed.

### Status
- **Verified:** the master calibration store PearlScope rebuilds `PearlScope.ini` from is present on this unit's LED board, intact, and readable.
- **All values remain CANDIDATE.**
- **Parsed exactly (same day, offline):** the schema derived from the decompiled readers is in [`fl1_v1.6_pearl_persistent_schema.md`](../evidence/fl1_v1.6_pearl_persistent_schema.md), and the parser is `openevos.maintenance.pearl_persistent`.
  - The 5,660 bytes parse with **zero bytes left over**, in the **pre-1.6 layout**: each objective slot lacks the `-APO` string that 1.6 reads and writes. So the blob was written by older PearlScope software.
  - The structure is Verified. The values stay CANDIDATE until their meanings and physical behaviour are checked.

## Consequences

- **Recommended order:**
  1. a fresh Step B;
  2. the dry run;
  3. a header-only send;
  4. after review of the header, the full read;
  5. a fresh Step B to confirm the checksums are unchanged.
- **Interpreting the decoded blob** (which controller's fields are where, and their units) is offline work that follows. Only after physical checks may any value move beyond CANDIDATE.
- **Tests:** `tests/test_maintenance_section2_read.py`. It covers:
  - framing, and that only reviewed reads can be built;
  - a complete read with zlib decode;
  - the header-only stop;
  - the magic and length gates;
  - fail-fast mid-blob with the partial blob kept;
  - an unrecognised format reported, not guessed;
  - the address-echo check;
  - the refusals, dry run and CLI files.
