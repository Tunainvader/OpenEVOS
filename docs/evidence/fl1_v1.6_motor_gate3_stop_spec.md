# Motor Gate 3: controlled stop on the idle X axis, the first write to a drive (specification, for review)

- **Status:** implemented (ADR-0018) and **run once on 2026-11-13: PASS** (§10). Three writes were each acknowledged by `O`; X returned to idle; nothing changed.
- **Date:** 2026-11-13.
- **Builds on:**
  - Motor Gate 1 rev 2 (transport, ADR-0016);
  - Motor Gate 2 (power-up values, ADR-0017, spec §8);
  - motor L0 §3a ("Stop, halt and move", "Remaining stop/move items", and the write-acknowledgement correction).
- **Classification:** the first **L4 operation** on the motor drives. It writes to drive RAM variables only: no EEPROM, setup or firmware write.

## 1. Purpose
To prove, with **no intended physical motion**, that OpenEVOS can:
1. send a valid write frame to a drive, and handle its acknowledgement;
2. trigger the drive's own controlled-stop routine (`STATE_6_STOP`) on X;
3. see the state machine return to idle with position and parameters unchanged.

This is the minimum needed before any move tool. A stop must exist and be verified first.

## 2. What is known (offline and from Gates 1–2)

| Fact | Label |
|---|---|
| **The vendor's `XYStageStopXAxis`** (`FUN_18003A7C0`) sends, in this order: `IPENDINGMOVES` = 1, `CACCSTOP` = *vendor constant*, `ESTATE` = 6 | Strongly supported (decompiled) |
| **Writes are acknowledged by a single `O` (`0x4F`).** The vendor's exchange routine (`FUN_1800333B0`, no-reply mode) reads exactly 1 byte and treats anything else as a link failure | Strongly supported (decompiled) |
| **`STATE_6_STOP` on X** (`0x41F2`): loads `CACCSTOP` as the deceleration, sets the target to the actual position (`LTARGETPOSV` = `APOS`), reloads the motion settings, runs `UPD` (`0108`), and clears `IPENDINGMOVES`, the convergence and oscillation counters and `BCOASTTOSTOP`. It sets `ESTATEV` = 0 and returns to `MAINLOOP`. **No `AXISOFF`, no offset.** It does not issue `AXISON`; it assumes the drive is powered | Strongly supported (`.sw` read) |
| **Power-up values on X** (Gate 2): `APOS` = `CPOS` = 0; `LPOSMIN`…`LPOSMAX` = 0…50,000; `CACCSTOP` = 1.0 (raw, vendor order, `00 00 00 01`) | Verified |
| **Word order** of 32-bit values: low word first, each word big-endian | Verified on hardware |

**Deviation from the vendor (deliberate):** the vendor writes its own `CACCSTOP` constant (`DAT_180094548`, value not decoded). Gate 3 instead **writes back the value it has just read**, which must be exactly 1.0, so the write leaves the deceleration unchanged.

## 3. The only frames this tool can send

Everything is on axis 1 (X, destination `00 10`).

| Purpose | Frame | Notes |
|---|---|---|
| Line sync | `FF` | as Gate 1 rev 2: ≤ 15 tries, 10 ms apart, reply `0D` |
| Gateway axis-ID query | `08 00 00 B0 00 00 01 00 00 B9` | as Gates 1–2 |
| Readiness reads (all 6 axes) | Gate 2's `ESTATEV` and `IINITONCEDONE` frames | 10 constants |
| X baseline and post-check reads | Gate 2's X frames for `ESTATEV`, `APOS`, `CPOS`, `LPOSMIN`, `LPOSMAX`, `CACCSTOP` | constants |
| **W1** `IPENDINGMOVES` (`0x03CD`) = 1 | **`06 00 10 21 CD 00 01 05`** | byte-identical to the vendor's |
| **W2** `CACCSTOP` (`0x03E8`) = 1.0 | **`08 00 10 25 E8 00 00 00 01 26`** | fixed 1.0 = `0x00010000` in vendor order; checksum as `FUN_18003A080` |
| **W3** `ESTATE` (`0x03B3`) = 6 | **`06 00 10 21 B3 00 06 F0`** | checksum as `FUN_18003A5F0` |

- **Allowlist:** the three write frames above are **constants**. No function can build another write: no other variable, value, axis or opcode. In particular it cannot build `ESTATE` = 8 (move), 9 (halt) or 11 (unhalt), `AXISOFF` (`00 02`), `AXISON`, `UPD`, a limit write, or a baud or axis-ID write. Tests pin this.
- **Correct addresses (X):** `ESTATEV` `0x03EA`, `ESTATE` `0x03B3`, `IPENDINGMOVES` `0x03CD`, `CACCSTOP` `0x03E8`. These come from X's `variables.cfg`; the other addresses circulated in review notes were wrong.

## 4. Sequence (each frame once, each preceded by its own line sync; fail fast)

**Phase 0, refuse before opening:**
- exactly one FTDI port;
- the operator confirms the instrument has been on ≥ 2 minutes, **the stage is clear, and they are watching the X carriage**;
- the phrase `STOP IDLE X AXIS ON COMn`;
- dry run by default.

**Phase A, port and readiness (read-only):**
- port opened as Gate 1 rev 2, including the driver-state check;
- baud passes;
- gateway ID = 1;
- every axis `ESTATEV` = 0, and X/Y/Z/F `IINITONCEDONE` = 12345.

Otherwise stop with nothing written.

**Phase B, X baseline (read-only):** read `APOS`, `CPOS`, `LPOSMIN`, `LPOSMAX`, `CACCSTOP`. Stop with nothing written unless:
- `APOS` = `CPOS` exactly. `STATE_6_STOP` retargets to `APOS`, so equality means the stop cannot move the carriage, not even by the holding error;
- `LPOSMIN` < `LPOSMAX`, and `LPOSMIN` ≤ `APOS` ≤ `LPOSMAX`;
- `CACCSTOP` raw = `00 00 00 01` (1.0).

**Phase C, the writes:**
- W1, then W2, then W3.
- After each write, read **1 byte**; it must be `O` (`4F`). Anything else, or nothing within the read timeout, means **stop immediately**, with no further write and no retry. The tool then goes to Phase D's reads, to record the state.

**Phase D, completion and integrity (read-only):**
1. Poll X `ESTATEV` up to **10 times, 50 ms apart**, until it reads 0. Record every value: the transition 6 → 0 may be too fast to see.
2. Re-read X `APOS`, `CPOS` and `CACCSTOP`, and `ESTATEV` on all six axes.
3. **Pass** only if:
   - X `ESTATEV` returned to 0;
   - `APOS` and `CPOS` both equal the Phase B `APOS`;
   - `CACCSTOP` is still 1.0;
   - all six axes are idle.

## 5. Abort and recovery
- **Any failure before W1:** nothing is written; close; report.
- **Failure after W1 or W2, before W3:** the variables written are a PC marker (`IPENDINGMOVES`) and a deceleration equal to its old value. The drive state is unchanged. Stop, run the Phase D reads, report.
- **X does not return to idle within 10 polls, or a position changed:** stop and report; **send nothing else**. No halt, no unhalt, no second stop.
  - **Recovery is the operator's choice:** power-cycle the instrument, which re-runs homing.
  - Any further OpenEVOS action needs a new review.
- **Fail-fast** throughout; the line sync is the only retry.

## 6. Hazards

| # | Hazard | Control |
|---|---|---|
| G3-1 | The stop moves the carriage | `STATE_6_STOP` retargets to `APOS`; refuse unless `APOS` = `CPOS` and inside the limits. Expected motion: none. The operator watches; the stage is clear |
| G3-2 | A malformed or wrong write reaches the drive | The three write frames are constants, byte-checked against the vendor's code and pinned in tests; nothing can build another frame |
| G3-3 | The deceleration is changed | W2 writes only the value just read (1.0); refuse if it is not 1.0; verify afterwards |
| G3-4 | The `O` acknowledgement is mishandled, desynchronising later reads | Read and check exactly 1 byte after each write; RX purge before each sync; replies are checked by sender and variable echo |
| G3-5 | The state machine is left non-idle | Bounded poll, then stop and report; recovery by power cycle; no automatic follow-up command |
| G3-6 | The power stage is switched off | `STATE_6_STOP` contains no `AXISOFF`; the tool cannot build one |
| G3-7 | A different axis is affected | Destination `00 10` only (constants); all-axis idle check before and after |
| G3-8 | `IPENDINGMOVES` = 1 has a side effect | The stop state resets it to 0 at once; the vendor sends it on every stop. The post-check would show any lasting effect (not read directly; Probable: a PC-side marker) |

## 7. Evidence
A git-ignored JSON file with:
- the port identity and read-back state;
- every frame and reply in hex, with timings;
- the acknowledgement bytes;
- the Phase B and Phase D values;
- the poll sequence;
- the pass/fail result with reasons.

The tool would be registered in `REVIEWED_MAINTENANCE` (ADR-0018). The command core still refuses every motor command.

## 8. Not covered
Any move (`ESTATE` = 8 or 5); halt or unhalt; any other axis; limit, speed or acceleration writes; any EEPROM or setup write; the desktop app. The 1 mm X move (Gate 4) is a separate spec.

## 9. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement the §4 tool (fake device only) | **Approved and implemented** (`maintenance/motor_gate3_stop.py`, ADR-0018); fake chain only | operator | 2026-11-13 |
| Run it once on the instrument (stage clear, operator watching X) | **Approved; run once: PASS (§10)** | operator | 2026-11-13 |

## 10. First run (record `motor-gate3-20261112T192540-pass.json`): PASS

- **Readiness:** 115,200 baud; gateway ID 1; all six axes idle; X/Y/Z/F homed.
- **X baseline:** `APOS` = `CPOS` = 0; limits 0…50,000; `CACCSTOP` raw `00 00 00 01`.
- **Writes:** W1, W2 and W3, each sent once after its line sync. **Each was acknowledged by a single `4F` (`O`) within 15–16 ms.**
  - **Verified on hardware:** a write is answered by exactly one `O`, as the vendor's exchange routine expects.
- **Completion:** the first `ESTATEV` poll already read **0**. On a stationary axis, `STATE_6_STOP` completes faster than one read round trip (about 0.4 s).
- **Afterwards:** `APOS` = `CPOS` = 0 and `CACCSTOP` unchanged; all six axes idle.
- **Operator:** **no movement seen** (reported 2026-11-13), as expected.

### What this establishes
- **Verified:**
  - OpenEVOS can write drive RAM variables with correctly framed, acknowledged writes (16-bit `21xx` and 32-bit `25xx` forms);
  - requesting a state through `ESTATE` works;
  - the drive's controlled stop on an idle axis leaves position and parameters unchanged, and every drive idle.
- **Still unverified:** a stop **during motion**, which is what a stop is actually for. That can only be tested once a move exists (Gate 4), and Gate 4 must include it.
