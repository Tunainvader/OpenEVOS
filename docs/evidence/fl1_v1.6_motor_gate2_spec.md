# Motor Gate 2: read-only check of the drives' safety-relevant variables (specification, for review)

- **Status:** implemented (ADR-0017) and **run once on 2026-11-13: complete** (§8). Power-up limits: X/Y 0–22 mm; Z −1,775…67,000 counts (matches the stored calibration); `CACCSTOP` = 1.0.
- **Date:** 2026-11-13.
- **Builds on:**
  - Motor Gate 1 rev 2 ([spec](fl1_v1.6_motor_gate1_spec.md), ADR-0016), whose transport and procedure are reused unchanged;
  - motor L0 §3a ("Stop, halt and move", "Remaining stop/move items", "32-bit values on the wire").

## 1. Purpose

Before any stop or move tool can be designed, learn from the drives themselves:
1. **The limits each drive applies to every move** (`LPOSMIN`/`LPOSMAX`). The drive's `CLAMPTOLIMITS` routine is the **only** position check for relative X/Y moves and for Z and F moves (the PC checks nothing there; L0 §3a).
2. **The stop deceleration** (`CACCSTOP`) that a controlled stop (`STATE_6_STOP`) or a halt would use.
3. **The speed and acceleration a move would use** (`CSPDCMD`/`CACCCMD`). PearlScope skips rewriting these when unchanged, so their current values matter.
4. **Actual versus commanded position** (`APOS`/`CPOS`) on all six axes.

> **⚠ Correction after the run (§8):** the next paragraph is **wrong**. The drive programs' `VAR_INIT` does set `LPOSMIN`, `LPOSMAX` and `CACCSTOP`, with short-form 32-bit writes whose opcode carries the address (`25B4`/`25B6`/`25E8` on X, `25BD`/`25BF`/`25BB` on Z). The offline search looked only for the address as a separate word. The original text is kept below for the record.

### Why this is open: the drive programs never set these variables (Verified, offline; ⚠ wrong, see the correction above)
In the X, Z and F `.sw` images, every reference to `LPOSMIN`, `LPOSMAX` and `CACCSTOP` is a **read**: inside `CLAMPTOLIMITS`, `STATE_6_STOP` and `STATE_*_HALT`. Nothing in `VAR_INIT` or `INIT_ONCE` writes them. Only the host does: PearlScope writes the `mcba` limits at start-up (`SetXAxisPositionLimits`).

The instrument has been power-cycled without PearlScope, so the drives now hold their **power-up values**. Those may be 0, or the setup-table defaults. **Unknown.**

If `LPOSMIN` = `LPOSMAX` = 0, every clamped move goes to 0. If `CACCSTOP` = 0, a stop or halt has no deceleration defined. Either way, a stop or move tool must not exist until this is known.

## 2. Reused from Gate 1 rev 2, unchanged
- **Port identification:** one FTDI `0403:6001`.
- **Driver-state check:** DTR/RTS ENABLE, no flow control.
- **Port setup:** read-back DCB with only the four line fields changed, and the vendor's timeouts.
- **Baud passes:** 115,200, then 9,600. Never a baud command.
- **Each frame:** its own line sync, then one write, 50 ms, and one 32-byte read.
- **Checks and decoding:** fail-fast reply checks (`O`, length, checksum, sender, echoed variable). 32-bit values are decoded in the **vendor order** (low word first, each word big-endian; Verified).
- **Preconditions and safeguards:** the 2-minute precondition, a dry run by default, and git-ignored evidence.
- **Opcode allowlist:** `0xB000`/`0xB004`/`0xB005` only. No write opcode (`0x20xx`/`0x21xx`/`0x24xx`/`0x25xx`, `0x0002`, `0x0102`, `0x0108`, `0x01C4`, `0x0801`, `0x0820`, `0x2Cxx`/`0x3Cxx`) can be built.

Confirmation phrase: **`READ MOTOR LIMITS ON COMn`**. It is deliberately different from Gate 1's.

## 3. The reads (41 constants, generated from each axis' own `variables.cfg`)

**Variable addresses differ per axis.** Every frame below comes from that axis' hash-verified `variables.cfg`, and a test pins each one. The 32-bit reads use opcode `B005`, the 16-bit reads `B004`.

**Order:**
1. Gateway axis-ID query.
2. The readiness reads, all first, in the order X, Y, P, Z, F, O. **Stop** if any axis is not idle, or if `IINITONCEDONE` ≠ 12345 on X/Y/Z/F.
3. The parameter reads, axis by axis.

| Axis (ID) | Variable | Type | Address | Frame |
|---|---|---|---|---|
| X (1) | `ESTATEV` | INT | `0x03EA` | `08 00 10 B0 04 00 11 03 EA CA` |
| X (1) | `IINITONCEDONE` | INT | `0x03FE` | `08 00 10 B0 04 00 11 03 FE DE` |
| X (1) | `APOS` | LONG | `0x0228` | `08 00 10 B0 05 00 11 02 28 08` |
| X (1) | `CPOS` | LONG | `0x029E` | `08 00 10 B0 05 00 11 02 9E 7E` |
| X (1) | `LPOSMIN` | LONG | `0x03B4` | `08 00 10 B0 05 00 11 03 B4 95` |
| X (1) | `LPOSMAX` | LONG | `0x03B6` | `08 00 10 B0 05 00 11 03 B6 97` |
| X (1) | `CACCSTOP` | FIXED | `0x03E8` | `08 00 10 B0 05 00 11 03 E8 C9` |
| X (1) | `CSPDCMD` | FIXED | `0x03D6` | `08 00 10 B0 05 00 11 03 D6 B7` |
| X (1) | `CACCCMD` | FIXED | `0x03D8` | `08 00 10 B0 05 00 11 03 D8 B9` |
| Y (2) | `ESTATEV` | INT | `0x03EA` | `08 00 20 B0 04 00 11 03 EA DA` |
| Y (2) | `IINITONCEDONE` | INT | `0x03FE` | `08 00 20 B0 04 00 11 03 FE EE` |
| Y (2) | `APOS` | LONG | `0x0228` | `08 00 20 B0 05 00 11 02 28 18` |
| Y (2) | `CPOS` | LONG | `0x029E` | `08 00 20 B0 05 00 11 02 9E 8E` |
| Y (2) | `LPOSMIN` | LONG | `0x03B4` | `08 00 20 B0 05 00 11 03 B4 A5` |
| Y (2) | `LPOSMAX` | LONG | `0x03B6` | `08 00 20 B0 05 00 11 03 B6 A7` |
| Y (2) | `CACCSTOP` | FIXED | `0x03E8` | `08 00 20 B0 05 00 11 03 E8 D9` |
| Y (2) | `CSPDCMD` | FIXED | `0x03D6` | `08 00 20 B0 05 00 11 03 D6 C7` |
| Y (2) | `CACCCMD` | FIXED | `0x03D8` | `08 00 20 B0 05 00 11 03 D8 C9` |
| P (3) | `ESTATEV` | INT | `0x03B2` | `08 00 30 B0 04 00 11 03 B2 B2` |
| P (3) | `APOS` | LONG | `0x0228` | `08 00 30 B0 05 00 11 02 28 28` |
| P (3) | `CPOS` | LONG | `0x029E` | `08 00 30 B0 05 00 11 02 9E 9E` |
| Z (4) | `ESTATEV` | INT | `0x03B2` | `08 00 40 B0 04 00 11 03 B2 C2` |
| Z (4) | `IINITONCEDONE` | INT | `0x03F6` | `08 00 40 B0 04 00 11 03 F6 06` |
| Z (4) | `APOS` | LONG | `0x0228` | `08 00 40 B0 05 00 11 02 28 38` |
| Z (4) | `CPOS` | LONG | `0x029E` | `08 00 40 B0 05 00 11 02 9E AE` |
| Z (4) | `LPOSMIN` | LONG | `0x03BD` | `08 00 40 B0 05 00 11 03 BD CE` |
| Z (4) | `LPOSMAX` | LONG | `0x03BF` | `08 00 40 B0 05 00 11 03 BF D0` |
| Z (4) | `CACCSTOP` | FIXED | `0x03BB` | `08 00 40 B0 05 00 11 03 BB CC` |
| Z (4) | `CSPDCMD` | FIXED | `0x03B5` | `08 00 40 B0 05 00 11 03 B5 C6` |
| Z (4) | `CACCCMD` | FIXED | `0x03B7` | `08 00 40 B0 05 00 11 03 B7 C8` |
| F (5) | `ESTATEV` | INT | `0x03B6` | `08 00 50 B0 04 00 11 03 B6 D6` |
| F (5) | `IINITONCEDONE` | INT | `0x03F8` | `08 00 50 B0 04 00 11 03 F8 18` |
| F (5) | `APOS` | LONG | `0x0228` | `08 00 50 B0 05 00 11 02 28 48` |
| F (5) | `CPOS` | LONG | `0x029E` | `08 00 50 B0 05 00 11 02 9E BE` |
| F (5) | `LPOSMIN` | LONG | `0x03EA` | `08 00 50 B0 05 00 11 03 EA 0B` |
| F (5) | `LPOSMAX` | LONG | `0x03EC` | `08 00 50 B0 05 00 11 03 EC 0D` |
| F (5) | `CSPDCMD` | FIXED | `0x03BE` | `08 00 50 B0 05 00 11 03 BE DF` |
| F (5) | `CACCCMD` | FIXED | `0x03C0` | `08 00 50 B0 05 00 11 03 C0 E1` |
| O (6) | `ESTATEV` | INT | `0x03B2` | `08 00 60 B0 04 00 11 03 B2 E2` |
| O (6) | `APOS` | LONG | `0x0228` | `08 00 60 B0 05 00 11 02 28 58` |
| O (6) | `CPOS` | LONG | `0x029E` | `08 00 60 B0 05 00 11 02 9E CE` |

The table has 41 reads (X 9, Y 9, P 3, Z 9, F 8, O 3); with the gateway ID query, 42 requests. Each is preceded by its line sync.

**Deliberately not read:**
- **F `CACCSTOP`:** it does not exist. F's stop is `STATE_15_STOP`, not yet decoded.
- **P/O limits and stop parameters:** these variables exist, but the P/O programs never use them (L0 §3a), so reading them would teach nothing.
- **P/O `IINITONCEDONE`:** it does not exist.

## 4. Decoding and reporting
- **`LONG` (`APOS`, `CPOS`, `LPOSMIN`, `LPOSMAX`):** a signed 32-bit integer in the vendor order, plus the raw bytes. X/Y/Z values are converted to candidate mm with the L0 units: X/Y 2,880 counts/rev at 1.27 mm/rev; Z 5,000 counts/rev at 0.61 mm/rev.
- **`FIXED` (`CACCSTOP`, `CSPDCMD`, `CACCCMD`):** high word = integer part, low word = fraction / 65,536 (from the vendor's `FUN_18003A080`), in drive internal units (counts per sampling period, or per period²). The tool records them and does not convert them; the sampling period is not yet known.
- **Automatic comparison, offline and in the same run, sending nothing:**
  - `LPOSMIN` < `LPOSMAX` on X/Y/Z/F, both non-zero, and the span in mm compared with the `mcba` spans (119.1 / 78.1 / 8.4 mm);
  - if the git-ignored decoded calibration (Step D, `pearl_persistent` output) is present in `openevos-scans/`, each axis' limits are compared with its `mcba` min/max, reported as equal, different or zero;
  - `CACCSTOP` > 0 on X/Y/Z;
  - `|APOS − CPOS|` per axis, as the holding error in counts.
- The report labels every value **CANDIDATE**, and says plainly whether the drives' own clamps are currently meaningful.

## 5. Stop and abort rules
As Gate 1:
- fail fast;
- no frame is ever resent;
- not idle means stop **before** any parameter read;
- a driver state other than the normal one means refuse before any byte is sent.

The incubator and the LED board are not touched.

## 6. Hazards

| # | Hazard | Control |
|---|---|---|
| G2-1 | A read has a side effect | The same `B004`/`B005` read opcodes Gate 1 used without effect, and the same reply checks. The vendor itself reads `LPOSMIN`/`LPOSMAX`/`CPOSCMD`/`CSPDCMD`/`CACCCMD` this way (`GetXAxisPositionLimits`, drive-reset path) |
| G2-2 | A wrong per-axis address reads an unrelated variable | Frames are generated from each axis' `variables.cfg` and pinned in tests; the reply's echoed variable address is checked |
| G2-3 | A misread value is trusted | The word order is Verified; raw bytes are kept; values are CANDIDATE; nothing acts on them |
| G2-4 | Drives are moving during the reads | The readiness reads come first, with the stop-on-not-idle rule and the 2-minute precondition |

## 7. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement the §3 tool (fake device only) | **Approved and implemented** (`maintenance/motor_gate2.py`, ADR-0017); fake chain only | operator | 2026-11-13 |
| Run it once on the instrument | **Approved; run once: complete (§8)** | operator | 2026-11-13 |

## 8. First run (record `motor-gate2-20261112T190810-complete.json`): complete

- **Run:** 115,200 baud; gateway ID 1; every axis idle and X/Y/Z/F homed. All 41 reads were answered, each once. No write was possible or sent.
- **Context:** the instrument had been power-cycled earlier, and PearlScope has not run since. These are therefore the **drives' own power-up values**.

| Axis | `APOS` / `CPOS` | `LPOSMIN` … `LPOSMAX` (counts) | Span (candidate) | `CACCSTOP` | `CSPDCMD` | `CACCCMD` |
|---|---|---|---|---|---|---|
| X | 0 / 0 | 0 … 50,000 | **22.05 mm** (stored calibration: 119.1) | 1.0 | 15.0 | 1.0 |
| Y | 0 / 0 | 0 … 50,000 | **22.05 mm** (stored calibration: 78.1) | 1.0 | 15.0 | 1.0 |
| Z | 0 / 0 | −1,775 … 67,000 | **8.39 mm** (stored calibration: 8.4, **matches**) | 1.0 | 10.0 | 0.25 |
| F | 0 / 0 | 0 … 128,000 | units not established | — | 20.0 | 0.03 |
| P, O | 0 / 0 | not read | — | — | — | — |

The fixed-point values are in drive internal units (counts per sample, or per sample²). The sampling period is not yet known.

### What this establishes
- **32-bit word order Verified on hardware:**
  - Z `LPOSMIN` raw `F9 11 FF FF` decodes in the vendor order to **−1,775**, a sensible negative limit. The other order would give −61,163,521.
  - Z `LPOSMAX` raw `05 B8 00 01` decodes to 67,000.
  - X's `.sw` writes 50,000 as `25 B6 C3 50 00 00`, the same order.
- **The drives start with their own limits, set by `VAR_INIT`, not zero.**
  - **X and Y are confined to about 0–22 mm from home** (0–50,000 counts) until the host widens the limits. PearlScope does this at start-up with the `mcba` values.
  - **Z's power-up limits already match the stored calibration span** (8.39 mm).
- **The stop deceleration is defined:** `CACCSTOP` = 1.0 on X/Y/Z, so `STATE_6_STOP` has a non-zero deceleration.
- **Every axis sits exactly at its commanded position:** `APOS` = `CPOS` = 0, a holding error of 0 counts.
- **Consequence for any future move tool:** after a power-up without PearlScope, the drives clamp X/Y moves to about 22 mm. That is safe, but a target beyond it would be **silently clamped**, not refused. A tool must read the limits first, as here, and refuse targets outside them itself.
- **Still open:**
  - the F counts-to-mm units;
  - the drive sampling period, needed to convert the fixed-point speed and acceleration values;
  - whether `CLAMPTOLIMITS` reports a clamp anywhere.
