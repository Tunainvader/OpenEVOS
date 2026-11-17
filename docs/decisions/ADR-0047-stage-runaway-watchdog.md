# ADR-0047: X/Y runaway watchdog, shadow mode first

- **Status:** Accepted for Phase 1 (2026-11-16): shadow mode built and tested on the simulator (spec §11), not yet
  run on the instrument. Phase 2 remains Proposed.
- **Code:** `src/openevos/backends/technosoft/watchdog.py`, wired into `telemetry.py` `_move_leg`; tests in
  `tests/test_stage_watchdog.py`.
- **Spec:** `docs/evidence/fl1_v1.6_stage_watchdog_spec.md`.
- **Source:** Y-axis plan v3, §6.3 (the "Axis Moving" rule, the watchdog's limits and escalation) and the §9 roadmap
  (P0, runaway watchdog).
- **Builds on:**
  - ADR-0040 (drift monitor and move loop; its "Not in this ADR" list names the watchdog);
  - ADR-0044 (host `AXISOFF` on an idle Y; spec §9 sets the rules for using it in the app);
  - ADR-0045 (Run C, the only logged current during motion).
- **Numbering:** ADR-0046 and Gate 16 are reserved for another session.

## Context
The plan asks for a watchdog that switches a runaway axis off. It should trip when one of these holds while the axis
is moving:
- the following error goes beyond the normal maximum + 300 counts;
- `APOS` moves more than 50 counts against the command;
- |`IQ`| exceeds 0.8 × `IMAXPROT` for more than 100 ms.

The thresholds must be calibrated against logged normal motion before they are relied on. No such log exists:
- `CPOS` is logged at 1 Hz;
- Run C logged X current at 1 mm/s only;
- separate `CPOS` and `APOS` reads are ~16 ms apart. That gives up to ~300 counts of apparent error at the fast
  speed, the size of the plan's margin.

Host `AXISOFF` is validated only on an idle Y.

## Decision
1. **Two phases.** Phase 1 measures; Phase 2 acts. Each needs its own build approval.
2. **Following error is read as `POSERR`** (0x022A, read at rest by Gate 9), not computed from `CPOS` − `APOS`. Phase 1
   cross-checks it at every leg end and records its sign. It is trusted only where those checks hold.
3. **"Against the command"** is measured from the closest approach to the target, so a normal overshoot and its return
   do not count.
4. **Phase 1, shadow mode, read-only:**
   - one extra read per move tick, alternating `POSERR` and `IQ` (~15 Hz);
   - a per-leg row in `openevos-scans/stage-monitor/motion-profile.csv`;
   - would-trip notes from provisional thresholds (1,000 / 50 / 13,694 counts). They never act;
   - existing guards unchanged.
5. **Calibration:** ≥ 20 legs per axis per speed preset, including near-full-range legs, each run approved in chat.
   - The thresholds are then fixed in a revision of this ADR: following error = maximum seen + 300; back = 50, or the
     maximum seen + 30 if larger; current = 13,694 on 2 consecutive reads.
   - They are protected settings (ADR-0040 §8).
6. **Phase 2, armed:**
   - **Y:** the constant `AXISOFF` frame `04 00 20 00 02 26` is sent inline from the telemetry thread through
     `protocol.axis_off("Y")`, which refuses every other axis.
     - It is confirmed by `SRL` bit 15 = 0 within 100 ms.
     - Otherwise: one repeat, a stop, the X/Y latch, a blocking main-switch alarm and `watchdog_failure`.
     - Y is then Off until a power cycle. No `AXISON`, ever.
   - **X:** the controlled stop and the latch only, until X has its own one-try `AXISOFF` run.

## Consequences
- **Phase 1:** the drift monitor samples at ~15 Hz during moves instead of ~20 Hz. Idle sampling is unchanged.
- **Calibration** costs operator time on the instrument, and power cycles where the ground rules need them.
- **Phase 2:**
  - a false trip switches Y off mid-move and costs a power cycle;
  - the first real trip is also the first `AXISOFF` on a moving axis, unless the operator chooses the optional
    one-try validation gate first.
- **The wrong-way guard (100 counts)** stays as the backstop in both phases.
