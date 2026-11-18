# ADR-0057: Operator release of a small stage-drift lock (amends ADR-0040)

- **Status:** Accepted (2026-11-18). Implemented and tested against the simulated drive chain. Not yet used on the
  instrument.
- **Asked for by:** the operator, 2026-11-18, after the monitor locked the stage on a 3-count Y drift at rest:
  "can't we manually control the stage even after lock". They chose a guarded unlock button over raising the
  threshold or keeping power-cycle only.
- **Amends:** ADR-0040. Until now, only an observed re-homing of X and Y (a power cycle while the app is connected)
  cleared a latch.
- **Code:**
  - `backends/technosoft/stage_monitor.py`: `release`, `RELEASABLE_RULES`, `RELEASE_MAX_DRIFT_COUNTS`;
  - `backends/technosoft/telemetry.py`: `release_latch`, `_ReleaseJob`;
  - `ui/shell.py`: Microscope › Release stage lock…;
  - the precheck dialog's button.
- **Tests:** `tests/test_stage_lock_release.py`.

## Decision
- **Which locks:** only the drift-type locks `idle_encoder_drift`, `command_mismatch` and
  `cross_axis_encoder_drift`, and only when the drift recorded at the lock is at most 10 counts (≈4.4 µm). That is
  well under the ~14-count point where Y's commutation reverses torque. Over-current, oscillation, comm-abort and
  power-stage-off locks still need a power cycle.
- **Checks:** the service reads X and Y fresh, on its own thread. It reads only and writes nothing to the drives. The
  monitor releases the lock only if both axes are:
  - homed;
  - idle;
  - free of the drive's fault flags;
  - powered;
  - within 3 counts of their command, the existing pre-move rule; otherwise the next move would lock again.
- **Confirmation:** the operator confirms in the app after seeing the lock message and the recorded drift.
- **Afterwards:** the release writes `stage-latch-cleared-<time>.json` with the reads, removes the persisted lock,
  sets the stage health back to normal, and takes fresh references, so the monitor keeps watching. A further creep
  locks again.

## Not changed
- The thresholds (idle drift > 2 counts, move drift > 3 counts) and every other rule of ADR-0040.
- No drive write: the release cannot re-sync a command or re-home an axis.
