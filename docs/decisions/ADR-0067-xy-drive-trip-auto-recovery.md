# ADR-0067: Bounded automatic recovery after an X/Y drive trip (amends ADR-0040, ADR-0058, ADR-0065)

- **Status:** Proposed (2026-11-25). Implemented and tested on the drive emulator only. **Not run on the instrument.**
  Amended the same day by the operator's choice: a switched-off axis is homed from where it stands (see below).
- **Asked for by:** the operator, 2026-11-25, after a Multi-D run (8 wells, 2 channels, 141 planes) ended "partial"
  after three wells and 846 images: "who locks the stage, hardware firmware or software why cant we make our scope
  smart, and on such fault detection it will automatically start repairing itself without user intervention".
- **Amends:** ADR-0040 (a drive-fault lock is cleared only by a power cycle or the operator), ADR-0058 (the approach
  leg's landing), ADR-0065 (what the Multi-D runner does when a position move fails).
- **Code:** `backends/technosoft/stage_monitor.py` (`DRIVE_FAULT_RULES`, `classify_trip`, the leg context of a flag
  trip); `backends/technosoft/telemetry.py` (`auto_recovery_blocker`, `claim_auto_recovery`, `_RecoveryJob`,
  `_run_recovery`, `_recovery_test`, the staging landing of an approach leg); `backends/technosoft/transport.py` (the
  emulator's `trip_on_move`); `acquisition/stage_recovery.py` (`StageAutoRecovery`, the app side);
  `acquisition/mda_runner.py`, `acquisition/scan_runner.py` (retry once, or skip); `ui/shell.py` (manual moves);
  `instrument/settings.py` + Settings › Interface (`stage_auto_recovery`).
- **Tests:** `tests/test_stage_auto_recovery.py` (trip classes from the recorded trips; the ladder, its refusals and
  hard locks on the drive emulator; the app side; the approach leg; the shell; Multi-D and Scan retry or skip).
  `tests/test_stage_lock_prompt.py` now runs with the setting off: it tests the operator's own flows.

## Who locks the stage
Two layers, both still in place:
1. **The drive's own program** (Technosoft TML in each drive). On over-current (`ISTATUSFLAGS` bit 0) or oscillation
   after a move (bit 13) it stops the axis and **switches its power stage off** (`SRL` bit 15 clears). `MER` stays 0.
   This is the drive protecting its motor; OpenEVOS cannot and does not change it (no firmware, no drive parameters).
2. **OpenEVOS's stage monitor** (ADR-0040) sees that record on the next read and latches: every X/Y move is refused.
   Until now a drive-fault latch cleared only on a power cycle or the operator's Re-home (ADR-0058).

## Evidence: the eight drive-fault trips on record
From `openevos-scans/stage-monitor/` (`stage-monitor-*` JSON and CSV, `stage-latch-cleared-*`, `stage-rehome-*`,
`motion-profile.csv`, `landing-log.csv`, `power-up-history.csv`). "Class" is judged from the fast encoder samples of
the leg, the drive's reads at the latch and `ISTATUSFLAGS` bit 3 (homing interrupted).

| # | Latch (UTC) | Rule, axis | Doing what | Encoder at the latch | Class | Cleared by | After the clear |
|---|---|---|---|---|---|---|---|
| 1 | 11-18 17:42 | over-current Y | jog Y 44,890 → 0, after 4 stopped X jogs | never left 44,890 (0.19 s); APOS = CPOS; I peak −17,715 | stuck at start | power cycle | 17 legs clean (7 done, 10 jog stops); a Y landing lock at home 30 min later |
| 2 | 11-18 18:58 | over-current Y | its own power-up homing (`ISTATUSFLAGS` 0x1009) | stalled at −9,814; I peak 18,019 | homing | power cycle | idle-drift lock 14 s later |
| 3 | 11-20 04:56 | over-current X | jog X 176,113 → 0, after 4 stopped jogs | 2,839 counts (1.25 mm) in 0.25 s, then dead; APOS = CPOS | stopped short | power cycle | **X's power-up homing tripped 2.5 s later (#4)** |
| 4 | 11-21 16:59 | over-current X | power-up homing (0x1009) | stalled at −5,684 | homing | power cycle | 7 jog legs, then #5 |
| 5 | 11-21 17:14 | power stage off Y | at rest, 3 s after a stopped Y jog; no fault flag | APOS = CPOS 15,847 | at rest | Re-home failed (X's approach stopped 168,826 counts short); power cycle | **Y's power-up homing tripped (#6)** |
| 6 | 11-21 17:16 | over-current Y | power-up homing (0x1009) | stalled at −12,025; I peak 18,024 | homing | Re-home homed both, Y stood at −6 (refused); power cycle | 79 legs clean over 55 min; a Y landing lock |
| 7 | 11-25 09:21 | oscillation Y | the Re-home's return leg, Y → 13,667, just after that Re-home cleared a −32 count mismatch | landed: APOS 13,674, CPOS 13,667 | landed | Re-home (pass; Y homed switched off from 6.0 mm) | return leg clean; **the next move locked 70 s later** (+13 landing) |
| 8 | 11-25 14:28 | over-current X | Multi-D B2 → B5: X 193,352 → 62,052 at medium | see below | **ran back** | Re-home failed: Y's approach landed −6, one count outside the band | still locked |

**#8 is not "X barely left B2".** APOS 192,661 against CPOS 156,342 at the latch reads like an axis that never left
its start while its command ran on. The 20 Hz samples show otherwise: X travelled 37,010 counts (16.3 mm) towards B5
at ~9,000 counts/s for 4.1 s, reached 156,342, and then ran **36,319 counts back against its command in about 0.8 s**
(8,000 counts in one 63 ms sample, ≥ 40,000 counts/s), with the current saturated at −16,776, to stop 691 counts short
of where it started. The shadow watchdog (ADR-0047) flagged `runaway_against_command` and a following error of −13,822.
That is the commutation-reversal signature of ADR-0030 (the motor pushes harder the wrong way the further it falls
behind), not a mechanical block. The wrong-way guard did not fire because X never got further from its target than
its start. **APOS against CPOS at the latch cannot tell these apart; the closest approach during the leg can.**

### What the data says about retrying
- **No trip type has a record that shows a retry is safe.** One software Re-home cleared a drive-fault lock (#7);
  its return landed and the next move locked 70 s later. Power cycles were followed by a new trip of the same drive's
  homing twice (#3 → #4, #5 → #6).
- **Homing is the riskiest single step on record:** 3 of the 8 trips (#2, #4, #6) happened in the drives' own
  homing, and a software Re-home is that same homing. The 13 software Re-homes homed X 9 times of 10 (one aborted in
  0.11 s, 11-25 09:27) and Y 6 of 6.
- **A ran-back (#8) must not be retried automatically.** The phase can still be wrong after the drive switched the
  axis off; whether the home request's `AXISON` re-aligns it is not known.
- **Every move trip left the tripped axis switched off far from home:** #1 Y at 19.8 mm, #3 X at 76.4 mm, #5 Y at
  7.0 mm, #7 Y at 6.0 mm, #8 X at 85.0 mm. A switched-off axis cannot make the approach leg, and the only reviewed
  way to switch it on is its home request (`HOMETOHARDSTOP` does `AXISON` itself), which would then home it from
  mid-travel into the stop. Under the standing rule against that, **none of the eight trips would have been
  recovered automatically**: three are homing trips, one ran back, four are switched off far from home. With the
  operator's choice below, those four (#1, #3, #5, #7) are attempted.
- Y also reads switched off with no fault flag after a lock and a stop (11-19 13:35, 11-25 09:27, 11-25 15:17, and
  #5 itself). Why is not known.

## Decision
A bounded **recovery ladder** that runs without the operator, behind a setting that is on by default
(`basic.stage_auto_recovery`, Settings › Interface › "Recover the stage automatically after a drive trip").

### When it runs
All of these, checked again on the service thread before anything is written:
1. The latch is a drive fault: `overcurrent_trip`, `oscillation_axis_off` or `power_stage_off` (`DRIVE_FAULT_RULES`).
   Comm-abort, drift, landing, wrong-direction and any other lock are out of scope (they keep ADR-0057/0058/0061).
2. It was set in this session (not carried over from an earlier one) and no Re-home or recovery is running.
3. **The tripped axis has not had its automatic attempt this session.** One attempt per axis per session, used up
   whether it recovers or escalates. A second trip of that axis, or any trip during the ladder, is a hard lock.
4. **The trip class allows it** (`classify_trip`, from the leg's start, target, closest approach and the drive's
   `APOS` read at the latch):
   - `stuck_at_start` (never more than 100 counts from its start), `stopped_short` (got further, did not run back),
     `landed` (within 5 counts of its target, e.g. an oscillation after landing), `at_rest` (no leg: a status poll
     found it): **eligible**;
   - `ran_back` (more than 100 counts, `WRONG_WAY_COUNTS`, back from its closest approach) and `homing`
     (`ISTATUSFLAGS` bit 3): **hard lock at once**.

Whether the tripped axis is still powered does not decide it: a switched-off axis is homed from where it stands
(step 4, the operator's choice below).

### The ladder
Every step is written to the monitor's command log and to `stage-recovery-<time>-<outcome>.json` in the stage
evidence folder (lock, class, the app's steps, fresh reads, each axis' homing trace, test legs, return, outcome).
1. **Light off, run paused.** `StageAutoRecovery` (app side) switches every LED off with the board's halt. A Multi-D
   or Scan run is blocked in the call, so it is paused by construction; Multi-D records `state: paused` (stage
   recovery) in its log. Images already taken are kept on disk as before.
2. **Objective lowered to Z 0**, the turret-safe height, through the router (the focus device's checks apply). If Z
   does not reach 0 nothing goes to X or Y: hard lock.
3. **Fresh drive reads** on the service thread: X, Y (`REHOME_PRE_READS`), Z, P/F/O states, turret sensor. The
   ADR-0058 refusals apply (not idle, `MER` ≠ 0, `SRH` fault bit, Z not at 0, P/F/O busy, turret between positions).
4. **One software re-home of X then Y** (ADR-0058: approach leg to 1,000 counts for a powered axis further out than
   3,000, `ESTATE` 19, verify, park a stand-off). An axis its drive switched off cannot make the approach leg; its
   home request switches it on (`HOMETOHARDSTOP` does `AXISON`) and it homes from where it stands. That is recorded
   as its own step and as `homed_from_travel` (axis → mm from home) in the evidence file. The lock clears only on the
   existing `rehome_done` rule.
5. **Test move** of the tripped axis alone, at the slow preset (1.0 mm/s): three verified legs of 1.0 mm outwards from
   where it homed, each with every leg check (drive flags, still axis, landing). The drive's earlier speed is written
   back afterwards. Any trip here latches again; the axis' attempt is used, so it is a hard lock.
6. **Return** X then Y to where the stage stood before the ladder, with the ordinary verified move.
7. **Objective back** to its earlier Z (for a Multi-D run with the operator's above-safe confirmation, confirmed;
   otherwise the safe-height check applies).
8. **Resume or escalate.**
   - Multi-D (amends ADR-0065): the position whose move failed is retried once. If that move fails and the stage is
     locked, the run ends "partial" with its data, as before; if the stage is not locked, the position is skipped
     (recorded in the log) and the run goes on.
   - Scan: the same for the tile.
   - Manual moves (no run): the shell starts the ladder when it sees an eligible lock and no task is running; the log
     reports each step.
   - **Escalation** leaves the stage locked (or locks it again with `auto_recovery_failed` if the failure came after
     the lock had cleared), with a plain message naming the step that failed and that the axis' automatic attempt is
     used. Only a power cycle or the operator's Re-home clears it.

### Amendment to ADR-0058: an approach leg is a staging leg
The 14:55 Re-home after #8 failed because Y's approach leg (63,054 → 1,000) settled 6 counts short, one count outside
the 5-count band, and re-approaches are off under a re-home's lock. An approach leg only brings an axis near its stop
before homing sets its position afresh. **An approach leg now accepts a landing within 10 counts**
(`LANDING_RETRY_MAX_COUNTS`) after the settle window; it is recorded as `accepted (approach)` in the landing log.
Every other leg keeps the 5-count band. This applies to the operator's Re-home too.

### Operator's choice (2026-11-25): a switched-off axis homes from where it stands
Asked whether the recovery may home a switched-off axis from where it stands, the operator answered "Allow it". This
is what the operator's Re-home does today (ADR-0058 amendment, unchanged here) and what every power cycle does
(power-up homing starts wherever the stage stands, both axes at once). It is the only way the ladder can recover #1,
#3, #5 and #7. It sets aside, for this one homing per axis per session, the standing rule against homing from
mid-travel. The known cost: from mid-travel the homing reaches about 5.6 mm/s before it hits the stop, against about
2.6 mm/s from near home ("a screech", 2026-11-19), and Y passes its tight spot about 1.9 mm from home on the way. One
attempt per axis per session still holds, so it happens at most once per axis before the operator is needed.

## Not chosen
- **Switching the axis back on in place** (the drive's `AXISON`), then the approach leg, then homing. No reviewed
  frame exists; it needs the research and a motor gate on the instrument first.
- **Retrying the move without a re-home.** The drive has switched the axis off; the phase may be wrong.
- **Changing the drive's protection** (current limits, oscillation detection). No drive parameter is written.

## Consequences
- Of the eight recorded trips, the ladder would attempt four (#1, #3, #5, #7), each homing the switched-off axis from
  6–76 mm, and hard-lock the other four at once (the three homing trips and #8, which ran back). Whether those four
  would have passed is not known: after the one comparable software recovery (#7) the next move locked 70 s later, and
  after #3 the power-up homing of the same axis tripped. In every case the light goes off and the objective comes
  down without the operator, the trip is classified and recorded, and the run keeps its data.
- A recovery takes about the length of a Re-home (X ~16 s, Y ~10 s homing, plus approach and return legs) and three
  short slow legs (~5 s).
- The second trip of an axis in a session always needs the operator. So does any lock outside `DRIVE_FAULT_RULES`.
- Not yet seen on the unit: the ladder end to end, a homing from mid-travel started by the ladder, the slow test legs
  after a homing, and whether a re-home after a `ran_back`-free over-current gives a clean next move. The first runs on the instrument need the operator's approval.
