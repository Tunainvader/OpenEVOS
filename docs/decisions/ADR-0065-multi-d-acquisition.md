# ADR-0065: Multi-D acquisition (Z × C × T × P)

- **Status:** Accepted (2026-11-25). Implemented and tested on the simulator only. **Physical positions (P) are
  locked** on the instrument until the XY stage is qualified (gate G-XY below). Z × C and Z × C × T at one position
  have not yet been run on the instrument either: the on-instrument check below is still to do.
- **Asked for by:** the operator, 2026-11-25, with the OpenEVOS Multi-Dimensional Acquisition Kit v1.0: "read
  instructions carefully lets build a multi dimensional scan system".
- **Code:**
  - `acquisition/mda.py` (new, Qt-free): the frozen, versioned, serialisable plan (`MultiDimPlan` and its axis
    plans), validation, the lazy compiler (`compile_jobs`, `compile_events`, stable event IDs), the summary
    (`summarize`) and the experiment-level pre-flight (`preflight`);
  - `acquisition/mda_runner.py` (new, Qt-free): `MultiDimRunner`, the scheduler and state machine;
  - `imaging/mda_store.py` (new): `ExperimentWriter` (streaming, one volume at a time, atomic commits, journal,
    manifest) and `ExperimentReader`;
  - `imaging/zstack_io.py`: `write_zstack_folder` split out of `save_zstack_set` so a Multi-D volume is written by
    the same Z-stack writer;
  - `ui/pages/multid.py` (new page: Plan, Acquire, Review), `ui/pages/others.py`, `ui/shell.py` (rail entry
    "Multi-D" under Acquire), `ui/assets/icons/multid.svg`;
  - `instrument/settings.py` (`MultiDSettings`: save folder, last plan).
- **Tests:** `tests/test_mda.py` (36), named after the kit's acceptance IDs (MDA-P001…P010, T001…T006, S001, S004,
  S007, A003, A004, D001…D010, U001, U003) where one applies.

## Context
The Z-stack page (ADR-0064) takes one Z × C volume at one place. The kit asks for an experiment-level planner above
it: Z, channels, time and positions in one plan, run reliably and saved so the data can be trusted and read without
OpenEVOS. It freezes a set of decisions: product name "Multi-D Acquisition"; dimensions Z, C, T, P with P a list of
XY positions, never an image axis; keep `ZStackRunner` as the volume executor; T → P → C → Z by default, T → P → Z →
C as the alternative, no other permutation; a minimum start-to-start interval by default and a fixed schedule with
skip / run-late-once, never a catch-up burst; pause at a safe boundary with the light off; Stop & keep data never
returns to the start; streaming, bounded-memory writing with atomic volume commits; and no physical P until the XY
stage is qualified, because of the cross-axis feedback issue (simulation is not qualification).

## Decision
**One event model, one state machine, one writer.**

1. **Plan.** `MultiDimPlan` is a frozen dataclass with a schema version and a random plan id. It serialises to JSON
   and back exactly; another schema version is refused, not guessed. `content_hash()` covers what the plan does
   (not its id, creation time or name), so two runs of the same plan carry the same hash. Absolute focus is never
   stored in the Z axis: planes are offsets from a focus *reference*, global or per position, set explicitly
   ("Use focus now", "Set focus here").
2. **Compiler.** `compile_jobs` yields one `VolumeJob` per (T, P), T outer; `compile_events` expands each into C × Z
   or Z × C events. Both are generators: a 100 000-event plan is never held in memory. Event ids are
   `T0001P0001C01Z0001`, the same every time the same plan is compiled.
3. **Volume execution.** Each job becomes a `ZStackPlan` (store off, return to start on) and runs through
   `capture.run_zstack` with the run's shared `RunControl`. Every Z-stack safety property of ADR-0064 therefore
   holds per volume: verified moves, focus limits, safe height, light restored, Stop between slices.
4. **Scheduler.** Minimum interval: a timepoint starts no earlier than the interval after the previous one started;
   if a cycle runs long the next starts at once and is marked late; a pause shifts the following start. Fixed:
   targets are offsets from the experiment start and do not move for a pause; a missed target is skipped, or with
   run-late-once runs once late. If the next target has also passed it is skipped. There is never a catch-up burst.
   The clock is injectable, so the policies are tested without waiting.
5. **Pause, stop, failure.** Pause takes effect between slices (inside the Z-stack runner, light off) or between
   volumes. Before the next volume after a pause the focus read-back is checked again. Stop & keep data writes the
   volume in progress as a partial volume, keeps every volume written, marks the rest "not acquired" and does not
   move back. A full disk, a write error or any other failure ends the run "partial" (images written) or "failed"
   (none), with the reason in the manifest. Stop all (top bar) reaches the runner through `register_stopper`.
6. **Storage.** `Experiment_<time>_<plan id>/` with `experiment.json`, `plan.json` (+ SHA-256), `manifest.json`,
   `events.jsonl` and `Positions/P0001_<label>/T000001/` holding the volume as the Z-stack writer makes it
   (`stack.ome.tif` ZCYX with per-plane PositionZ, ExposureTime and DeltaT; projections; `zstack.json` with the
   T/P indices, stage coordinates and event ids). Each volume is written to a hidden temporary folder and renamed
   when complete. Free space is checked before every volume against the volume plus a 512 MB reserve. The journal
   has one line per image: event id, indices, planned and observed XY and Z, channel settings, time and a CRC-32 of
   the pixels. Simulated runs say so in `experiment.json`, in every volume record and on every journal line.
7. **Gate G-XY.** `PHYSICAL_POSITIONS_QUALIFIED = False`. On the instrument (or any session with the physical stage
   enabled) pre-flight refuses a plan with any XY move, and the page says why. The rest (Z × C × T at the current
   position) stays available. The simulator runs positions so the plan, writer and review can be exercised.
   Autofocus at every position and per-channel Z offsets are not offered: the kit gates them until validated.
8. **Page.** One page, three steps. Plan: a card per dimension with its own switch, presets for the common shapes,
   plain-language order choice, positions table (current position, every well of the vessel, per-position focus),
   and on the right the exact counts, estimated size and time (saying where each comes from), pre-flight grouped
   into "fix", "confirm" and notes, and the first events in run order. Acquire: per-axis progress, the newest image,
   timepoint timeline (done, late, skipped, stopped, with words as well as colour), event log, writer status, Pause
   and Stop and keep data. Review: any experiment folder, step T / P / C / Z, projections over a Z subrange,
   metadata, and every volume not acquired listed.

## Consequences
- Z-stack and Multi-D share the volume executor and writer; a fix to one is a fix to both.
- A time-lapse of Z-stacks now exists in two places (Time-lapse page beacons with stacks, and Multi-D T × Z). The
  Time-lapse page keeps its beacons and autofocus; Multi-D is the reproducible, journalled path. Merging them is a
  later decision.
- Per-image journalling with fsync costs a little time per image; worth it for a run that can be rebuilt after a
  crash.
- Multi-D reads exposure, gain and light from the Capture page's channel settings; there is no per-run override yet.

## Not done (later)
- Position-major (P → T) order; arbitrary permutations (kit: deferred).
- OME-Zarr for large volumes; 3D rendering; volumetric analysis.
- Resume of a stopped experiment into the same folder (the journal and manifest have what it needs).
- A writer thread with a bounded queue: writing now happens between volumes on the run thread, which bounds memory
  to one volume and never drops a frame, at the cost of the write time between volumes.

## On-instrument check (to do)
1. Z × C at the current position, 2 channels, 11 planes: compare `stack.ome.tif` with the Z-stack page's own save.
2. Z × C × T, 3 timepoints at 1 min, minimum interval: timepoint records and lateness as expected; light off
   between timepoints.
3. Pause during a volume and between timepoints; Stop and keep data mid-volume: partial manifest, no return move.
4. Confirm pre-flight refuses a plan with two positions while G-XY is closed.

## Addendum (2026-11-25): merged onto main, page laid out after the kit's mockup
The patch was applied to main on top of the merged Z-stack engine (ADR-0064); its 36 tests pass unchanged there. The
operator asked for the page to follow the kit's mockups (`design/mockups/08_multidimensional_plan_acquire_review_concept`
and the contact sheet), so the page was laid out again without changing what it does:
- **Header:** the Plan / Acquire / Review steps and four dimension chips (Z depth, C channels, T time, P positions)
  in their colours (Z blue, C green, T violet, P amber) with their counts; an off dimension reads "off". On a narrow
  window a chip shrinks to its count.
- **Plan, three columns:** Experiment (name, preset, vessel / objective / camera, an XY position map of the vessel's
  wells, where a click adds or removes a position at that well's centre, and the positions table); Dimension plan
  (Z with a ruler of the planned planes and the focus reference, C with the channel order, T with a timepoint strip,
  P with the focus policy and the XY gate); Plan summary (counts, total images, duration, per timepoint, storage, Z
  travel, light on), the pre-flight checklist with marks and words, the destination folder, and Save / Load / Open /
  Acquire experiment, which stay in view while the summary scrolls. Below 1400 px wide the side columns narrow first.
- **Acquire:** tiles for experiment, position, timepoint, channel and Z slice; the newest image; per-axis progress
  bars in the dimension colours; a queue of this timepoint's positions (done / acquiring / paused / queued); a status
  card (images, time left, written, throughput) with the overall bar, Pause and Stop and keep data; the timepoint
  timeline, a position map with the current well ringed, the event log and the writer.
- **Review:** a dataset tree (positions, then each timepoint's volume with its images and status; a click shows it),
  the viewer with position / timepoint / channel / projection and Z range, metadata as a label / value table, the
  volumes not acquired, and Open raw data / Experiment manifest.
Orthogonal XZ / YZ sections and a thumbnail gallery in Review, shown in the mockup, are still later (kit doc 06 §4
calls them a later phase). Tests: `tests/test_mda.py` gains the map, chips, queue, axis bars and dataset tree.
