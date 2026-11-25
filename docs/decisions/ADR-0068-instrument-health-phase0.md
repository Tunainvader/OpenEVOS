# ADR-0068: Instrument Health, Phase 0: trustworthy status (read-only)

- **Status:** Accepted (2026-11-26) for Phase 0. Simulator and emulated drives only. Nothing has run on the instrument,
  and Phase 0 adds no hardware action of any kind.
- **Asked for by:** the operator's design kit *OpenEVOS Instrument Health & Predictive Maintenance v1.0*
  (2026-11-25; design only, no code; `roadmap/IMPLEMENTATION_ROADMAP.md`, Phase 0). The kit's mockup was treated as
  illustrative only. The page is rebuilt in this app's own conventions.
- **Amends:** nothing. The StageMonitor (ADR-0040, 0047, 0057–0061) and the SafetyManager keep their behaviour.
- **Code:** `src/openevos/health/` (`inventory.py`, `model.py`, `assess.py`, `journal.py`, `service.py`),
  `src/openevos/ui/pages/health.py`, `ui/assets/icons/health.svg`; wiring in `ui/shell.py` (the Instrument › Health
  rail entry), `ui/pages/others.py`, `ui/session_model.py` (`health_service()`) and `ui/app.py` (`start_health`).
- **Tests:** `tests/test_health_model.py`, `tests/test_health_journal.py`, `tests/test_health_service.py`,
  `tests/test_ui_health_page.py`.

## Context

The kit asks for one place that shows instrument condition, which operations remain possible, and the evidence. Its
rule is that Health observes and recommends. The StageMonitor and the SafetyManager stay authoritative, and every
hardware action still goes CommandRouter → SafetyManager → DeviceManager. AI never controls hardware.

Phase 0 is defined as follows: list the real telemetry per adapter, define the states, provenance labels and
capability map, subscribe to the EventBus, the RuntimeStateStore and the StageMonitor, persist events in SQLite, and
build a read-only overview that shows UNKNOWN and NOT MONITORED honestly. The exit criteria are that every value
traces to a source, unknown stays unknown, and monitoring cannot block acquisition.

The repository differs from the kit in three ways:

- There is no `CapabilityValidator`. `SafetyManager.validate` (`safety/command_validator.py`) is the only authority on
  commands.
- There was no SQLite and no Health page before this change.
- The kit's "Never automate homing" conflicts with ADR-0067 (automatic X/Y drive-trip recovery). The operator chose
  to keep that recovery automatic (2026-11-26), and ADR-0067 records it as a departure from the kit. This branch is
  based on ADR-0067, so Health shows that recovery as an automatic action, never as a rule breach (§5).

## Decision

### 1. Inventory (what each adapter really exposes today)

The source of truth is `health/inventory.py`: one row per signal with its unit, rate, source and provenance. A test
checks every `attr:` row against the adapters' code. A value the backend keeps only inside its own object is **NOT
MONITORED** until it is published through the store or the bus.

| Subsystem | Monitored (unit · rate · source) | NOT MONITORED (why) |
|---|---|---|
| X/Y stage (Technosoft X, Y) | position µm, APOS/CPOS counts, homed, ESTATEV, limits mm, activity · ~1 Hz status poll · RuntimeStateStore `motor.stage`; following error \|APOS−CPOS\| counts (Derived); drift-lock, warnings, fast-sample count (~20 Hz), re-approaches · StageMonitor.state(), sampled at 1 Hz | motor current IQ/IIQMAX/IIQMIN (read only when a lock is set and kept in the evidence JSON); raw ISTATUSFLAGS/SRL (the monitor latches on them; not published); landing miss and settle time (landing-log.csv); drift trend (needs baselines) |
| Z focus (Z) | position µm, APOS, homed, ESTATEV, activity · ~1 Hz | limit-switch events, landing repeatability |
| Objective turret (O) | position sensor ICURRENTPOSHF · ~1 Hz | switch time (only as a command's duration) |
| Cube carriage F, phase wheel P | position counts, F homed · ~1 Hz | cube identity (the LED board's sensor, shown under Illumination) |
| Mono camera (FlyCapture2) | connection, activity, exposure ms, gain dB, model/firmware · on change | frame rate, incomplete frames, latency, reconnect count (counted inside the backend, not published); sensor temperature |
| Colour camera (uEye) | connection, activity, exposure ms, gain dB, model · on change | frame rate, skipped grabs, latency |
| Illumination (LED board) | connection, firmware mode, channel, light on (read back), intensity %, cube in path · on change | optical output (no photometer); LED temperature, current, on-time |
| Incubator (AMC1000) | none | temperature, CO2, O2, alarms: no control or telemetry path (bring-up record) |
| Communications | per-link connection (Technosoft serial, camera USB, LED HID); link errors from backend reasons; refusals; failures and time-outs; command duration (Derived) | serial frame time-outs and retries inside DriveLink |
| Storage & data | free space GB on the data and journal drives (every 10 s, `shutil.disk_usage`); StorageFaultEvent; frames failed (AcquisitionStatusEvent); preview frames dropped (display only) | write latency, writer queue depth, checksums |
| Host & application | memory % (GlobalMemoryStatusEx), threads, EventBus.delivery_errors, RuntimeStateStore.stale_observations, the journal's own queue and drops · 1 Hz | CPU, GPU, UI latency, worker restarts |

The Technosoft poll rate is `TelemetryService.poll_interval_s` (1.0 s in the app). Cameras and the LED board report on
change only, so age is shown for them but they never go stale.

### 2. Health model (`health/model.py`, `health/assess.py`)

- **Six states:** HEALTHY, WATCH, RESTRICTED, FAULT, UNKNOWN, OFFLINE. The overall state is the worst *monitored*
  subsystem, ranked FAULT > RESTRICTED > OFFLINE > WATCH > UNKNOWN > HEALTHY. UNKNOWN ranks above HEALTHY, and no
  evidence counts as UNKNOWN.
- **Device mapping:**
  - not registered or disconnected → OFFLINE;
  - bootloader or recovery firmware → FAULT (quarantined);
  - recovery required or a device FAULT → FAULT;
  - a closed-loop axis not homed → RESTRICTED;
  - a backend WARNING → WATCH;
  - connecting, or health not reported yet → UNKNOWN;
  - otherwise HEALTHY.
- **Stale telemetry:** a periodic source (the Technosoft poll) with no observation for
  max(10 s, 5 × its period) → UNKNOWN, whatever it said last. The page shows "Last known: …".
- **StageMonitor (authoritative; never goes stale):**
  - a drift or landing lock (`RELEASABLE_RULES`) → RESTRICTED;
  - any other latch (over-current, oscillation, communication abort, power stage off, unreadable latch file) → FAULT;
  - its warnings → WATCH.
- **NOT MONITORED:** a subsystem with no source in this session reads NOT MONITORED. It is never green and is left out
  of the overall state.
- **Provenance** on every value and finding:
  - Measured: from the instrument or the host;
  - Derived: computed, or the SafetyManager's decision;
  - Inferred: not produced in Phase 0;
  - Unavailable;
  - Demo: anything from a simulated backend (never shown as Measured).
- **Physical health and operating capability are separate.** Nine capabilities (move the X/Y stage, multi-position
  runs, focus/Z-stacks, objective change, mono imaging, fluorescence, colour imaging, saving data, incubator control)
  each name the subsystems they depend on. Each subsystem card lists the capabilities it affects (the inverse map).
  A capability is decided in this order:
  1. **RESTRICTED** only if the SafetyManager refuses its probe command. Its reasons are quoted verbatim, and the
     authority is "SafetyManager".
  2. Otherwise it follows what it depends on:
     - OFFLINE → UNAVAILABLE;
     - FAULT or RESTRICTED → AT RISK (Health's advice; the SafetyManager would accept it);
     - UNKNOWN → UNKNOWN;
     - WATCH → WATCH;
     - HEALTHY → PERMITTED.
- **Probe commands.** Each probe is a move to where the device already is, its current exposure, or a light ON. It is
  passed to `SafetyManager.validate`, a pure function, and is **never submitted**: it reaches no router, worker,
  backend or drive (a test checks the emulated drive chain). Only the state checks are kept: device, kind,
  capability, device_state, recovery, firmware and provenance. Target checks (limits, confirmation, interlocks) do not
  apply to a probe.
- **Thresholds.** Phase 0 rules are provisional, not qualified baselines. Free space below 10 GB → WATCH, below 2 GB
  → RESTRICTED (saving may fail). Memory ≥ 90 % → WATCH. Link errors or command time-outs in the last 10 min →
  WATCH.

### 3. Recorder and journal (`health/service.py`, `health/journal.py`)

- **Subscriptions.** `HealthService` subscribes to DeviceStateChanged, CommandLifecycleEvent, HardwareFaultEvent,
  StorageFaultEvent and AcquisitionStatusEvent. The callbacks run on the publisher's thread: device workers, and the
  RuntimeStateStore with its lock held. Each one does O(1) work and catches its own errors. Only condition changes are
  journaled; position-only updates stay in the existing telemetry log.
- **Sampler.** A 1 Hz sampler thread reads `StageMonitor.state()` and `.refusal()` and journals latch, clear,
  evidence and warning transitions. The clear note is the monitor's own "latch cleared: …" text. The sampler also runs
  the probes, reads disk and memory, runs `assess`, journals subsystem state changes, and swaps in `latest`. The page
  only reads `latest`, so the UI never waits on a lock, a disk or a device.
- **Journal.** `HealthJournal` has a bounded queue (4096) with `put_nowait`. When the queue is full the event is
  counted as dropped, and a `journal.overflow` row records the gap once the writer catches up. One writer thread
  batches inserts into `<config>/Logs/health-journal.sqlite3` (WAL, 180-day retention). The columns follow the kit's
  `health_event` schema: event_id, observed_at (source time), received_at, subsystem, kind, severity, device,
  message, provenance, source, session, data.
- **Failure.** Open failure, disk full and I/O errors put the journal in state `failed` alone: offers keep returning
  at once and are counted. An assessment failure shows UNKNOWN with the error, never the last green. `start()` and
  `stop()` never raise, and `start_health` falls back to memory-only. Nothing in `health/` is on a command or safety
  path, so its failure cannot change what the SafetyManager decides. A test compares the decisions with Health broken.

### 4. Page (`ui/pages/health.py`, Instrument › Health)

- **Overview:** overall state, its reasons, freshness, counts per state and the journal's state.
- **Operating capability**, apart from health, each with who decided.
- **"Why is operation restricted?"** quotes the StageMonitor's lock once: rule, axis, since, refusal, releasable or
  not, recovery, evidence and carried-over flag. It also gives the SafetyManager's refusal and the last refused
  command, then lists what is still permitted.
- **One card per subsystem:** state chip, provenance (DEMO on the simulator), freshness ("expected every …", or
  STALE), findings, metrics (value, unit, provenance, age), source, affected capabilities and the NOT MONITORED list.
- **Recent events.**
- States are shown as glyph + text + colour, never colour alone. The page has no buttons: acknowledgement, dismissal
  and lock release are different things, and Health does none of them. Releasing or re-homing stays on Microscope ›
  Release stage lock or Re-home stage.

### 5. ADR-0067 automatic recovery as an event source

Health reads only what the stage service exposes, and changes nothing about the recovery:

- **Will it run?** For the current lock, the "Why is operation restricted?" panel shows whether the recovery is
  switched off (Settings › Interface), allowed (the app then starts it by itself), or not attempted, with the
  service's own reason from `TelemetryService.auto_recovery_blocker()`. Examples: "already had its automatic attempt
  this session", "this trip is never retried automatically (ran_back …)".
- **Steps.** Each "automatic recovery: …" note the stage service writes to the StageMonitor's log is journaled as
  `stage_recovery.step`. Notes are read by position, not by time: the monitor's clock ticks at about 16 ms, so many
  notes share a timestamp. While a recovery runs, the X/Y card shows WATCH ("automatic recovery running").
- **Records.** Each new or rewritten `stage-recovery-*.json` in the stage evidence folder (real drives only) is
  journaled as `stage_recovery.record`: stage, ok, message, trip class, lock and path. Records from before Health
  started only set the "last automatic recovery" line; they are not journaled again.
- **Labelling.** Every such event says "Automatic action (ADR-0067)", carries `automatic: true` and is info or
  warning, never error or critical: a recovery that ran is the operator's chosen behaviour, not a breach. An
  escalated recovery leaves the stage locked, and the `auto_recovery_failed` latch maps to FAULT like any other
  drive-fault latch.
- **X/Y card metrics:** attempts this session per axis (`auto_recovery_attempts`) and the last recovery record.

## Consequences

- An operator can see why X/Y is refused and that stationary imaging is still permitted, in the monitor's and the
  SafetyManager's own words.
- The journal gives Phase 1 a record to build baselines from: condition changes, refusals, failures, command
  durations, latches and clears.
- Every Technosoft poll now also passes through one more bus subscriber. It does O(1) work, and the publisher-load
  test (20,000 events, journal stalled) shows no blocking.

## Not done (later phases or physical gates)

- Publish the counters the backends keep internally: camera frame, incomplete-frame and reconnect counts; drive
  current; landing log. Until then they stay NOT MONITORED.
- Qualified, versioned baselines and deterministic rules (Phase 1). Capability-scoped protection through the
  SafetyManager (Phase 2). Analytics and an assistant (Phases 3–4).
- Physical qualification on the AMAFD1000: signal units, rates and timestamps on the real drives; the page against a
  real lock and a real recovery record. This needs the operator's approval and is not part of this change.
