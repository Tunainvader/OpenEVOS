# ADR-0064: Z-stack engine and page

- **Status:** Accepted (2026-11-24). Implemented and tested on the simulator. **Not yet run on the instrument:** the
  on-instrument check below is still to do.
- **Asked for by:** the operator, 2026-11-24: "do a deep audit of the z stack feature … window, ui and function. help me
  to upgrade. do get inspiration from peers."
- **Audit:** "OpenEVOS Z-stack: deep audit and upgrade" (14 function findings ZF-01…14, 11 UI findings ZU-01…11),
  with simulator probes `probe_zstack.py` and `probe2.py`.
- **Code:**
  - `acquisition/zstack.py` (new: plan, pre-flight, runner, record);
  - `imaging/zproject.py` (new: projections, focus measure);
  - `imaging/zstack_io.py` (new: save a run);
  - `ui/pages/zstack.py` (new page; `workspaces.py` re-exports it);
  - `acquisition/capture.py` (`ZStack.complete`, `ImageBuffers.zstack_set`, `safe_height_for`, `stop_zstack`,
    `run_zstack`; `zstack()` now runs through the engine);
  - `acquisition/session.py` (safe-height hook);
  - `acquisition/timelapse.py` (stacks through the engine, saved when there is an output folder);
  - `ui/session_model.py` (Stop all stops a stack);
  - `ui/dialogs.py` and `ui/pages/capture.py` (hot button through the engine);
  - `instrument/settings.py` (`ZStackSettings`).
- **Tests:** `tests/test_zstack.py` (28); `tests/test_audit_phase3_ui.py` updated for the channel list.

## Context
The audit found the Z-stack correct in its core (a verified move before every exposure, focus limits checked, focus
returned, full per-slice metadata) but unsafe or wasteful around it. Confirmed on the simulator:
- the light was left on after the stack;
- Stop was missed whenever it arrived during a snap (5 of 8 tries);
- live view was stopped and restarted for every slice (24 times for 11 slices). This is the pattern behind the
  2026-11-24 mono-camera error-41 fix;
- the safe height was found only mid-stack, losing the slices taken;
- a failure threw away every finished slice;
- the range overshot when it was not a whole number of steps;
- the first slice was approached from the other direction;
- time-lapse stacks were never written to disk.

The page had no live view, focus control, progress or Stop. It showed relative numbers only and one projection.

The peers converge on the same workflow:
- set the ends from live focus (ZEN First/Last, NIS-Elements Top/Bottom, LAS X Begin/End);
- go to first / centre / last;
- an optimal (Nyquist) step;
- acquire bottom to top;
- a settle wait;
- light only while exposing (Micro-Manager);
- Abort or Finish, keeping what was taken (NIS-Elements);
- maximum, best-focus and extended-depth-of-field projections (ZEN, MetaXpress, Gen5, Fiji);
- save the planes and the projection (MetaXpress).

## Decision
1. **One engine for every path:** the Z-stack page, the Capture hot button, time-lapse, and the old
   `CaptureService.zstack()` call (kept for its callers).
2. **Plan inside the range.** First/last: whole steps up from the bottom, never past the top. Centre: outward from
   the centre, so the focus plane is a slice.
3. **Pre-flight before motion.** Steps, the 200-slice cap, channels, focus limits, and the safe height. A top above the
   safe height raises `ZStackNeedsConfirmation`. Only the moves above the safe height carry `confirmed=True`, and only
   after the operator says yes. The pre-flight also estimates time, memory and light-on time.
4. **Run:**
   - pause live view once;
   - approach the first slice from below (2 µm by default; moving down is always allowed);
   - optional settle and light off between exposures;
   - channel order channel-by-channel (default) or every channel at each Z;
   - check for Stop between every move and snap;
   - restore the light exactly, on every exit;
   - return focus after success or a refusal, never after a Stop.
5. **Keep the record.** `ZStackSet` holds a stack per channel, planned and observed Z, status, message, plan and timing.
   Partial runs are kept and shown as incomplete.
6. **Projections** never rescale values: max, min, mean, best focus, extended focus. Memory stays bounded for 200
   slices.
7. **Saving:** one folder per run, claimed with `mkdir`; every file written to a temporary name and renamed into
   place. `stack.ome.tif` has every channel, the pixel size, the Z step and each plane's observed Z. The chosen
   projections are saved beside it, plus `zstack.json`. Time-lapse with an output folder writes each stack as it is
   taken and keeps only the folder.
8. **Settings:** `AppSettings.zstack` holds mode, range, step, channels, order, options, auto-save, folder,
   projections and view. Top and bottom are not saved. Settings files from before this ADR load with defaults.

## Consequences
- A stack can no longer leave the light on, ignore Stop, lose its slices or cross the safe height without the
  operator's yes.
- Fewer live-view restarts on the USB 2.0 mono camera: one per stack.
- Slice order changes for multi-channel stacks (channel by channel by default). The old code only did one channel per
  stack.
- `CaptureService.zstack()` with `start > stop` now returns slices bottom to top (it used to follow the order given).
- Time-lapse stacks above the safe height are refused before moving. Previously they were refused mid-stack. The
  time-lapse routine has no confirmation step; set the safe height or the range accordingly.
- Pausing a time-lapse during a stack holds the light on until the stack resumes and ends (noted for a follow-up).

## On-instrument check (to do)
1. Light restored (off stays off, on stays at its level).
2. No `fc2RetrieveBuffer … 41` warning during an 11-slice stack with live view on.
3. Backlash: first-slice offset with approach 0, 2 and 10 µm on a bead slide. Keep the smallest distance that removes
   it.
4. Settle 0 / 50 / 200 ms on the 40x.
5. Safe-height question before any motion.
6. Stop within one slice, slices kept, no move after.

## Addendum, 2026-11-24: design review
A critique, a WCAG 2.2 AA check and a copy review of the page above (simulator, all six theme presets) led to these
changes. Mock-ups: the "OpenEVOS Z-stack page redesign" Design canvas.

1. **Channels** are toggle chips with the channel's display colour and a ✓ when on. The check boxes of the old list
   had a 1.05:1 border. Item views app-wide now style their check indicators like check boxes.
2. **Sampling:** Step and Slices edit each other (the range is kept). The Nyquist step is a button showing its value.
   A grey note appears when the step is 4× or more finer than Nyquist ("41 slices where 2 would do"). ZEN's "keep the
   optimal interval" lock is not copied: the step would change by itself when channels change.
3. **The drawing** carries the Go to buttons on the planes they move to ("Top 3,025.0"). The safe height is grey until
   the stack crosses it; then the region above is hatched red and the slices above it are red. While a stack runs,
   the drawing keeps the running plan and fills in.
4. **Footer:** four numbers (slices × channels, time, size, light on). Errors red, the safe-height notice amber,
   notes grey. The question box names its buttons: Run stack / Cancel.
5. **Viewer:** Live set apart from Slices, Max, Min, Mean, Best focus, Extended focus. Each slice shows as it arrives
   (`ZStackRunner.run(on_image=…)`), with a live focus curve. Projection views drop the slice slider. The focus curve
   has a Z scale and labels its best and shown slices.
6. **Keyboard and names:** every control has an accessible name; a segmented control is one Tab stop (arrow keys
   move); Ctrl+Up / Ctrl+Down nudge the focus; Alt+T / Alt+B set top and bottom.
7. **Text size:** the set-up column widens with the Windows text-size setting; painted text uses the theme's type
   scale; the projection check boxes are stacked.
8. **Layout:** channel order moved under More options; More options and Saving are one-line rows showing their
   values. The set-up fits 1440×900 without scrolling.
9. **Height-for-width:** the shell hosts each page in a resizable QScrollArea, which sizes a page with wrapped labels
   to its preferred height. After a theme switch the page stayed 792 px tall in a 760 px window and Acquire was off
   screen. The page's two columns no longer pass height-for-width up. Other pages with wrapped labels may do the same
   (not checked here).
10. **Copy:** "Z-stack" everywhere (it was "Z-Stack" in the task name and the hot-button dialog title); progress reads
    "Slice 13 of 21, DAPI, z 3,017.00 µm".

On-instrument additions: check that the chips' colours match the profile's display colours. Check that slices show
as they arrive without slowing the stack on the real camera (one render per slice).

## Addendum, 2026-11-25: redesign kit review (patch v3)
The OpenEVOS Z-Stack Redesign Kit v1.0 was reviewed against v2; the items below were adopted. Its other items are
deferred or not adopted, with reasons, in the "Redesign kit review" tab of the Z-stack doc.

1. **Pause at a slice boundary.** `ZStackRunner` honours `RunControl.pause()` at its checkpoints (between moves and
   snaps). While paused the light is off; on resume the channel's light comes back and the run continues from the
   next image, none repeated. This also covers a time-lapse paused during a stack (the consequence noted above).
   `CaptureService.pause_zstack(on)`; the page has Pause / Resume beside Stop, and time left excludes pauses.
2. **Run start time** is taken before the first move. v1 took it when the result was built, at the end, and the save
   folder was named from it.
3. **Projections:** Sum (uint32 for integer slices, float32 otherwise), Median (in bands of rows), Standard
   deviation (float32, population). `best` is labelled "Sharpest plane": it picks one whole slice. File keys are
   unchanged (`<slot>_best.tif`).
4. **Viewer:** Live | Slices | Projection, with the method in a list (eight methods). A filmstrip shows every planned
   slice; slices not taken stay empty frames. Badges carry Z.
5. **Planner:** Keep interval / Keep slices (`ZStackSettings.keep`, default interval). Keep slices remembers the
   number asked for, so centre mode's whole-step rounding on one side does not wear it down.
6. **Drawing:** travel limits as a solid neutral line, apart from the safe height (now an amber dashed line, red
   hatched above when crossed); the vessel's focus nominal as an outlined diamond.
7. **Pre-flight line:** travel, safe height, channels, storage, each with a mark and a word. With auto-save on, the
   save folder's free space must cover the stack, its projections and 512 MB, or Acquire is blocked.

## Addendum, 2026-11-25: peer features (patch v4)
The page is built around eight core capabilities: Centre and Top/Bottom planning; Z sampling with Keep interval /
Keep slices; the Z drawing (focus, nominal, safe height, travel limits); multichannel acquisition and order;
pre-flight and reliable controls (Stop, Pause); slice navigation and focus quality; intensity and focus projections;
reproducible OME-TIFF with rich metadata. 3D rendering, per-channel focus offsets, autofocus at every plane, OME-Zarr
and volumetric analytics are advanced expansions. The basic path stays Live → set range → Acquire → Slices; the items
below sit behind Projection, Sections, Details and Save, so they add nothing to it.

1. **Merged channels** (Fiji Merge Channels, ZEN's multichannel view): a "Merged" entry in the channel list.
   Fluorescence channels are added in their display colours; transmitted light and phase are a grey base at half
   brightness. Works for slices, projections and sections. Display only (`imaging/zviews.merge_rgb`).
2. **Projection range** (Fiji Z Project start / stop slice): From / To spin boxes under the image in projection
   views, All, and Sharp slices (the slices where the focus curve stays above half height). View only; Save stack
   still writes whole-stack projections.
3. **Focus quality:** the curve shades where sharpness stays above half its height and states the sharpest Z and
   that width ("sharp over ≈ 8 µm"), or says the half height is not reached on both sides. A relative score, not an
   optical depth of field.
4. **Sections** (Fiji Orthogonal Views, ZEN Ortho): XY with YZ to the right and XZ below; click a panel to move the
   cut or pick the slice. Z is drawn to scale from the step and pixel size; when that would be thinner than 1/8 of
   the image it is stretched and the badge says by how much; with no pixel size it says "Z not to scale". Slices are
   rendered on one display range per channel and downsampled to keep the volume under ~160 MB.
5. **Details:** the stack's own settings from the result (not the set-up, which may have changed): status, start,
   range, order, per-channel exposure / gain / light / bit depth, objective and NA, pixel size and whether it is
   calibrated, options, simulated.
6. **Reproducible output** (core capability 8): each OME Plane carries ExposureTime and DeltaT besides PositionZ;
   zstack.json gains a run id, the software version, the plan's SHA-256, and each channel's focus curve with its
   sharpest slice and sharp range; projection files record their method and Z range.
7. **Undersampling note:** a grey note when the step is coarser than the Nyquist step (beside the v2 note for a
   step 4× or more finer).

## Addendum, 2026-11-25: merged onto main
Patch v4 was written against 5640ec7. Main had meanwhile gained its own Z-stack work (67f013c, 27ab818, 7deb66e: a
start/stop/step page with a slice map and playback, time-lapse stacks, Z-stacks in Scan, Save and auto-save). The
merge keeps this ADR's engine and page and moves main's routine features onto them:

1. **One engine for Scan too.** `ScanRunner.stacks` runs every channel of a field's stack as one `ZStackPlan`
   (centre mode, `store=False`) sharing the scan's RunControl; the time-lapse calls the same method. A stopped
   stack's slices are kept and written, then the routine ends. `cancel_zstack` is gone (Stop is `stop_zstack`).
2. **Time-lapse output:** with an output folder each position's stack is written as a `save_zstack_set` folder
   (`<folder>/<routine>/t001_<position>[_stopped]_<time>`), a stopped one too, and only the folder is kept.
   `imaging/zstack_io.load_zstack_set` reads a folder back, so the Time-lapse page still opens a saved stack on the
   Z-stack page. Without an output folder the stacks stay in the result as before.
3. **Opening a routine's stack** (Scan field, time-lapse position) puts the whole set on the Z-stack page
   (`zstack_set_of`), the channel picked first.
4. **`ZStack`** keeps `planned` and `stop_um` (main's), and `complete` is a field (this ADR's). The routines' slice
   estimates count centred stacks as the engine plans them (`zstack_slices_centred`).
5. **Retired from main:** the old page (slice map, playback directions) and `ZStackSaveSettings`; this ADR's page,
   drawing, play button and `ZStackSettings.auto_save` / `save_folder` replace them. A settings file with the old
   `zstack: {autosave, folder}` loads into the new fields (`extra="forbid"` would otherwise refuse it at launch).
6. **Light after a capture:** `CaptureService.capture` now turns the light off after the snap whenever live view is
   dark, also when no light had been set yet this session (it used to stay on, and a stack's exact light restore then
   kept it on).

The redesign kit v1.0 needs no separate change: its adopted items are patch v3 (addendum above).
