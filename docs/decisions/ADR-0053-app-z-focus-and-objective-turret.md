# ADR-0053: Z focus and the objective turret from the app

- **Status:** Accepted (2026-11-17). Implemented and tested on the simulated drive chain. **Not yet used on the
  instrument from the app.**
- **Asked for by:** the operator, 2026-11-17: "map all objectives and z axis movement to the ui".
- **Builds on:**
  - ADR-0052 (Motor Gates 17–19). Gate 18: Z +1 mm and +5 mm and back, 0 counts error; + = towards the stage.
    Gate 19 rev 2: a full 5-step turret cycle with the stage centred and Z at 0, no contact.
  - ADR-0024/0040 (X/Y in the app).
  - ADR-0051 (motor writes from the app).
- **Code:**
  - `backends/technosoft/protocol.py`: Z and O frames.
  - `backends/technosoft/telemetry.py`: focus and turret jobs, and O's position from its sensor.
  - `backends/technosoft/transport.py`: Z and O in the simulated chain.
  - `acquisition/session.py`: device routing and this unit's objectives.
- **Tests:** `tests/test_app_focus_turret.py`.

## Decision
1. **Frames.** Only the gates' frames, checked byte for byte by tests.
   - **Z:** `CPOSCMD` (0..66,000 counts only, so never below home), `ESTATE` 8, `ESTATE` 6.
   - **O:** `ITARGETPOS` 1–5, `ESTATE` 3 and the vendor's `AXISOFF`.
   - Speed, acceleration and limits are never written. All of them pass `is_motion_write`, so they reach the port only
     with motor control.
2. **Sender.** The stage service thread, the single owner of COM3, as queued jobs. X/Y keep their drift samples
   during a Z move.
3. **Focus move.** Exactly Gate 18:
   1. refuse unless Z is homed, idle, error-free and its encoder agrees with its command;
   2. target, then move;
   3. complete on `SRH` bit 9 + `ESTATEV` 13 + `CPOS` = target, with `APOS` within `LCONVERGEBAND`;
   4. then the stop at rest.

   Moving more than 200 counts away from the target, a timeout, or STOP sends Z's stop. The range offered to the UI
   is 0–8.05 mm (`LPOSMAX` 67,000 less 1,000 counts).
4. **Objective change.** Exactly Gate 19 rev 2.
   - **Refused, with nothing written,** unless the stage is centred (X 40–80, Y 20–60 mm) and the turret is idle,
     off, not stuck, error-free and at a known position.
   - **Sequence:** **Z is retracted to 0 first**, then target, then `GOTO_POS`.
   - **Polling:** about every 0.1 s. Positions must follow the **decreasing** path.
   - **Done:** sensor = target, program idle, motor off, on 2 polls in a row.
   - **Fault:** stuck flag, a position off the path, Z moving, a timeout, or STOP sends `AXISOFF` once.
   - **After a change, Z stays at 0;** the operator refocuses. Parfocal compensation (`session.change_objective`)
     applies only once parfocality is calibrated in the app.
5. **Reported positions.**
   - Focus in µm, frame `focus`.
   - The turret as its sensor position, 1–5, with 0 = between positions. Its `APOS` stays 0, so it is not used.
     The router verifies a turret change against the sensor.
6. **Routing.** With `--motor-control`, the profile's focus and turret devices become `motor.focus` and
   `motor.objective_turret`, and the objective buttons become this unit's lenses (4X, 10X, 20X, 40X, 60X; from `tpco`,
   not calibrated). The telemetry option `focus_turret` (default on with motor control) turns this off.

## Consequences
- **Turret buttons need a centred stage.** Elsewhere the change is refused with the reason ("centre the stage"),
  never risked.
- **No safe Z with a vessel on the stage is measured yet.** Every objective change retracts Z to 0, the most
  retracted position, and the stage must be centred. Turning the turret with a vessel on the stage is untested.
- **Pixel sizes** come from the nominal magnification until calibrated in the app.
