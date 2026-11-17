# ADR-0044: Motor Gate 15, host `AXISOFF` on the idle Y axis

- **Status:** Accepted (2026-11-16). Implemented, tested against a fake drive chain, and **run once on the
  instrument: PASS** (`motor-gate15-20261115T194640-pass`; Y off 15 ms after the acknowledgement, Y held at 0 counts
  while off), then **recovered** after a power cycle (`…T194911-recovered`). See spec §12.
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate15_y_axisoff_spec.md`.
- **Code:** `src/openevos/maintenance/motor_gate15_y_axisoff.py`; tests in
  `tests/test_maintenance_motor_gate15_y_axisoff.py`. Registered as `motor.y_axisoff_idle` in `REVIEWED_MAINTENANCE`.
- **Source:** Y-axis plan v3, §4 run order step 3, and §6.3 (watchdog and escalation).
- **Builds on:** ADR-0018 (first write, `O` acknowledgement), ADR-0030 (Gate 9 status reads), ADR-0040 (drift
  monitor).

## Context
Three later steps need a working host `AXISOFF`:
- T1 Runs C and A switch Y off before X moves;
- the Run B guard switches Y off at 6 counts of drift;
- the runaway watchdog switches a runaway axis off.

None has been sent by OpenEVOS. The plan asks for one try on an idle Y, with nothing else moving, confirmed by `SRL`
bit 15 = 0.

## Decision
1. **One constant write.** W1 = `04 00 20 00 02 26`: TML `AXISOFF` (0x0002) to axis 2. This is the vendor's
   `HaltAllMotors` frame for Y, sent at most once per run, preceded by a line sync and checked for one `O`.
   - The tool cannot build anything else: no `AXISON`, no other axis, no broadcast, no state or variable write.
     Tests pin this.
2. **Refuse unless all of these hold:**
   - the operator's confirmations (spec §4 Phase 0) and the phrase `SWITCH OFF IDLE Y AXIS ON COMn`;
   - every axis idle and X/Y/Z/F homed;
   - Y on and settled, with its settle window over, its settle check not armed, and \|`APOS` − `CPOS`\| ≤ 3;
   - Y still within ±1 count for 1 s;
   - X on and idle.
3. **Timed reads use the app's read timing.** Gate 4's link waits 50 ms before every reply (~65 ms per read), which
   is too coarse to time a 100 ms limit.
   - The off check, the 10 s hold and the stillness check instead read the exact reply length with no fixed wait,
     about 16 ms per read on COM3.
   - The line is synced once, and again only after a defective reply, with one retry. This is the app's
     `DriveLink`, measured on COM3 with 0 failures in 100.
   - The untimed reads keep Gate 4's sync-every-read path, which stops at the first bad reply.
4. **Pass:**
   - one `O`, and Y `SRL` bit 15 = 0 within 100 ms of it;
   - Y off at every read of a 10 s hold;
   - Y `ESTATEV`, `CPOS` and `ISTATUSFLAGS` unchanged;
   - X unchanged (on, idle, `APOS` ±1, same flags);
   - every axis idle.
5. **Never a second `AXISOFF`.** A late drop is timed for up to 1.1 s, then fails.
6. **Reported separately:** how far Y's encoder moved while unpowered. More than 2 counts is a finding for T1 Run C,
   not a failure.
7. **Recovery is always a power cycle.** The tool never switches Y back on. `--check-recovered` (read-only) then
   confirms X and Y are on, homed and free of trip flags, and writes its own evidence file
   (`motor-gate15-<time>-recovered.json`).

## Consequences
- **Until it passes on the unit,** nothing in OpenEVOS may rely on a host `AXISOFF`: not the T1 Y-off runs, the Run
  B guard or the watchdog.
- **After it passes,** each use still needs its own ADR, under the rules in spec §9:
  - Y's frame only;
  - sent from the telemetry thread only;
  - confirmed within 100 ms, else escalated;
  - Y refused until a power cycle.
- **Running it costs two power cycles,** one before and one after. Each resets the cube changer, so the mirror
  position must be restored afterwards (Gate 12).
- **Not covered:** `AXISOFF` on a moving axis, and `AXISOFF` on X or any other axis.
