# ADR-0051: Cube channel buttons move the F carriage, then light the cube

- **Status:** Accepted (2026-11-17). Implemented and tested against the simulated drive chain and a fake LED board.
  **Not yet run on the instrument from the app.**
- **Asked for by:** the operator, 2026-11-17: clicking a cube channel should engage the F motor and switch the light
  on, and the light buttons must be able to switch off.
- **Code:**
  - `backends/technosoft/protocol.py`: F frames and F reads.
  - `backends/technosoft/telemetry.py`: `move_carriage` and `_run_carriage`.
  - `backends/technosoft/transport.py`: an F model in the simulated chain.
  - `backends/ledboard/illumination.py`: the `carriage` hook and `UNIT_CUBE_SLOTS`.
  - `acquisition/session.py`: `_link_carriage`.
  - `ui/pages/capture.py`: channel click behaviour.
  - Tests in `tests/test_f_carriage.py` and `tests/test_ui_light_camera.py`.
- **Builds on:**
  - ADR-0035 (Motor Gate 11, F to cube slots 1–4);
  - ADR-0041 (Motor Gate 12, F to the mirror);
  - ADR-0024 (app motion writes);
  - ADR-0048 (real LED board in the app).

## Context
- **The problem:** epi lights whichever cube is in the light path. Until now the app refused a fluorescence channel
  unless the right cube was already there. Only the maintenance gates could move F.
- **What the gates proved:**
  - Gate 11 moved F to slots 1–4 on the unit.
  - Gate 12 moved F to the mirror and back.
  - Both used only the drive's own stored positions: LTARGETPOS = slot, then ESTATE 16 (GOTO_CUBE), or ESTATE 17
    (GOTO_MIRR).
- **This unit's loadout:** slot 1 RFP, slot 2 empty, slot 3 DAPI, slot 4 GFP. The source is LED Gate 3's 0x47 read
  at slot 1, plus the operator's photo.

## Decision
1. **Frames.** The app sends only the F frames Gates 11 and 12 sent, and a test checks them byte for byte:
   - LTARGETPOS = 1..4;
   - ESTATE = 16 (GOTO_CUBE);
   - ESTATE = 17 (GOTO_MIRR);
   - ESTATE = 15 (F's own STOP).

   They join `is_motion_write`, so they reach the port only when motor control is on. The PC never sends F a position.
2. **Sender.** Only the stage service thread, the single owner of COM3, sends them, as a queued carriage job.
   - X and Y keep their ~20 Hz drift samples during the job.
   - No X/Y move runs at the same time, because jobs run one after another.
3. **Refuse with nothing written** unless F:
   - is homed and idle;
   - is at rest;
   - has MER clear and a sane LCONVERGEBAND;
   - holds exactly the reviewed stored positions (0/34,000/68,000/102,000, mirror 128,000) and limits 0..128,000.

   An F already within its band of the target is not commanded.
4. **Completion.** As in the gates:
   - The move completes on ISTATUSFLAGS 0x4000 with APOS within LCONVERGEBAND of the stored target.
   - The final checks require ESTATEV 0 and MER clear.
   - Moving more than 200 counts away from the target, a timeout (distance / 5,000 counts/s + 5 s), or a router STOP
     sends F's STATE_15_STOP. A STOP also stops X and Y.
5. **Light sequence (LED device).** For a fluorescence channel whose cube is not in the path:
   1. Both lights OFF, confirmed by read-back. F only moves in the dark.
   2. F to the cube's slot from `UNIT_CUBE_SLOTS`.
   3. The board's own 0x47 sensor must report that cube.
   4. Only then epi ON, at the slider's level, with the existing 300 s hold limit.

   Each failure leaves the instrument dark:
   - a refused or failed move fails `CARRIAGE_FAILED`;
   - a sensor that disagrees fails `CUBE_NOT_IN_PATH`;
   - a cube this unit lacks (the profile's TxRed) is refused before any move.

   Transmitted channels (trans, phase) do not move F. Whether the mono camera's brightfield needs the mirror or an
   empty slot is not established.
6. **When it is active.** Only when the session has both the real LED board (`--illumination real`) and real motor
   control (`--motor-control`).
   - Then the session sets `carriage_control` on the stage service and hands the LED device its `move_carriage`.
   - Otherwise nothing changes: F is never moved by the app.
7. **Light buttons (UI).**
   - The light is off at launch; nothing lights until the operator asks.
   - Clicking a channel selects it and switches its light on. For a cube, that moves F first.
   - Clicking the lit channel again switches the light off.
   - The Light on/off button stays in step, including after a refusal: the device reports the requested channel as
     dark.
8. **Timeouts.** The light command's router timeout rises from 10 s to 45 s. Gate 12 took 10.4 s from slot 1 to the
   mirror.

## Consequences
- **F moves from the app.** A cube click on the instrument moves F (up to ~10 s). The near-UV DAPI cube can be
  reached, but the light is off while F moves and comes on only once the board confirms the cube.
- **Cube buttons match this unit (amended 2026-11-17).** With the real LED board, the session replaces the profile's
  simulated cubes with this unit's loadout (`session.UNIT_CUBES`): cube1 RFP, cube2 empty (greyed), cube3 DAPI and
  cube4 GFP. Each button's slot is the slot F is driven to, and a test pins that. The simulator keeps its own
  profile. Per-channel settings are stored by slot, so a channel's saved exposure moves with its slot.
- **First use.** The first cube click on the instrument is the first app-driven F move. It needs the operator present
  with the stage clear and nobody looking into the light path.
