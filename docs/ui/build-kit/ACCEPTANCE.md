# Acceptance checklist

Every item is testable. Items marked **[T]** are automated in the reference repository's test suite.
Name in brackets = the test function. The suite count at kit time: 250 passed, 6 skipped (Windows-only).

## A. Safety and honesty

- [ ] **[T]** UI and workflows never import device backends to write state. Every change is a typed
  command (`test_core_has_no_qt_dependency_and_no_sleeps`, router tests).
- [ ] **[T]** A capture is refused while the stage moves (`test_acquire_refused_while_stage_moves`).
- [ ] **[T]** The light cannot change during an exposure (`test_carriage_moves_and_is_interlocked_against_exposure`).
- [ ] **[T]** A camera setting never cuts an acquisition short. Live view pauses only for its own
  commands (`test_live_view_pauses_for_camera_settings`; calibration page test repeated three times).
- [ ] **[T]** A non-motion readback mismatch fails closed (`test_non_motion_readback_verification_fails_closed`).
- [ ] **[T]** Saved files say SIMULATED in the sidecar and the TIFF description
  (`test_save_files_preserve_raw_and_label_simulated`).
- [ ] Pills show `SIMULATED · IDLE`, never "READY". Environment shows `UNAVAILABLE`.
- [ ] **[T]** Stop all (`Esc`) cancels running commands and workflows (`test_stop_all_cancels`,
  `test_router_cancel_all`).

## B. Design system

- [ ] **[T]** All text roles ≥ 4.5:1 on bg/s1/s2 in both themes; control borders ≥ 3:1
  (`test_text_roles_meet_wcag_aa`).
- [ ] **[T]** On-accent labels are computed (`test_on_accent_label_is_computed`).
- [ ] **[T]** Theme presets never change semantic colours (`test_semantics_do_not_follow_accent_presets`).
- [ ] **[T]** Every state has text (`test_every_state_has_text`); faults use a square indicator.
- [ ] **[T]** Light / dark switch rebuilds the stylesheet (`test_theme_switch_and_narrow_rail`).
- [ ] **[T]** Below 1440 px the rail shows icons only, keeping the name in the tooltip. At ~1200 px every
  control is still reachable (`test_theme_switch_and_narrow_rail`; screenshot 22).
- [ ] **[T]** ParamRow: arrow, Shift ×10, Alt ×0.1, double-click reset; reset visible only when changed
  (`test_param_row_keyboard_and_reset`).
- [ ] **[T]** Menus grey commands instead of hiding them, with the reason; the palette lists unavailable
  commands with their reason (`test_menus_grey_not_hide_and_palette_explains`).
- [ ] Every interactive control has an accessible name and a visible 2 px focus ring.

## C. Manual parity (MAN0007986)

- [ ] **[T]** Colour camera = brightfield/phase only, in capture, UI and routines
  (`test_color_camera_refuses_fluorescence`, `test_color_camera_disables_fluorescence_channels`).
- [ ] **[T]** Seven hot-button actions; at most four per camera (`test_hot_buttons_and_light_slider`).
- [ ] **[T]** Save rules: 16-bit only TIFF + mono; JPEG/PNG/BMP/colour 8-bit; Default and Quantitative
  presets (`test_save_rule_matrix`, `test_save_page_rules_and_quick_save`).
- [ ] **[T]** Measure: ≤ 12 ROIs, per-channel statistics, line length in µm, CSV, burn-in
  (`test_annotation_rois_stats_csv_and_burn_in`, `test_measure_annotations_burn_into_saved_image`).
- [ ] **[T]** Count ≤ 6 labels with % (`test_manual_count_labels`, `test_count_percentages_and_aggressive_boundaries`).
- [ ] **[T]** Auto count: toward 100 % = more sensitive; Separate touching cells; Aggressive boundaries
  (`test_auto_count_known_disks_and_sensitivity`).
- [ ] **[T]** Scan: six coverage modes; exactly two beacons; stage-limit warning
  (`test_scan_coverage_modes`, `test_scan_beacons_limits_and_camera_rules`).
- [ ] **[T]** Time-lapse: ≤ 384 beacons; Populate wells; one-frame mode only with a single scene;
  Z-stack per time point (`test_timelapse_rules_and_estimate`, `test_timelapse_zstack_per_timepoint`).
- [ ] **[T]** Review: Reuse recalls position; Delete moves to the trash folder
  (`test_review_reuse_recalls_position`, `test_review_delete_moves_to_trash_and_movies_dual`).
- [ ] **[T]** Movies: two viewers, export (`test_review_delete_moves_to_trash_and_movies_dual`).

## D. Imaging correctness

- [ ] **[T]** Scientific mode refuses gamma; the log states linear mapping and range
  (`test_scientific_display_is_linear_and_logged`).
- [ ] **[T]** Raw 16-bit data survives saving bit-exactly (`test_save_files_preserve_raw_and_label_simulated`).
- [ ] **[T]** OME-TIFF round trip with pixel size and channel names (`test_ome_tiff_roundtrip`).
- [ ] **[T]** Autofocus finds the simulated focal plane within 4 µm at 10×; says "no sample" between wells
  (`test_autofocus_finds_the_simulated_focal_plane`, `test_autofocus_reports_no_sample_between_wells`).

## E. Calibration

- [ ] **[T]** Pixel size within 1 % of truth; parfocality within 1.5 µm; parcentration within 2 µm
  (`test_pixel_size_parfocal_parcentric_match_simulator_truth`).
- [ ] **[T]** Vessel alignment within 10 µm of the simulated placement error. Hot pixels: exact defect
  set, corrected in captures (`test_vessel_alignment_and_hot_pixels`).
- [ ] **[T]** The store persists; objective change applies Z and XY compensation; captures report
  `pixel_size_source=calibrated` (`test_calibration_store_roundtrip_and_compensation`).
- [ ] **[T]** The Calibration page runs a procedure end to end (`test_calibration_page_runs_pixel_size`).

## F. Visual review

- [ ] Render `tools/make_ui_screenshots.py` offscreen. Compare each workspace with `screenshots/`:
  - same regions and order;
  - no clipped labels at 1680 × 1000;
  - no control wider than the 860 px settings column;
  - image viewport dark in both themes.
