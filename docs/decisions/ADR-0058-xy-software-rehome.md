# ADR-0058: X/Y software re-home during a stage lock (amends ADR-0040)

- **Status:** Accepted (2026-11-19). Part 1 (Motor Gate 20) **run on the instrument 2026-11-19: PASS** (X homed in
  17.7 s, Y in 10.6 s; research note §11). Part 2 (the app action) implemented and tested on the simulated drive chain;
  not yet used on the instrument.
- **Asked for by:** the operator, 2026-11-19: "i want to be able to re home (as in powercycle) during stage lock (of
  course with a notification about stage lock rehoming), and then reach to the old position". Option A of the
  research note approved by the operator the same day.
- **Evidence:** `docs/evidence/fl1_v1.6_xy_software_rehome_research.md` (offline decode of the byte-exact `XAxis.sw`,
  `YAxis.sw` and `MotorController64.dll`).
- **Amends:** ADR-0040 (a lock that is not drift-releasable cleared only through a power cycle).
- **Code (part 1):** `maintenance/motor_gate20_xy_rehome.py`; registered as `REVIEWED_MAINTENANCE["motor.xy_rehome"]`.
- **Code (part 2):** `backends/technosoft/protocol.py` (`XY_HOME_FRAMES`, `home_frame`); `stage_monitor.py`
  (`rehome_blocker`, `begin_rehome`, `end_rehome`, `rehome_done`, `REHOME_READS`); `telemetry.py` (`rehome_stage`,
  `_RehomeJob`, `_run_rehome`); `transport.py` (the simulated drive homes on `ESTATE` 19); `ui/shell.py`
  (Microscope › Re-home stage…, offered from the Stage pill, a refused move and Release stage lock);
  `ui/precheck_dialog.py`; `acquisition/precheck.py`.
- **Tests:** `tests/test_maintenance_motor_gate20_xy_rehome.py`, `tests/test_stage_rehome.py`,
  `tests/test_stage_lock_prompt.py`.

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

## Part 2: the app action
- **Where:** Microscope › Re-home stage…; and wherever the app offers the lock release (the Stage pill, a move refused
  by the lock, Microscope › Release stage lock, the precheck dialog): a lock the release cannot clear now leads to the
  re-home instead of a dead end. Small drift locks keep the ADR-0057 release.
- **Notification:** a confirmation names the lock (rule, axis, message), says the stage re-homes X then Y as after a
  power cycle, where it will return to, that Z must be at 0, and that Stop cannot interrupt an axis while it homes.
- **On the service thread (`_run_rehome`):**
  1. fresh reads; **refuse, sending nothing**, if X/Y are not idle (state 0 or 13; state 13 hands `ESTATE` on like
     idle, `XAxis.sw` 0x4388), have `MER` ≠ 0 or the `SRH` fault bit, Z is not at 0 (±5 counts), or P/F/O is busy;
  2. remember X/Y (`APOS`);
  3. for X, then Y: register the home frame as `extra_writes` for that one write, send it, follow `ESTATEV`/`HOMEDONE`
     (started ≤ 5 s, done ≤ 120 s); the monitor treats the axis as moving meanwhile; a stop request before Y ends
     the re-home; any fault ends it with nothing more sent and the lock kept;
  4. **the monitor's new clear rule** (`rehome_done`): fresh `REHOME_READS` of both axes must show initialised, homed
     (`HOMEDONE` bit 0, state 0), no interrupted/over-current/oscillation/comm-abort flag, `MER` 0, powered, `APOS` and
     `CPOS` within 5 counts of 0. Only then is the latch cleared (`stage-latch-cleared-*.json`);
  5. a fresh poll, so the ADR-0056 path reloads the calibrated limits if the drives hold the power-up ones;
  6. the return to the remembered X/Y with the ordinary verified move (`_run_move`: X then Y, pre-move drift check,
     landing check), refused if the target is outside the drives' current limits (the stage then stays home).
- **Evidence:** `stage-rehome-<time>-<outcome>.json` with the lock, before/after reads, each axis' homing trace and the
  return.

## Amendment (2026-11-19): approach home before homing
- **Why:** the operator reports that homing with Y halfway yanks it toward home with a screech. The stage logs show
  the cause. Homing from mid-travel reaches the drive's full homing speed (5.6 mm/s) before it hits the hard stop;
  from near home it touches at about 2.6 mm/s. Every recorded homing also slows sharply on its fast back-off at the
  same place, about 1.9 mm above home, and on 2026-11-18 18:58 it stalled there, went over current and aborted.
- **Change (`_rehome_approach`, step 2a):** before the home requests, each powered axis further out than 3,000 counts
  (1.3 mm) is brought to 1,000 counts with an ordinary verified leg, X then Y. Homing then starts near the stop, as a
  power-up from home does.
  - These legs run with the monitor latched, so they guard the still axis themselves on every poll and stop if it
    changes by more than `CROSS_AXIS_TOLERANCE_COUNTS` (10, ADR-0059). The leg's wrong-way guard, drive-flag check,
    timeout and landing check also apply.
  - Any failure stops the re-home before homing, with the lock kept.
  - An axis switched off by its drive (an over-current trip) is homed from where it is, as before.
- **Recorded:** `approach` in the `stage-rehome-*.json` record.

## Consequences
- A software re-home does not clear latched drive faults (`MER`) and is not a reboot; those still need a power cycle.
- The stage ends at home (0, 0), where the turret collides with the stage: no turret change until it is centred.
