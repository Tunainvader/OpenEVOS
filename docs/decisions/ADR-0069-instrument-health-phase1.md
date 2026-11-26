# ADR-0069: Instrument Health, Phase 1: rules, baselines and evidence

- **Status:** Accepted (2026-11-26) for Phase 1 on the simulator and the emulated drives. Nothing has run on the
  instrument. Phase 1 adds no hardware action: no command, no lock, no release, no new automatic action.
- **Asked for by:** the operator's design kit *OpenEVOS Instrument Health & Predictive Maintenance v1.0*
  (`roadmap/IMPLEMENTATION_ROADMAP.md`, Phase 1: "Qualified versioned baselines; deterministic rules for available
  camera, communication, storage and stage signals; event correlation and diagnostic export; simulator injection.
  Exit: repeatable detection and bounded false alarms."). The operator asked to continue after Phase 0 (ADR-0068).
- **Builds on:** ADR-0068 (Phase 0). The StageMonitor (ADR-0040, 0047, 0057–0061, 0067) and the SafetyManager keep
  their authority and their behaviour.
- **Code:** `src/openevos/health/` — new `stats.py` (robust statistics, versioned baselines), `rules.py` (metric
  series, rule engine, eight rules), `correlate.py` (incidents), `export.py` (diagnostic bundle), `inject.py`
  (simulator fault injection); changed `model.py` (`Assessment`, `DetectorStatus`), `assess.py` (a rule's WATCH
  reaches its subsystem and the capabilities), `inventory.py` (camera and LED-board counters now MONITORED),
  `service.py` (series, rules, baselines, fingerprint). `ui/pages/health.py`: the "Rules and incidents" panel and the
  Export diagnostics button. `backends/rig/camera.py`: the simulated camera can simulate incomplete frames.
- **Tests:** `tests/test_health_rules.py` (pure), `tests/test_health_phase1_sim.py` (injection on the simulator),
  `tests/test_ui_health_page.py` (the page's rule WATCH, incident and export).

## Context

Phase 0 shows what each source says now. Phase 1 asks whether a signal is drifting, repeating or filling up before the
existing authorities act on it, and keeps the evidence. The kit sets three limits on that:

- An anomaly is not a fault, and a detector returning to normal releases nothing (`EVENT_AND_FAULT_LIFECYCLE.md`).
- Baselines must be qualified, versioned and stratified by regime, and invalidated after a hardware or calibration
  change (`MONITORING_CATALOG.md`). Qualification happens on the AMAFD1000 itself (the physical gates).
- Faults are injected in the simulator, never by sending guessed commands to the instrument (`TEST_STRATEGY.md`).

What the instrument exposes decides which rules are possible. Phase 0's inventory listed camera frame counters and
reconnect counts as NOT MONITORED although the backends already count them: `frames_produced` (all cameras),
`incomplete_frames` (FlyCapture2 mono), `skipped_grabs` (uEye colour) and `reconnects` (both cameras and the LED board,
ADR-0066). Motor current, drive flags, landing settle time and incubator readings are still not exposed.

## Decision

### 1. Rules say WATCH, nothing more

Eight deterministic rules (`rules.py`), each with a name and a version, run once per sample on the sampler thread:

| Rule | Reads | Limit (v1) |
|---|---|---|
| `comm.error_burst` | `command.failed` / `command.timed_out` events | 3 to one device within 60 s |
| `comm.link_errors` | link errors per link (Phase 0) | 3 within 10 min |
| `comm.reconnects` | backend `reconnects` counters | 2 within 30 min |
| `camera.incomplete_frames` | `frames_produced` + `incomplete_frames` / `skipped_grabs` | ≥ 5 % of ≥ 20 frames in 60 s, 3 samples running |
| `stage.following_error` | \|APOS − CPOS\| at rest, per axis, against its baseline | rolling 120 s median above the baseline p95 and ≥ 1 count above its median, 5 samples running |
| `stage.warning_recurrence` | `stage_monitor.warning` events | 2 within 30 min |
| `stage.landing_reapproach` | the StageMonitor's re-approach list (ADR-0061) | 3 within 30 min, per axis |
| `storage.time_to_full` | free space on the data and journal drives | least-squares rate over 10 min (≥ 5 readings, ≥ 2 min) projects full within 2 h, 3 samples running |

A rule raises after `raise_after` consecutive evaluations with a hit and resolves after `clear_after_s` without one,
so one noisy reading neither raises nor clears it. An open assessment adds a WATCH finding to its subsystem (a worse
state stays), and so the capabilities that depend on it read WATCH. Rules never say RESTRICTED or FAULT and never
touch a lock: protection is Phase 2, through the SafetyManager. A rule that raises an exception is marked
"error: …" and the others go on; if the whole engine fails, the Phase 0 assessment is shown without rules.

Each assessment follows the kit's `fault_assessment` schema: detector name and version, the baseline it used (id,
version, regime, "not-qualified" when it is), the limit in words, evidence event ids, affected capabilities,
hypotheses (always `confirmed: false`) and recommended operator checks. Provenance is Inferred, or Demo when the
evidence came from the simulator. Raising and resolving are journaled (`assessment.raised`, `assessment.resolved`)
with the full assessment.

Limits without a baseline are labelled "provisional, not qualified". They were chosen from the simulator and the
instrument logs already in the repository, not qualified on the instrument.

`stage.following_error` deliberately watches a trend below the StageMonitor's own limits. The monitor warns once a
reading at rest is more than 3 counts from its command and locks on encoder drift. The rule looks at the rolling
median creeping above its baseline: the kit's "gradual XY drift" example, as an early WATCH.

### 2. Baselines: versioned, fingerprinted, not qualified

`stats.py` builds a baseline from at least 30 samples of one metric in one regime (today: `idle`). It stores the
median, the MAD, p05 and p95, the source (`simulator` or `instrument`), and a fingerprint of the subsystem's devices:
backend kind, link (the motor port) and identifying attributes (model, firmware, serial, drive limits). Baselines live
in `<config>/Logs/health-baselines.json`. A new one for the same metric and regime is the next version, and earlier
versions stay. A baseline whose fingerprint no longer matches the session is invalid. Its rule then says
"inactive: baseline vN invalid: … changed since it was made" instead of comparing against the wrong instrument. A
simulator baseline is never valid on the instrument, because the backend kind differs.

Every baseline is "not-qualified". `BaselineStore.qualify` exists, refuses a simulator baseline, and has no caller in
the app. Qualification is the physical gate: it happens on the instrument, with the operator. Phase 1 also adds no
"record baseline" button. `HealthService.capture_baseline` is there for that gate and for tests.

### 3. Evidence: correlation and the diagnostic bundle

`correlate.py` groups events into incidents. A warning-or-worse event opens one. Later events join while they come
within 30 s of the incident's last event and are related: the same subsystem or device, or a known pair (a link and
the devices on it, X/Y with focus and turret, storage with the cameras and the host). Info events just before are
attached as context. Bookkeeping events never count. Incident ids are hashes of their events, so the grouping is
deterministic. The page and the bundle say plainly that an incident groups events; it does not prove a cause.

`export.py` writes `health-diagnostics-<UTC>.zip` next to the journal (`<config>/Logs/diagnostics/`). It contains the
manifest (version, platform, simulated or not, the fingerprint, journal stats), the latest report, open assessments
and their history, the detectors' status, incidents, the window's events (24 h, at most 50,000), baselines, the
inventory with every NOT MONITORED reason, and the recorded series. It reads the journal through its own connection
and runs on a worker thread from the page's one button, so the UI does not wait. It sends nothing anywhere.

### 4. Simulator injection

`inject.SimulatorFaultInjector` turns knobs the simulator already has: the emulated drive chain's registers and test
hooks (offset at rest, landing off target, drive trip, stalled moves), the simulated camera's incomplete frames (a new
sim-only counter that mirrors FlyCapture2's skip), a simulated device's fail-next hook, and Health's own disk reading.
It refuses, before touching anything, any session with a physical device or a motor port that is not the emulated
chain (`NotSimulated`). Each injection is journaled as `simulation.injected` (Demo), so a bundle shows what was
injected next to what the rules saw.

### 5. Counters read, never called

The sampler reads the backends' integer counters with `getattr` (no method call, no lock taken) and publishes them on
the camera and LED-board cards. In the inventory they move from NOT MONITORED to MONITORED, and a test ties each
`backend:<name>` to a counter a backend keeps. Frame latency stays NOT MONITORED, because no adapter times it.

## Consequences

- Exit criterion "repeatable detection": the simulator tests inject each fault the rules can see on the simulator
  (offset at rest below the monitor's limit, incomplete frames, a filling drive, a burst of failed commands). They
  assert detection, and the offset case is injected, detected, cleared and detected again.
- Exit criterion "bounded false alarms": a clean simulator session with X and Y baselines, four stage moves and
  settling raises no WATCH from any rule, and the engine's persistence and hold are unit-tested. This bounds false
  alarms on the simulator only. The bound on the instrument needs the physical gate below.
- The rules command nothing. A test checks that sampling, the rules and the injector send no frame to the emulated
  drives, and that the page's export does not either.
- The simulated camera gains `incomplete_frames` and `inject_incomplete_every` (default 0: unchanged behaviour).

## Not done (later phases or the physical gate)

- Qualifying baselines on the AMAFD1000, and the instrument-side false-alarm rate. Both need the operator's approval
  in chat and time on the instrument.
- Any protection (Phase 2), streaming analytics (Phase 3), the diagnostic assistant (Phase 4).
- Rules for signals no adapter exposes yet: motor current, drive flags, landing settle time, frame latency,
  incubator readings, turret switch time.
