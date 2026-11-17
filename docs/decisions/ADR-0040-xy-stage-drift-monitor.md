# ADR-0040: X/Y stage drift monitor and one-axis-at-a-time moves

- **Status:** Accepted (2026-11-15). Tested on the simulated drive chain with injected faults. First run on the
  instrument (COM3) 2026-11-16: see "On the instrument" below. That run found two faults, both fixed.
- **Source:** Y-axis plan v3 (`Y_axis_commutation_drift_findings_and_fix_plan.md`, 14 Nov 2026): roadmap P0, the T0
  logging, the §6.3 latch rules and the ground rule "no combined X/Y moves".
- **Code:** `src/openevos/backends/technosoft/stage_monitor.py`, wired into `telemetry.py`.
- **Builds on:** ADR-0024 (X/Y control), ADR-0030 (Gate 9 status reads; 3-count pre-move check), ADR-0032 (speeds).

## Context
- Y is a closed-loop stepper commutated from the same encoder that gives `APOS`: 6.25° electrical per count, so
  ~14 counts reverses its torque.
- While only X moved, Y's count changed 0 → −3 → −8 → −9 (2026-11-13). Y then ran the wrong way or tripped.
- The physical cause is still open (encoder mounting, contact, or electrical coupling).
- Until now the app checked Y only right before Y moved, and right after an X move.

## Decision

**1. Logging (plan T0).**
- X and Y `APOS` are read back to back every 50 ms, between and through the ~1 Hz status poll, whenever motor
  control is on. The period counts from each sample's start.
- On COM3 this gives ~15 Hz: during the slow poll a sample waits for the read in progress (~16 ms).
- The last 40 s stay in memory.
- `--stage-log` (real port only) also writes every sample to
  `openevos-scans/stage-monitor/stage-log-<time>.csv`.

**2. New reads (read-only).** X/Y `SRL`, `ISTATUSFLAGS` and `BDOAXISOFF` join the status poll. `MER`, `IIQMAX`,
`IIQMIN`, `IQ` and `LCOMMERRPOS` are read only at a latch.
- All of them except `LCOMMERRPOS` were read on the unit by Gate 9; a test pins their addresses to Gate 9's.
- `LCOMMERRPOS` is 32-bit at 0x03BC (from `variables.cfg`) and has not yet been read on the unit.

**3. Latch rules.** Any of these blocks every X/Y move:

| Rule | Trigger |
|---|---|
| `idle_encoder_drift` | An axis OpenEVOS is not moving changes more than 2 counts from its reference. Checked at ~20 Hz, including while the other axis moves |
| `command_mismatch` | \|`APOS` − `CPOS`\| > 3 counts on an axis about to move. At rest this is only a warning |
| `overcurrent_trip` | `ISTATUSFLAGS` bit 0 (`MER` reads 0 after this trip) |
| `oscillation_axis_off` | `ISTATUSFLAGS` bit 13 |
| `comm_error_abort` | `ISTATUSFLAGS` bit 4 |
| `power_stage_off` | `SRL` bit 15 clear on a homed axis. OpenEVOS never switches X or Y off |
| `wrong_direction`, `landing_error`, `cross_axis_encoder_drift` | Detected by the move loop |

- **References.** An axis takes its reference 0.5 s after OpenEVOS ends its move. A still axis takes it from a fresh
  read at the start of the other axis' move (Gate 10: `APOS` sits within 0–1 count of `CPOS` after a leg).
- **Values checked.** Gate 9's reads on a healthy stage (`ISTATUSFLAGS` 0xD000, `SRL` 0xC400) do not latch. Its
  reads after Y's trips (0x1001; `SRL` 0x4400) do.
- **Not latched.** "Did not move at all" with the power stage reading on stays a failure without a lock (operator
  decision, 2026-11-14). If the power stage is actually off, `power_stage_off` latches.

**4. Containment.**
- **During a move:** controlled stop, and the command fails with the rule's name.
- **At the latch:** the drive's own record (flags, current peaks, `LCOMMERRPOS`) is read before anything else is sent.
- **Display:** the stage shows WARNING health with the reason, and the safety layer's refusal names it.

**5. Recovery.**
- **Never automatic.** The latch does not clear when `APOS` and `CPOS` agree again.
- **What clears it:** OpenEVOS seeing both X and Y re-initialise, meaning `IINITONCEDONE` leaves 12345 and returns.
  That is the power-on homing after a power cycle, done with the app running and connected.
- **Restarts:** on the real port, the latch is kept in `openevos-scans/stage-monitor/stage-latch.json`, so it
  survives an app restart. A simulated stage never writes or reads that file.
- **Unreadable file:** if the file can't be read, the stage stays locked (fail closed).

**6. Evidence.** 10 s after a latch, it writes `stage-monitor-<time>-<rule>.json` and a CSV.
- **The JSON:** the latch, thresholds, status polls, commands and drive reads.
- **The CSV:** columns `t, x_apos, y_apos, x_cpos, y_cpos, x_cmd, y_cmd`. `y_drift_correlate.py` reads it unchanged
  (checked).

**7. One axis at a time.**
- Until the cause of Y's drift is found, X and Y never move together.
- A target that changes both runs as two legs, X then Y.
- **Between the legs:** a 0.5 s settle, sampling X and Y throughout. Then the latch is checked and both axes are
  re-read.
- **Why the settle:** a drive reports motion complete inside its 5-count band, and the axis keeps converging. Without
  the settle, X's reference for Y's leg was taken mid-settle, so X settling could latch as drift.
- The joystick maps to four directions; diagonals are gone.
- An axis already within 5 counts (2.2 µm) of its target is not commanded at all.

**8. Settings.** Thresholds are protected settings: 2 counts idle, 3 counts before a move, 0.5 s settle, 50 ms
sample period, 40 s ring, 10 s after an event. They are recorded with every snapshot, and changing one needs an ADR.

**9. Power-up record (added 2026-11-16).** These closed-loop steppers take the rotor angle from the same encoder
that gives `APOS`, so no direct motor-versus-encoder comparison exists. Two indirect checks are recorded instead,
read-only, once per homing, into `openevos-scans/stage-monitor/power-up-history.csv`:
- **Index audit (plan T4, §6.7).** X and Y `LHOMEINDEXDELTA` (0x03F4, the distance from the homing hard stop to
  the encoder's index), with `HOMEDONE` (0x03B0).
  - A change of more than 10 counts on one axis since the previous record becomes a standing stage warning: its
    encoder may have moved relative to the mechanics.
  - It is never a lock.
- **Holding current.** `IQ` at rest, 20 samples per axis interleaved over about 0.5 s (mean, min, max), alongside
  `APOS`, `CPOS`, `ISTATUSFLAGS` and `SRL`.
  - With `APOS` = `CPOS`, a much larger Y current than X's means a load on the carriage or a commutation phase error.
  - Torque falls as cos(phase error), so 9 counts of slip needs about 1.8× the current.
  - Existing records show Y at rest at −1,132…+1,863, against X's 88–461 (Gate 9, Gate 15, Run C).

**Who records:**
- **The app:** at connect, and after each re-home it sees. It waits 5 s after X and Y report homed, with neither
  moving and no move queued. Real port only.
- **The CLI, for gate sessions without the app:**

  ```
  python -m openevos.maintenance.motor_power_up_record --instrument-on-2min
  ```

  It has no writes, and appends to the same file.

## On the instrument (COM3, 2026-11-16)
Run with `--motor-control --stage-log` and simulated cameras, the stage just re-homed after a power cycle. Logs are
in `openevos-scans/stage-monitor/stage-log-20261115T19*.csv` (git-ignored).

**At rest (6 min over three app starts).** X and Y read exactly 0 the whole time, with no change of a single count.
No latch, no warning.

**Moves.** The same three moves twice: X +0.1 mm, Y +0.1 mm, then "Go to 0, 0" (X leg, then Y leg).
- **The still axis never changed** during the other axis' single move. During the go-to's Y leg, X changed by
  1 count in the first run and by none in the second.
- **X overshoots** 16–20 counts in a short move, then is within ±1 count of its target ~0.5 s later.
- **Y ends a move to 0 at 4 counts** (1.8 µm), with `CPOS` also 4. It sits inside the drive's 5-count band, so it is
  not a landing error. The stop at rest sets `CPOS` to where the axis stands, as the simulator models.
- No latch, and no lock file was written.

**Faults the run found (both fixed, each with a test that reproduces it):**
- **Sample rate ~10 Hz.** The 50 ms period was counted from the end of each sample, so the sample's own two reads
  added to every period. The simulator's reads take no time, so its tests could not see it. The new test gives each
  read 15 ms.
- **Go-to: false-latch risk.** Y's leg started while X was still settling (−19 → −1 → 0), and X's reference was
  taken then. Fixed with the settle between legs (section 7). With it, the second run's go-to logged X settling
  during the wait, and X held still through Y's leg.

**Known gap.** The CSV has a 0.3–0.6 s hole around each leg, where the fresh pre-move reads and the post-move flag and
backstop reads run. A 0.1 mm move gets one sample in the middle. Drift in those windows is still caught: by the
pre-move check, by the backstop read after the leg, and by the first sample after it. A longer move is sampled every
50 ms throughout.

## Not in this ADR (later, plan §6)
- The runaway watchdog with host `AXISOFF`: a new write, which needs its own spec and one validation run on an idle Y.
  Spec: Motor Gate 15 (`docs/evidence/fl1_v1.6_motor_gate15_y_axisoff_spec.md`; ADR-0044), which also sets the rules for using
  `AXISOFF` in the app.
  - The watchdog itself: spec `docs/evidence/fl1_v1.6_stage_watchdog_spec.md`, ADR-0047 (Phase 1 built). A read-only
    shadow mode first, to calibrate its thresholds; then `AXISOFF` on Y, and stop and latch on X.
- The stage feedback screen.
- Gate R (single-axis `RESET`).
- Using the logger from the maintenance gates (T1 Run C/A).

## Consequences
- **Line load:** the serial line is busy almost all the time while motor control is on. A move still starts within
  one read (~16 ms) of its request.
- **Move time:** each leg adds ~4 reads (~65 ms) for the flags and the backstop check. A go-to that changes both X
  and Y adds 0.5 s for the settle between its legs.
- **Y after X travel:** a Y that drifts during X travel now stops the X move within one sample (50–65 ms) and blocks
  Y's next move.
  Before, that was caught only at the end of the X move or by Y's own pre-move check.
- **Diagonal moves are gone** until the ground rule is lifted.
