# ADR-0058: X/Y software re-home during a stage lock (amends ADR-0040)

- **Status:** Proposed (2026-11-19). Part 1 (Motor Gate 20) implemented, tested against a fake drive chain, and **run on
  the instrument 2026-11-19: PASS** (X homed in 17.7 s, Y in 10.6 s; research note §11). Part 2 (the app action) not yet built.
- **Asked for by:** the operator, 2026-11-19: "i want to be able to re home (as in powercycle) during stage lock (of
  course with a notification about stage lock rehoming), and then reach to the old position". Option A of the
  research note approved by the operator the same day.
- **Evidence:** `docs/evidence/fl1_v1.6_xy_software_rehome_research.md` (offline decode of the byte-exact `XAxis.sw`,
  `YAxis.sw` and `MotorController64.dll`).
- **Amends:** ADR-0040 (a lock that is not drift-releasable cleared only through a power cycle).
- **Code (part 1):** `maintenance/motor_gate20_xy_rehome.py`; registered as `REVIEWED_MAINTENANCE["motor.xy_rehome"]`.
- **Tests:** `tests/test_maintenance_motor_gate20_xy_rehome.py`.

## Decision
- **The request:** `ESTATE` = 19 on X or Y (`STATE_19_HOME_DOUBLE`). It calls the same homing routine the drive runs at
  power-up, and it is byte for byte what PearlScope sends for "home X/Y":
  - X `06 00 10 21 B3 00 13 FD`
  - Y `06 00 20 21 B3 00 13 0D`
- **Not chosen:** writing `IINITONCEDONE` = 0 then `ESTATE` = 1 (the vendor never does this), and a TML `RESET`
  (unknown encoding; X is the RS-232 gateway).
- **One axis at a time, X then Y.** No stop is sent while an axis homes: the drive does not read `ESTATE` until homing
  ends. The operator's power switch is the stop.
- **Refused when:** any axis is not idle; X or Y has `MER` ≠ 0 or the `SRH` fault bit (a latched drive fault needs a
  power cycle); either is moving; Z is not at home (±5 counts).
- **Pass:** `ESTATEV` 0, `HOMEDONE` bit 0 set, `ISTATUSFLAGS` bits 0 and 3 clear, `MER` 0, `APOS`/`CPOS` within 5
  counts of 0, and `IINITONCEDONE`, the limits and the speed unchanged.
- **Transport:** the two frames are not app motion writes (`protocol.is_motion_write` is false; `check_write` refuses
  them). The app will pass them only as `extra_writes` for the duration of one re-home, as ADR-0056 does for the limit
  writes.

## Part 2 (to follow, after the first gate run)
The app's "Re-home stage" action from the stage-lock prompt: notify with the lock reason, remember X/Y, re-home through
the telemetry service with the monitor watching (a new monitor rule clears the lock on an app-started home seen to
finish, since `ESTATE` 19 leaves `IINITONCEDONE` at 12,345), wait for both axes, then return X then Y.

## Consequences
- A software re-home does not clear latched drive faults (`MER`) and is not a reboot; those still need a power cycle.
- The stage ends at home (0, 0), where the turret collides with the stage: no turret change until it is centred.
