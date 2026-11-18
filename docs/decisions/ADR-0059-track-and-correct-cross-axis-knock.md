# ADR-0059: Track and correct a small cross-axis knock instead of locking (amends ADR-0040, builds on ADR-0057)

- **Status:** Accepted (2026-11-19). Simulator-tested; not yet seen on the unit.
- **Asked for by:** the operator, 2026-11-19: "stage locked couldnt change, when we already know the creep cant we keep
  track and correct it".
- **Code:** `backends/technosoft/stage_monitor.py` (`CROSS_AXIS_TOLERANCE_COUNTS`, `disturbed`, `record_disturbed`,
  `take_disturbed`); `backends/technosoft/telemetry.py` (`_leg_still_axis_ok`, the Y-leg pre-move check in
  `_run_move`, `_correct_knocked`, `CORRECT_APPROACH_COUNTS`).
- **Tests:** `tests/test_cross_axis_knock.py`; the knock bound in `test_stage_monitor.py`, `test_drive_steps.py`,
  `test_technosoft_motion.py`, `test_ui_merge_physical.py`.

## Context
On 2026-11-18 the drift monitor locked the stage five times. Each time, Y lost 2–6 counts (1–3 µm) while X moved at
full speed, always as X passed about 33–41 mm (18:56: −2→−4; 19:26: −2→−4; 20:09: −2 at X 32.9 mm, −6 at 38.8 mm).
Y did not return by itself after X stopped. ADR-0040 locks on more than 2 counts of change on a still axis, and the
backstop at the end of a leg on more than 3. Each lock stopped the objective-change centring, and the small ones
needed a manual release (ADR-0057) or a re-home (ADR-0058).

## Decision
- **While the other axis moves**, a still axis may change by up to `CROSS_AXIS_TOLERANCE_COUNTS` = **10** counts
  (4.4 µm). That is the same bound as the operator release (ADR-0057), and under the ~14-count commutation reversal
  (ADR-0030). Inside it, the change is recorded as a knock, not latched. Beyond it, the monitor latches as before.
- **At rest** (nothing moving), the 2-count rule is unchanged. A still axis that moves on its own still locks.
- **The next leg of the same go-to** may start that axis up to the bound off its command. Its own move and landing
  check put it right. The 3-count pre-move check still applies to every other mismatch.
- **After the move**, any axis left more than the landing band (5 counts) off its requested position is put back:
  - one axis at a time;
  - a verified leg `CORRECT_APPROACH_COUNTS` = 100 counts (44 µm) past the target, then a verified leg onto it, so the
    drive really moves even if its command never followed the knock;
  - both legs carry every leg check (drive flags, the still axis, the landing).
- **Recorded:** every knock and correction is noted in the monitor's log and in the stage evidence.

## Consequences
- The stage no longer locks for the known small Y knock. It ends where it was asked to be, verified by the landing
  check.
- A larger knock, a knock repeated beyond the bound, drift at rest, and every drive-flag rule still lock.
- The cause (something in X's travel near 33–41 mm jolting Y) is still unexplained and should be looked at
  mechanically. A slower speed through that region may avoid the knock.
