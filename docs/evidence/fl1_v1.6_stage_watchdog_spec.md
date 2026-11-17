# X/Y runaway watchdog (specification, for review)

- **Status:** Phase 1 (shadow mode) **built 2026-11-16** and tested on the simulator; not yet run on the instrument
  (§11). Phase 2 is not built. The build of each phase needs the operator's approval (§10). No instrument run is part
  of this spec beyond ordinary app moves in Phase 1.
- **Decision record:** ADR-0047.
- **Source:** Y-axis plan v3 (`Y_axis_commutation_drift_findings_and_fix_plan.md`):
  - §6.3, the "Axis Moving" rule: following error beyond the normal maximum + 300 counts, `APOS` moving more than
    50 counts against the command, or |`IQ`| > 0.8 × `IMAXPROT` for > 100 ms → `AXISOFF` that axis; latch;
  - §6.3, the watchdog's limits, design requirements and escalation;
  - §9 roadmap, P0 "Runaway watchdog with escalation".
- **Builds on:**
  - ADR-0040 (drift monitor, move loop, latch, evidence);
  - ADR-0044 / Motor Gate 15 (host `AXISOFF` on an idle Y; spec §9, the rules for using `AXISOFF` in the app);
  - ADR-0045 (T1 runs: the only logged X current during motion).
- **Classification:**
  - Phase 1 is read-only: new reads during app moves, no new write.
  - Phase 2 adds one write to the app, Y's constant `AXISOFF` frame. That is an L4 operation (one TML instruction to
    drive RAM).

## 1. Purpose
Stop an axis that has lost control while OpenEVOS is moving it, sooner than the existing guards do, and switch Y's
power stage off when it does.

The plan treats the watchdog as **best-effort**. The latch and the physical fix are the real protections. The
watchdog limits how far a runaway travels before the latch matters.

## 2. What is known

| Fact | Label |
|---|---|
| **The move loop already samples at ~15–20 Hz.** Each tick reads X `APOS`, Y `APOS` (into the drift monitor) and the moving axis' `SRH` | Verified (ADR-0040, `telemetry.py` `_move_leg`) |
| **A wrong-way guard exists.** Moving away from the target by more than 100 counts beyond the start distance sends the controlled stop and latches `wrong_direction` | Verified in code and simulator tests. It has never fired on the unit |
| **No log holds the following error during motion.** The stage logs carry `CPOS` only from the 1 Hz slow poll. The Run C CSV's `x_cpos` changed 13 times, at leg ends | Verified (the logs, 2026-11-16) |
| **`IMAXPROT` = 17,117 on X and Y.** The program overwrites the setup's 14,429. 0.8 × 17,117 = 13,694 | Verified on the unit (Gate 9 records from `…T121350` on) |
| **X current while moving at 1 mm/s** (Run C, 16,673 samples): \|`IQ`\| median 1,218, p99 1,719, max 2,077. **At rest** X reads ~250, and Y 1,180 (power-up record, 2026-11-16) | Verified (Run C CSV; power-up history) |
| **No current is logged for Y moving, or for X at 4 or 8.27 mm/s** | Gap. Phase 1 fills it |
| **`POSERR` (0x022A) reads the drive's own position error, signed 16-bit.** At rest Gate 9 read 0 on every axis, and −33 on Y in one record after a trip | Verified at rest. Its sign and scale during motion, and what it does beyond ±32,767, are **unverified** |
| **`ERRMAX` = 1,000 counts, `TERRMAX` = 0xFFFF.** The drive's own control-error protection is effectively off | Verified on the unit (Gate 9; plan §2.1) |
| **Reading `CPOS` and `APOS` separately gives a skewed difference.** They are ~16 ms apart. At the fast preset (8.27 mm/s, ~18,750 counts/s) that is ~300 counts of apparent error, as large as the plan's margin | Arithmetic from verified read time and speed |
| **Host `AXISOFF` to Y works on an idle Y.** The power stage dropped 15 ms after the acknowledgement, and Y held at 0 counts while off | Verified on the unit (Gate 15, ADR-0044) |
| **Host `AXISOFF` on a moving axis, and on X, have never been sent** | Not validated (Gate 15 spec §8) |

## 3. Signals (both phases)
Measured on the axis OpenEVOS is moving, every leg, from the start of the move request to motion complete.

- **Following error, `fe`:** `POSERR`, read directly. It is the drive's own `CPOS − APOS` at one instant, so it has no
  skew.
  - Cross-check: at the end of every leg, `POSERR` must be within ±5 counts of `CPOS − APOS` read at rest. Otherwise
    `fe` is marked untrusted for that leg, and Phase 2 does not arm on it.
  - Phase 1 also records the sign of `POSERR` against the move direction, to pin its convention.
- **Against the command, `back`:** how far the axis has gone back from its closest approach to the target. This is
  the distance to the target minus the smallest distance seen so far in this leg.
  - A normal overshoot does not count: going past the target and returning moves toward it.
  - Overshooting by up to 20 counts and holding there would count up to 20. ADR-0040 saw 16–20 counts on X.
- **Current, `iq`:** `IQ`, signed. The rule uses |`iq`|, which counts as "over" only while above the limit.

**Read schedule per tick:** X `APOS`, Y `APOS`, the moving axis' `SRH`, plus one extra read that alternates between
`POSERR` and `IQ`.
- That is 4 reads, about 64 ms on COM3, so ~15 Hz.
- Each of `fe` and `iq` is refreshed about every 130 ms.

## 4. Phase 1: shadow mode (reads only)

1. **Reads.** The schedule in §3, during every app leg.
   - `POSERR` joins the app's read allowlist. It is the address Gate 9 read on the unit, and a test pins that.
   - Nothing is written that the app does not write today.
2. **Motion profile.** One row per leg is appended to `openevos-scans/stage-monitor/motion-profile.csv` (git-ignored,
   real port only):
   - `utc`, `axis`, `speed_preset`, `distance_counts`, `duration_s`, `ticks`, `tick_hz`;
   - `fe_max_abs`, `fe_sign_vs_direction`, `fe_trusted`;
   - `iq_max_abs`, `iq_over_limit_s` (the longest run above 13,694);
   - `back_max`, `outcome`.
3. **Would-trip notes.** The Phase 2 rule is evaluated with provisional thresholds. A rule that would have fired is
   written as a note on the stage monitor and in the row's `would_trip` column. **It never stops, latches or switches
   off anything.** The provisional thresholds are:
   - `fe`: \|`fe`\| > 1,000 counts, which is `ERRMAX`;
   - `back`: > 50 counts;
   - `iq`: > 13,694 on 2 consecutive `IQ` reads.
4. **The existing guards are unchanged:** the wrong-way guard (100 counts, stop and latch), the drift monitor, the
   flags read after a move, and the landing check.
5. **Cost:** about 25 % fewer drift-monitor samples while moving (~15 Hz, from ~20 Hz). This is still inside ADR-0040's
   measured range. Idle sampling is unchanged. Simulated with 15 ms reads: 14.2–16.0 Hz within the leg.

### Calibration
Done by ordinary use plus one operator-run calibration set. Each move in the set needs the operator's approval in chat
as usual, and keeps the ground rules: one axis at a time, a power cycle after anything that may change Y's count.

| Set | Moves |
|---|---|
| X, each preset (slow, medium, fast) | ≥ 20 legs, including ≥ 2 legs over ≥ 90 % of the Gate 8 range each way |
| Y, each preset | the same |

Phase 1 is complete when every cell has its legs, with `fe_trusted` true on all of them. The thresholds are then
computed per axis and per preset:
- **`fe` limit** = the maximum \|`fe`\| seen + 300 counts (plan §6.3);
- **`back` limit** = 50 counts, or the maximum `back` seen + 30 counts if that is larger. A higher value needs a
  separate decision: it would mean normal settling travels further back than the plan assumed;
- **`iq` limit** = 13,694, unless the calibration's maximum \|`iq`\| comes within 2× of it. The threshold is then
  reviewed rather than raised (plan: thresholds are never raised to make motion go through).

They go into an ADR revision (protected settings, ADR-0040 §8) with the profile file it was computed from.

## 5. Phase 2: armed watchdog

**Arming conditions:** Phase 1 calibration done; the thresholds written into the ADR; the build approved.

**Trigger** (any one, on the moving axis, while a leg is running):
- \|`fe`\| > the `fe` limit on one read (only while `fe` is trusted);
- `back` > the `back` limit;
- \|`iq`\| > 13,694 on 2 consecutive `IQ` reads (≥ ~130 ms, which meets the plan's "> 100 ms").

**Action, Y moving.** This is done inline in the tick on the telemetry thread, which owns the line (Gate 15 §9.2), with
no queued job ahead of it.
1. Send `protocol.axis_off("Y")`. This is the constant `04 00 20 00 02 26`, preceded by a line sync and checked for one
   `O`. The function refuses every other axis.
2. Latch `runaway_<rule>` on Y, and read the drive's record (`ISTATUSFLAGS`, `IIQMAX`/`IIQMIN`, `LCOMMERRPOS`) as
   `_on_latch` does today.
3. **Confirm:** Y `SRL` bit 15 = 0 within 100 ms of the acknowledgement, read with the fast path (~16 ms per read).
4. **Escalation if not confirmed** (plan §6.3; Gate 15 §9.3):
   1. send the same `AXISOFF` once more;
   2. send the controlled stop to Y;
   3. latch all X/Y motion;
   4. raise a **blocking** stage alarm: "Y did not switch off: use the main switch";
   5. write the event as `watchdog_failure`.
5. **State.** Y is Off (plan §6.2), and `power_stage_off` is expected, not a second fault. Y's drift reference becomes
   its `APOS` at the `AXISOFF`. Y moves are refused until a power cycle is seen. The app never sends `AXISON`.

**Action, X moving.** The controlled stop, the latch and the snapshot only, as the wrong-way guard does today. There is
no `AXISOFF` for X until X has its own one-try run (Gate 15 §9.1).

**Evidence:** the latch's JSON and CSV as today (ADR-0040 §6), plus:
- the watchdog signals for the leg (`fe`, `iq`, `back` per read);
- the time from the trigger read to the `AXISOFF` acknowledgement, and to `SRL` bit 15 = 0;
- the thresholds and the profile file they came from.

**Freshness:** a tick whose reads fail ends the leg as a link error (as today), and that also latches X/Y motion. A
runaway with a dead line is beyond this watchdog (plan §6.3 limits).

## 6. Tests (simulator, both phases)
- **Phase 1:**
  - a normal move gives one profile row with the right fields, and no write beyond today's set;
  - an injected runaway gives a would-trip note and no stop, latch or `AXISOFF` from the watchdog. The existing
    wrong-way guard still fires at 100 counts;
  - a simulated stage never writes the profile file;
  - the leg's sample rate stays ≥ 13.5 Hz with 15 ms reads (measured 14.2–16.0 Hz).
- **Phase 2:**
  - each trigger, on Y, gives exactly one `AXISOFF` frame (`04 00 20 00 02 26`), latched, Y refused until a power
    cycle;
  - the same triggers on X give the stop and the latch, and never an `AXISOFF` frame;
  - Y not dropping → one repeat `AXISOFF`, a stop, a blocking alarm and `watchdog_failure`, and never a third
    `AXISOFF`;
  - a 1-read `iq` spike does not trip;
  - an untrusted `fe` does not trip on `fe`;
  - `axis_off` refuses X, Z, F and broadcast;
  - no `AXISON` can be built.

## 7. Hazards

| Hazard | Mitigation |
|---|---|
| **A false trip in Phase 2 switches Y off mid-move.** The carriage coasts unpowered; Y then needs a power cycle | Thresholds come from calibration with a margin. Phase 1 shows the would-trip rate on normal moves first. A false trip is a nuisance, never an unsafe state |
| **`AXISOFF` on a moving Y lets it coast** a short distance at up to 8.27 mm/s | The plan accepts this (§6.3). Off is safer than driven with a reversed phase, the 2026-11-13 failure (77.7 mm wrong way) |
| **`POSERR` behaves differently than assumed during motion** (sign, saturation) | Phase 1 records and cross-checks it; `fe` is used only where trusted |
| **The extra read lowers the drift monitor's rate during moves** | §4.5: ~15 Hz, tested ≥ 13.5 Hz |
| **`AXISOFF` on a moving axis has never been seen to work here** | The first real trip is its first use, and its evidence records the timings. A deliberate validation (Y switched off mid-move at slow speed) would need its own one-try gate. It is listed as an option in §9, not required |

## 8. Recovery
Unchanged from ADR-0040 §5: a full power cycle with the app connected clears the latch. Gate R is not passed, so no
single-axis recovery is offered.

## 9. Not covered
- **Host `AXISOFF` on X.** It needs its own one-try run, by the Gate 15 procedure.
- **A validation of `AXISOFF` on a moving Y.** This is optional: a separate one-try gate if the operator wants it before
  arming.
- **Lowering `IMAXPROT`** (plan §6.7, deferred). The Phase 1 currents are part of the characterisation it needs, not
  all of it (homing and cold start are missing).
- **Motion not started by the app:** maintenance gates, homing, the joystick of another program. The gates have their
  own guards.
- **The stage feedback screen** (plan §6.6). The profile file is one of its future inputs.

## 10. Decision record

| Item | Decision | By | Date |
|---|---|---|---|
| Build Phase 1 (shadow mode, reads only) | **Approved and built** (§11) | operator | 2026-11-16 |
| Run the calibration set (§4) | pending; each move approved in chat | operator | |
| Fix the thresholds (ADR-0047 revision) | pending | operator | |
| Build and arm Phase 2 | pending | operator | |

## 11. Phase 1 as built (2026-11-16)
- **Code:**
  - `src/openevos/backends/technosoft/watchdog.py`: `LegWatch` (the signals of §3 and the provisional rules), the
    profile row, `append()` and `profile()`;
  - `telemetry.py` `_move_leg`: one `LegWatch` per moving axis; per tick, the encoder plus `POSERR` or `IQ`
    alternately; the at-rest cross-check reads (`POSERR`, `CPOS`, `APOS`) after the landing check and before the stop
    at rest; `_watch_end` writes the row and the would-trip notes;
  - `protocol.py`: `POSERR` (0x022A, 16-bit) joins the X/Y reads.
- **Tests:** `tests/test_stage_watchdog.py`:
  - `POSERR`'s frame is Gate 9's;
  - an overshoot and its return are not "against the command";
  - each provisional rule only notes; a 1-read current spike does not count;
  - the cross-check and the sign;
  - a normal move writes one row and no new frame;
  - an injected Y runaway is noted, the 100-count guard stops and latches as before, and no `AXISOFF` is sent;
  - a simulated stage keeps its rows in memory only;
  - the in-leg rate with 15 ms reads.
- **Where the rows go:** `openevos-scans/stage-monitor/motion-profile.csv`, real port only (the monitor's folder), and
  the last 50 in memory (`TelemetryService.watch_rows`). Would-trips appear in the monitor's command log as
  "watchdog (shadow, not acted on): …", which is in every latch's evidence JSON.
- **Rows are written on every leg outcome,** including stops and failures. `fe_trusted` is false whenever the leg did
  not reach the at-rest cross-check.
- **Next:** ordinary app moves on the instrument fill the profile; the calibration set (§4) then needs the operator's
  approval in chat, move by move.
