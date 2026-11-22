# ADR-0061: Let a landing settle, re-approach once, and lock only on a repeated miss (amends ADR-0040, ADR-0057)

- **Status:** Accepted (2026-11-22). Simulator-tested; not yet seen on the unit.
- **Asked for by:** the operator, 2026-11-22, after a Y landing 6 counts (2.6 µm) off its target locked the stage
  overnight: "is the software lock necessary … lets do a deep fix with automatic handling".
- **Code:** `backends/technosoft/telemetry.py` (`_leg_landed`, `_await_landing`, `_reapproach`, `_approach`,
  `LANDING_SETTLE_S`, `LANDING_RETRY_MAX_COUNTS`); `backends/technosoft/stage_monitor.py` (`RELEASABLE_RULES`,
  `latch_drift`, `landing_reapproached`, `append_landing`, `LANDING_LOG_FILE`); `backends/technosoft/transport.py`
  (the simulator's `land_off` / `land_settle_reads`); `ui/shell.py` (the release prompt names a landing miss).
- **Tests:** `tests/test_landing_settle.py`; the landing cases in `tests/test_drive_steps.py`,
  `tests/test_stage_lock_release.py`.

## Context
The landing check (ADR-0040, plan §6.3) read each moved axis' encoder once, right after the drive reported motion
complete, and locked the stage when it was more than the drives' 5-count band (2.2 µm) from its target. Four of the
18 locks so far were landing locks, all on Y:

| When (UTC) | Move | Read at the check | What the data shows |
|---|---|---|---|
| 2026-11-18 18:35 | 21.2 → 0 mm | +12 | at home, Y's known tight spot; IQ peak 6,299 |
| 2026-11-19 05:50 | 0 → 74.3 mm | −6 | **+1 about 0.3 s later**, in the lock's own drive reads |
| 2026-11-19 07:56 | 32.0 → 0 mm | +53 | at home; IQ peak 13,378 |
| 2026-11-21 18:18 | 66.4 → 65.6 mm | +6 | still +6 0.3 s later; IQ −2,152 pushing towards the target |

The motion profile (398 legs) shows how close the band is: X ends 5 counts off in 5 of its 14 completed legs, Y up to
5 in 22. ADR-0040's own evidence has X overshooting 16–20 counts in a short move and settling within ±1 count about
0.5 s later, and on 2026-11-16 X went −19 → −1 → 0 after motion complete. So the check could look too early, and one
miss of one count locked the whole stage until a power cycle or a re-home.

## Decision
1. **Settle window.** A moved axis outside the band at motion complete is watched for up to `LANDING_SETTLE_S` =
   **1.0 s**, at the fast-sample rate, with nothing written. It has landed once two samples in a row are inside the
   band. The wrong-way guard, the drift monitor (the still axis) and STOP stay active during the window.
2. **One automatic re-approach.** Still outside after the window, by at most `LANDING_RETRY_MAX_COUNTS` = **10**
   counts (4.4 µm, the bound of ADR-0057 and ADR-0059, under the ~14-count commutation reversal of ADR-0030):
   - only for a single-axis leg (every leg, under `SEQUENTIAL_XY`), not under a re-home's lock, not for a re-approach
     leg itself;
   - only with the drive's error register `MER` clear and its flags clean (a flag rule locks as before);
   - the axis gets the controlled stop at rest, then a verified leg `CORRECT_APPROACH_COUNTS` = 100 counts (44 µm)
     **back the way it came**, then a verified leg onto the target. It never goes past the target towards a hard stop.
   - each of the two legs carries every leg check, including this settle window, but no second re-approach.
3. **Lock** (unchanged rule, `landing_error`) only when the miss is over 10 counts after the window, the re-approach
   misses again, `MER` is set, or any other rule trips. The band itself stays 5 counts.
4. **A small landing lock is releasable** (amends ADR-0057). `landing_error` joins `RELEASABLE_RULES`. The recorded
   miss (`apos − target`) must be at most 10 counts, and the release's fresh reads (homed, idle, fault-free, powered,
   within 3 counts of the command) still decide. A landing lock no longer needs a re-home or a power cycle.
5. **Recorded.** Every settled miss, re-approach and landing lock appends a row to `landing-log.csv` in the stage
   evidence folder: time, axis, speed, start, target, position, miss at motion complete, miss after the window, the
   drive's current `IQ`, and what was done. The rows map where each axis drags.
6. **Escalation.** The third re-approach of one axis in a session sets a standing stage warning naming where it
   happened, so a dragging guide is looked at. It does not lock.

## Not changed
- The 5-count band, the drift thresholds (2 counts at rest, 3 before a move), the cross-axis tolerance, the
  wrong-way guard and every drive-flag rule of ADR-0040.
- Locks from over-current, oscillation, comm abort, power stage off, wrong direction and large drift still need the
  re-home (ADR-0058) or a power cycle.
- No drive parameter is written: no gain, band or current limit. The fix is in when OpenEVOS looks and what it does
  next, not in the drives.

## Consequences
- A landing that settles, or that lands on a second approach, finishes the move. Of the four locks above, the
  05:50 one would not have happened. The 18:18 one would have had a second approach before locking.
- A move with a miss takes up to about 1 s longer. A re-approach adds two short legs, about 1–2 s.
- A repeated miss at the same place still locks: that is a mechanical problem (friction or binding in the guide), and
  the log rows show where. The lock now costs one click, not a re-home.
- The drive's current sign at a miss is recorded but not used. Once the log rows confirm the sign convention on both
  axes, "pushing away from its target" could become a direct commutation-fault rule.
