# X/Y stage: what the lock means, and how to check the stage by hand

Written 2026-11-22 from the stage logs (`openevos-scans/stage-monitor/`). Software side: ADR-0040 (the monitor),
ADR-0057 (release), ADR-0058 (re-home), ADR-0059 (knock correction), ADR-0061 (landing settle and re-approach).

## Is the lock needed?
Yes, but it now trips far less. The X and Y motors are closed-loop steppers driven from the same encoder that gives
the position. If that encoder and the motor fall out of step by about 14 counts (6 µm), the motor's torque reverses
and the next move can run the wrong way, which happened on Y on 2026-11-13 (commanded to 0, it ran to 77.7 mm). The
lock stops every X/Y move the moment the position stops making sense, so the stage cannot crash into the turret, the
condenser or a hard stop.

What changed on 2026-11-22 (ADR-0061):

| Before | Now |
|---|---|
| The landing was checked once, the instant the drive said "done" | The axis gets up to 1 s to settle into the 5-count band |
| One count over the band locked the stage | Still off by up to 10 counts: one automatic re-approach (44 µm back the way it came, then onto the target) |
| A landing lock needed a re-home or a power cycle | It locks only if the second approach also misses, and a miss of 10 counts or less is cleared with **Microscope › Release stage lock** |
| Nothing recorded where it happened | Every settle, re-approach and lock is a row in `landing-log.csv` |
| | A third re-approach of the same axis in one session shows a stage warning naming where |

Locks from over-current, a power stage switching off, a wrong-way move or a large drift are unchanged: those mean
the drive itself protected the motor, and they still need **Microscope › Re-home stage** or a power cycle.

## What the logs say about this stage
| Where | What | When |
|---|---|---|
| **Y, first 2.5 mm from home** | every Y homing slows at ~1.9 mm; it stalled with over-current there and aborted homing; holding current at home ~1,180 vs ~500 mid-travel (X: ~260); since 2026-11-19 Y homes 14–31 counts short of 0 | 2026-11-18 on |
| **Y at home** | landing locks at target 0 (+12, +53 counts); the drive's current peaked at 7,668 and 13,378 | 2026-11-18, -19 |
| **Y at 65.6 mm** | landing lock: stuck 6 counts (2.6 µm) short, pushing with −2,152 (about 4× its usual holding current) | 2026-11-21 23:48 |
| **Y while X passes 33–41 mm** | Y knocked 2–6 counts each time X crossed there at full speed | 2026-11-18 |
| **Y homing** | over-current while homing; power stage switched off | 2026-11-21 22:46 |
| **X at 74.9–76.4 mm** | over-current trip at 76.4 mm; stalled at 74.9 mm; over-current while homing | 2026-11-20, -21 |
| **Both, at X/Y 0,0** | an objective struck the stage frame during a turret turn; Y's home index moved 728 → 739–744 counts at that moment and has stayed there | 2026-11-17 |

The pattern points at **mechanical drag on Y**: near home (worst), and at least once mid-travel. The 2026-11-17
turret strike happened at home, which is where Y is worst.

## Check the stage by hand
Do this with the microscope **switched off**: powered motors hold their position and must not be pushed. OpenEVOS
can stay open; when you switch the microscope back on, X and Y home by themselves and OpenEVOS clears any lock it sees
them home from.

1. **Clear the stage.** Take off the plate and lift out the vessel holder insert, if it comes out, so nothing on top
   can rub.
2. **Look first.** With a torch, look along both sides of the stage and under it, especially at the home corner
   (where the stage sits at X/Y 0,0) and near the objective turret. Look for:
   - a dropped coverslip, plate lid, tip or glass chip;
   - dust, hair or dried medium on the guide rails;
   - the stage's flat cable or any tube pressing on the stage or folding the wrong way;
   - a scrape, dent or bent bracket near the home corner (the 2026-11-17 turret strike).
3. **Feel Y, slowly, end to end.** Push the stage gently along Y over its whole travel, then back. It should feel
   evenly smooth. Pay attention to:
   - **the first 2–3 mm from home**;
   - around **65 mm** (the 2026-11-21 lock);
   - Y's resistance while X sits near **33–41 mm**.

   Note any spot that feels rough, notchy or tight, or that clicks. **If it will not move without force, stop**: do
   not force it.
4. **Feel X the same way** for comparison, especially around **75 mm**. X has been the smoother axis, so it shows you
   what "normal" feels like.
5. **Clean only what is loose.** Remove debris and wipe dust off with a dry lint-free cloth. Do **not** add oil or
   grease unless the instrument's service manual names the lubricant: the wrong one collects dust and makes drag
   worse.
6. **Put the holder back** and check that it sits flat and does not touch the housing anywhere along X or Y.
7. **Leave the stage near its home corner** (where it sits after start-up, X/Y 0,0). Power-on homing then starts
   close to the stop and reaches it at about 2.6 mm/s. From mid-travel it arrives at about 5.6 mm/s: the yank and
   screech heard before.
8. **Switch the microscope on.** X and Y home on their own. OpenEVOS clears the lock when it sees both home.

If you find a rough or tight spot, damage near the home corner, or a cable that rubs, that is a service job for the
instrument's vendor: give them the spots you found and this table.

## After the check
- Use the stage normally. Watch the app log for "re-approaching once" lines and the stage pill for a warning.
- `openevos-scans/stage-monitor/landing-log.csv` builds up a row per difficult landing (axis, position, how far off,
  the drive's current). Several rows at the same position point at the spot to look at again.
- If a landing lock does happen: **Microscope › Release stage lock** for a miss of 10 counts or less. Otherwise use
  **Microscope › Re-home stage** (it lowers the objective, homes X then Y, and goes back to where it was), or a power
  cycle.
