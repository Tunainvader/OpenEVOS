# Motor Gate 15: host `AXISOFF` on the idle Y axis (specification, for review)

- **Status:** implemented (ADR-0044) and **run once on the instrument, 2026-11-16: PASS, then recovered** (§12).
  How to run it: §11.
- **Source:** Y-axis plan v3 (`Y_axis_commutation_drift_findings_and_fix_plan.md`): §4 run order step 3 ("validate host
  `AXISOFF`: one try on Y, at idle, with nothing else moving; confirm `SRL` bit 15 = 0"), §6.2–§6.3 (the watchdog and
  its escalation), and the status table ("Validate before T1 Run A").
- **Builds on:**
  - Motor Gate 3 (first write, `O` acknowledgement, ADR-0018);
  - Motor Gate 9 (X/Y status reads and the Y program decode);
  - ADR-0040 (drift monitor; its "not in this ADR" list names this spec).
- **Classification:** an **L4 operation**. It sends one TML instruction to drive RAM: no variable write, no EEPROM,
  setup or firmware write.

## 1. Purpose
Before anything relies on it, show once on this unit that OpenEVOS can switch off Y's power stage on purpose, and
how Y behaves afterwards:
1. one host `AXISOFF` frame to axis 2 is acknowledged;
2. Y's power stage drops (`SRL` bit 15 = 0) within 100 ms, the plan's escalation limit;
3. nothing else changes: Y's flags, state and command position, and X;
4. what Y's encoder does while unpowered.

Three later steps need this:
- **T1 Runs C and A** switch Y off before X moves (plan §4 safety rules).
- **The Run B guard** switches Y off at 6 counts of drift.
- **The runaway watchdog** (plan §6.3) switches a runaway axis off.

None of them may be built on an `AXISOFF` never seen to work here.

## 2. What is known

| Fact | Label |
|---|---|
| **`00 02` is `AXISOFF`.** In the X and Y programs, `_OSCDETECTED` branches over the single word `0002` to a label the vendor named `_OSCNOAXISOFF`, and `STATE_11_UNHALT` begins with `0102` (`AXISON`) | Verified from the drive program (motor L0 §3a) |
| **The vendor sends this instruction to every drive.** `HaltAllMotors` (`FUN_18002DAA0`) sends `04 00 n0 00 02 cs` to Z, F, X, Y, P, O in turn, then requests HALT. For Y this is `04 00 20 00 02 26`. When PearlScope calls `HaltAllMotors` is not decoded | Strongly supported (decompiled) |
| **Checksum** = byte sum mod 256 (`protocol.checksum`). `04 00 10 00 02 16` (X) and `04 00 60 01 02 67` (the vendor's O-axis `AXISON`) both follow it | Verified (pinned in tests) |
| **Writes to Y, routed through X, are acknowledged by one `O`** (`0x4F`). Every OpenEVOS Y move does this (Gate 8; ADR-0024 app moves, last on 2026-11-16) | Verified on hardware for variable writes. For an instruction frame: Strongly supported (the vendor uses the same no-reply exchange) |
| **An axis whose power stage is off reads `SRL` = 0x4400** (bit 15 clear, bit 10 set). Two observations: after each of Y's trips, where Y's own over-current handler issued `AXISOFF`; and F, which switches itself off at idle. After Y's trips, `ISTATUSFLAGS` (0xD000 or 0x1001) and `MER` (0 or 0x0002) varied between records | Verified on the unit (Gate 9 records, 2026-11-13: `…T121350`, `…T131624`, `…T144813`, `…T171104`; F in `…T175045`). The drive's own `AXISOFF`, never a host one |
| **A healthy idle Y reads `SRL` 0xC400 and `ISTATUSFLAGS` 0xD000** (power stage on, motion complete, convergence check passed and its window over) | Verified on the unit (Gate 9; drift monitor on COM3, 2026-11-16) |
| **Nothing in Y's program switches the axis back on at idle.** `AXISON` appears only at start-up, in the move states (5, 8) and in `STATE_11_UNHALT`. The settle check runs only while `BDOAXISOFF` = 1, and `CHECKOVERCURRENT` compares `IQ`, which should read about 0 with the stage off | Strongly supported (`.sw` decode, Gate 9 §"What the Y program does") |
| **The next Y move would switch Y back on.** `STATE_8_MOVE_POS` executes `AXISON` and `UPD`. An `AXISON` after `AXISOFF` resumes from where the reference generator stopped | Strongly supported (decode; Technosoft documentation) |
| **F switches itself off and on routinely.** After its settle countdown F's program issues `AXISOFF`, and its next move issues `AXISON` (Gates 11–13 moved F that way) | Verified on the unit for F's own program. Not host-issued |
| **Y's encoder count keeps updating with the power stage off** (the encoder runs from the logic supply) | Probable (plan §4). Not measurable here unless Y moves while off |

**Open, and measured by this gate:**
- whether the instruction frame gets the same `O`;
- how long `SRL` bit 15 takes to drop;
- whether a host `AXISOFF` changes `ISTATUSFLAGS`, `MER`, `CPOS` or `ESTATEV`;
- whether the Y carriage moves when its stepper loses holding current, for example from cable tension.

## 3. The only frames this tool can send

| Purpose | Frame | Notes |
|---|---|---|
| Line sync | `FF` | as Gate 1 rev 2 |
| Gateway axis-ID query | `08 00 00 B0 00 00 01 00 00 B9` | as Gates 1–3 |
| Reads | the reviewed `protocol.VARS` frames for X and Y: `ESTATEV`, `IINITONCEDONE`, `SRH`, `SRL`, `MER`, `ISTATUSFLAGS`, `BDOAXISOFF`, `APOS`, `CPOS`, `IQ`, `IIQMAX`, `IIQMIN`, `LCOMMERRPOS`; and `ESTATEV` on Z, F, O, P | constants (Gate 9 addresses, pinned by test) |
| **W1, `AXISOFF` to Y (axis 2)** | **`04 00 20 00 02 26`** | the vendor's `HaltAllMotors` frame for Y. Sent **at most once** per run |

- **Allowlist:** W1 is a constant. The tool cannot build any other write. In particular it cannot build:
  - `AXISON` (`01 02`) on any axis, or `AXISOFF` to any axis but Y;
  - an `ESTATE` write (move, stop, halt, unhalt, homing), a variable write, `UPD`, or `RESET`;
  - a broadcast or group address.

  Tests pin this, as for Gate 3.
- **No host `AXISON`, ever.** Y comes back on only through a power cycle (§6).

## 4. Sequence (fail fast; the line sync is the only retry)

**Phase 0, refuse before opening:**
- exactly one FTDI port, and **no OpenEVOS app holding it** (the app must be closed: this tool owns the line);
- the operator confirms (plan §4 safety rules):
  - the microscope was power-cycled for this gate and has been on ≥ 2 minutes, with no move since;
  - stage clear, Z retracted, nothing in Y's path;
  - main switch within reach, a second person present;
  - they are watching the Y carriage;
- the phrase `SWITCH OFF IDLE Y AXIS ON COMn`;
- dry run by default: it lists the frames and opens nothing.

**Phase A, port and readiness (read-only):** as Gate 3:
- the port opens and the baud passes;
- gateway ID = 1;
- every axis' `ESTATEV` = 0;
- X/Y/Z/F `IINITONCEDONE` = 12345.

Otherwise stop with nothing written.

**Phase B, baseline (read-only).** Stop with nothing written unless all of these hold:
- **Y:**
  - `SRL` bit 15 = 1 (on) and bit 10 = 1 (motion complete);
  - `ISTATUSFLAGS` bits 0, 4 and 13 clear, and bit 15 set: the settle window is over;
  - `BDOAXISOFF` = 0, so the drive's own settle check cannot race the `AXISOFF`;
  - |`APOS` − `CPOS`| ≤ 3.
- **Y still for 1 s:** `APOS` read back to back for 1 s, every sample within ±1 of the first.
- **X:** `SRL` bit 15 = 1 and `ESTATEV` = 0.

Record all of this, plus Y `MER`, `SRH`, `IQ` (its holding current), `IIQMAX`, `IIQMIN` and `LCOMMERRPOS`. This is
the first read of `LCOMMERRPOS` on the unit.

**Phase C, the write:**
- One line sync, then W1, then read **exactly 1 byte**. It must be `O` (`4F`).
- Anything else, or nothing within the read timeout, means **no further write and no retry**. Go straight to
  Phase D: the reads show whether it took effect anyway.
- t0 = the moment the acknowledgement arrived.

**Phase D, confirm (read-only):**
1. **Off within 100 ms.** Read Y `SRL` back to back (~16 ms each) until bit 15 = 0, recording every value and its
   time after t0.
   - **Pass** if bit 15 is 0 within 100 ms.
   - If not, keep reading for up to 1 s more, to measure a slow drop, then **FAIL**.
   - **No second `AXISOFF`.** The watchdog's escalation repeat (plan §6.3) is for a runaway, not this validation.
2. **Hold 10 s.** Read Y `APOS` and Y `SRL` back to back throughout (~30 Hz). Once a second, also read:
   - Y `IQ`, `ISTATUSFLAGS`, `ESTATEV`, `CPOS` and `MER`;
   - X `APOS`, `SRL` and `ESTATEV`.
3. **Final reads:** every axis' `ESTATEV`, and the full Phase B set on X and Y.

**Result:**

| Check | Pass condition |
|---|---|
| Acknowledgement | exactly one `O` |
| Power stage | Y `SRL` bit 15 = 0 within 100 ms of t0, and still 0 at every read of the hold. Expected value 0x4400, as for the drive's own `AXISOFF`; a different value is recorded, not failed |
| Drive state | Y `ESTATEV`, `CPOS` and `ISTATUSFLAGS` as in Phase B. Y `MER` recorded, not judged (bit names are only Probable) |
| Other axes | X `APOS` within ±1 of Phase B, X `SRL` bit 15 = 1; every axis' `ESTATEV` = 0 |
| Nothing else sent | the frame log holds W1 once, plus syncs and reads |

**Reported separately (a finding, not a pass condition):** Y `APOS` during the hold.
- **Within ±2 counts:** Y holds position unpowered. X-only runs with Y off (T1 Runs C and A) are valid as planned.
- **More than 2 counts:** the carriage moves when its stepper is unpowered. Y-off runs would then mix real movement
  with any count drift. Review this before Run C.

Y `IQ` falling to about 0 is also recorded: independent evidence that the current stopped.

**Phase E, after the power cycle (separate invocation, read-only, no phrase):**
- The operator power-cycles the microscope and waits for power-on homing.
- Then `--check-recovered` reads X and Y: `SRL` bit 15 = 1, `IINITONCEDONE` = 12345, `ISTATUSFLAGS` bits 0, 4 and 13
  clear.
- This closes the gate. The run is complete only with Phase E recorded.

## 5. Abort and recovery
- **Any failure before W1:** nothing is written; close; report.
- **No acknowledgement, or a wrong one:** no further write. Run the Phase D reads and report.
- **Y still on after 100 ms + 1 s:**
  - Report FAIL: a host `AXISOFF` is not a reliable way to switch Y off, so the watchdog and the Y-off runs cannot use
    it.
  - Y is idle and holding, so this is not a hazard in itself. Recovery is a power cycle, at the operator's choice.
- **X or another axis changed state:** stop reading and report. **Send nothing.** Then power-cycle.
- **Y moves visibly during the hold:** the operator switches off at the main switch if it continues. The tool keeps
  logging and sends nothing.
- **The line fails mid-gate:** the untimed reads stop at the first bad reply, as in every gate. The timed reads (the
  off check, the hold and the stillness check) resync and retry once, as the app does, then stop. Nothing is sent
  either way.
- **Always, after a W1 was sent:** power-cycle before the next Y move, and before opening the OpenEVOS app.
  - Y stays off until something switches it on, and the next move request would do that through `STATE_8`.
  - If the app is opened first, its drift monitor sees Y's power stage off on a homed axis and latches
    (`power_stage_off`, ADR-0040): fail-safe. The lock then clears through the power cycle with the app open.

## 6. Hazards

| # | Hazard | Control |
|---|---|---|
| G15-1 | The carriage moves when its power stage drops (cable tension, no holding torque) | Y idle and still for 1 s first; stage clear; operator watching with the main switch at hand; `APOS` logged at ~30 Hz through the hold |
| G15-2 | The frame reaches the wrong axis. X is the gateway: switching X off would drop the stage's X holding torque | W1 is a constant with destination `00 20`, pinned by test. X `SRL`/`APOS` checked before, during and after |
| G15-3 | The instruction is not acknowledged like a variable write, desynchronising the line | Read exactly 1 byte; RX purge before each sync; replies checked by sender and variable echo; fail-fast |
| G15-4 | `AXISOFF` races the drive's own settle check or a move | Refuse unless `BDOAXISOFF` = 0, `ISTATUSFLAGS` bit 15 set, `SRL` bit 10 = 1 and `ESTATEV` = 0, with no move since the power cycle |
| G15-5 | Y is left off, and a later move switches it back on through `STATE_8` without a re-phase | The tool cannot build a move or `AXISON`. Power-cycle required before any Y move or app start; the app's monitor latches if forgotten |
| G15-6 | Y's program reacts to the off state | Decode: no idle `AXISON`; the settle check is not armed; `IQ` ≈ 0 stays below `IMAXPROT`. `ISTATUSFLAGS`, `ESTATEV` and `MER` are read through the hold |
| G15-7 | The CAN side records a communication error | X and Y `ISTATUSFLAGS` bit 4 and `MER` compared before and after |
| G15-8 | Y's commutation is disturbed | `AXISOFF` does not change the encoder count, and the power cycle re-phases Y anyway (plan §6.5). This gate never switches Y back on |

## 7. Evidence
A git-ignored `openevos-scans/motor-gate15-<time>-<result>.json` with:
- port identity, readiness and the Phase B baseline;
- W1 in hex, the acknowledgement byte and its timing;
- every `SRL` read in Phase D with its time after t0;
- the hold trace (Y `APOS`/`SRL`; the 1 Hz reads);
- the final reads;
- the pass/fail result and the separate Y-holds finding.

Phase E writes its own file, `motor-gate15-<time>-recovered.json` (or `-not_recovered`).

The tool is registered in `REVIEWED_MAINTENANCE`. The command core still refuses every motor command but the
reviewed X/Y moves.

## 8. Not covered
- **`AXISOFF` on a moving axis**, which is what the runaway watchdog would do. The carriage coasts unpowered, and the
  plan accepts the watchdog as best-effort (§6.3). It is not validated here.
- **`AXISOFF` on X, or on any other axis.** X would need its own one-try run by this procedure.
- **Host `AXISON`, never.**
- **Whether Y's count tracks real motion while off.** That needs Y to move while unpowered.
- **The T1 runs** themselves, the Run B guard, and the watchdog's thresholds.
- **Any app change.** §9 sets the rules for those.

## 9. After it passes: rules for using `AXISOFF` in OpenEVOS
Each use below needs its own ADR, with simulator tests, before it is enabled.

1. **Frame:** only the constant W1 for Y, through a `protocol.axis_off("Y")` that refuses every other axis. X is added
   only after X's own one-try run.
2. **Sender:** only the telemetry service thread, the single owner of the line. An `AXISOFF` request is checked
   between every read and served before any queued job, like a stop.
3. **Confirmation:** `SRL` bit 15 = 0 within 100 ms. Otherwise, per plan §6.3:
   - repeat the `AXISOFF` once;
   - latch all X/Y motion;
   - raise a blocking alarm telling the operator to use the main switch;
   - write the event as a watchdog failure.
4. **Monitor state:** a host-switched-off Y is in state Off (plan §6.2).
   - `power_stage_off` is then expected, not a fault.
   - Y's drift rule keeps running against its `APOS` at the moment of the `AXISOFF`.
   - Y moves are refused until a power cycle is observed (`IINITONCEDONE` leaves 12345 and returns). The next move's
     `AXISON` would resume a phase that may have drifted, and plan §6.5 rules out `AXISOFF` → `AXISON` as recovery.
5. **Triggers, in the plan's order:**
   - **a.** Y off before an X-only maintenance run (T1 Runs C and A; a Gate 8 revision): abort if Y `SRL` bit 15 is
     not 0 before X starts.
   - **b.** The Run B guard at 6 counts.
   - **c.** The runaway watchdog (plan §6.3), only after its thresholds are calibrated against logged normal motion.

## 10. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Implement the §4 tool (fake chain only) | **Approved and implemented** (`maintenance/motor_gate15_y_axisoff.py`, ADR-0044) | operator | 2026-11-16 |
| Run it once on the instrument (§4 Phase 0 conditions) | **Approved; run once: PASS, recovered (§12)** | operator | 2026-11-16 |

## 11. Running it
The OpenEVOS app must be closed: it holds COM3. Steps:
1. Power-cycle the microscope and wait ≥ 2 minutes. Move nothing.
2. Dry run (opens nothing):

   ```
   python -m openevos.maintenance.motor_gate15_y_axisoff
   ```
3. The run, with the Phase 0 conditions met:

   ```
   python -m openevos.maintenance.motor_gate15_y_axisoff --send --instrument-on-2min --power-cycled-no-move --stage-clear-watching-y --main-switch-second-person --confirm "SWITCH OFF IDLE Y AXIS ON COM3"
   ```
4. Power-cycle the microscope again, wait for homing (≥ 2 minutes), then:

   ```
   python -m openevos.maintenance.motor_gate15_y_axisoff --check-recovered --instrument-on-2min
   ```

Outcomes: `pass`, `fail`, `write_failed`; before W1, `refused`, `stopped`, `not_idle` or `baseline_refused` (nothing
written). Phase E gives `recovered` or `not_recovered`. Evidence goes to `openevos-scans/` (git-ignored).

## 12. First run (records `motor-gate15-20261115T194640-pass.json`, then `…T194729-not_recovered.json` and `…T194911-recovered.json`): PASS, recovered

- **Conditions:** after Gate 14 (F) had finished and freed COM3. The operator confirmed a fresh power cycle with no
  move since, the stage clear, Y watched, the main switch at hand and a second person present.
- **Baseline:** X and Y `SRL` 0xC400, `ISTATUSFLAGS` 0xD000, `BDOAXISOFF` 0, `APOS` = `CPOS` = 0, `MER` 0. Y's holding
  `IQ` was 543. Y read 0 at all 62 samples of the 1 s stillness check.
- **Write and power stage:** W1 was sent once and **acknowledged by one `O` in 16 ms**. The first Y `SRL` read
  afterwards, 15 ms later, was **0x4400**: the same value as after Y's own trips.
- **Hold (10 s, 273 samples):** Y stayed off at every read, and **Y `APOS` stayed at 0**: Y holds position
  unpowered. Y `IQ` read −90…0, and its `ESTATEV`, `CPOS`, `ISTATUSFLAGS` and `MER` were unchanged. X was unchanged
  throughout, and every axis stayed idle.
- **`LCOMMERRPOS`:** read on the unit for the first time. It reads −1,000,000 on both X and Y, which looks like an
  initial "no communication error recorded" value, not a position (Probable).
- **Recovery:**
  - The first `--check-recovered`, 49 s after the gate, still read Y off (`not_recovered`). The microscope had not
    actually gone through a power cycle.
  - After a full power cycle, X and Y read `SRL` 0xC400, homed, flags 0xD000: **recovered**.

### What this establishes
- **Verified on the unit:**
  - a host `AXISOFF` instruction frame is acknowledged like a variable write;
  - it switches Y's power stage off within one read (≤ 15 ms after the acknowledgement), with no flag, state or
    command-position change, and nothing on X;
  - Y does not move when unpowered at rest;
  - a power cycle brings Y back.
- **So:** the T1 X-only runs with Y switched off (Runs C and A) can use it, under the §9 rules and their own review.
- **Still unverified:**
  - `AXISOFF` on a moving axis (the watchdog case);
  - `AXISOFF` on X;
  - whether Y's count tracks real motion while it is off.
