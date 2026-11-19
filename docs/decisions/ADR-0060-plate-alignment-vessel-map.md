# ADR-0060: One saved plate alignment makes the vessel map work on the real stage

- **Status:** Accepted, implemented (2026-11-20). Tested on the simulated drive chain; **not yet used on the instrument.**
- **Amends:** ADR-0029 ("vessel-map moves stay disabled for the physical stage").
- **Operator, 2026-11-20:** "i want the vessel map simulator functional"; on how: "since these plates are standard can
  we do it once and save automatically".

## Decision
- **One alignment for every standard-footprint vessel.** The vessel ↔ stage map on the physical stage is
  `stage µm = (vessel mm − footprint centre) × 1000 × sign + offset`, per axis. It has an axis direction (`sign_x`,
  `sign_y`, ±1) and an offset (the stage position of the footprint's centre). Every plate and holder in the vessel
  library is drawn on the same SBS footprint, so the one map serves them all; choosing another vessel needs nothing.
- **How it is measured:** on the Capture page, *Align plate…* asks the operator to jog until the first well (A1 on a
  96-well plate) is centred in the live image and press *Record A1*, then the same for the last well (H12). The two
  stage positions give both axis directions and the offset (`solve_plate_alignment`).
- **What refuses it:** the two wells must be at least 20 mm apart on each axis. The stage distance between them must
  match the plate's within 3 % on each axis; otherwise it is a wrong well, a slipped plate or a swapped axis. When
  refused, nothing is saved and the previous alignment (or none) stays.
- **Where it is kept:** `plate_alignment` in the app's `calibration.json` (next to the settings, user profile, not in
  git), with the wells, the stage positions, the residual and the time. It is applied at every launch. This is an
  app-side calibration: nothing is written to a drive.
- **What it enables:** the map shows the live stage position, the current well and the travel box. A click on a well
  (or a crosshair drop) moves the stage there through the router and the stage service: X, then Y, never together,
  with every pre-move, landing and lock check. A target outside the travel box is refused before anything is sent.
- **Without an alignment** the map shows no position and moves nothing, as before, and says to run *Align plate…*.

## Consequences
- A plate seated differently in the holder (or a different holder) shifts every position by the same amount. If the
  centred well is off, re-run *Align plate…*; there is no per-plate correction yet.
- Moves to Y ≈ 0 (rows near the home edge, depending on the measured direction) remain subject to the Y home-edge
  findings of 2026-11-19 (ADR-0058 amendment).
- The simulator keeps its own fixed map and its image-based vessel alignment (Calibration centre); neither touches
  the physical map.
