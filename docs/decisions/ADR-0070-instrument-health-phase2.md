# ADR-0070: Instrument Health, Phase 2: capability-scoped protection, "ask, then restrict"

- **Status:** Accepted (2026-11-26) for Phase 2 on the simulator and the emulated drives. Nothing has run on the
  instrument. Phase 2 adds no device command and no new automatic hardware action. A restriction refuses commands
  and runs; it never moves, homes, resets or switches anything.
- **Asked for by:** the operator's design kit *OpenEVOS Instrument Health & Predictive Maintenance v1.0*
  (`roadmap/IMPLEMENTATION_ROADMAP.md`, Phase 2): "Map faults to affected capabilities; policy requests through
  SafetyManager; audit history; validate safe-boundary stop and writer flush; operator acknowledgement/service
  escalation. Exit: no safety bypass, no scientific frame loss, release requires verification." The operator asked to
  continue after Phase 1 (ADR-0069). They chose **"Ask, then restrict"** over "automatic, small set" and "no
  enforcement yet" (2026-11-26): Health proposes; the operator confirms; release runs an automatic check.
- **Builds on:** ADR-0068 (Phase 0), ADR-0069 (Phase 1). The StageMonitor (ADR-0040, 0047, 0057–0061, 0067) keeps its
  locks and its automatic recovery unchanged.
- **Code:**
  - New `src/openevos/safety/restrictions.py`: the registry, its interlock and the run guard.
  - New `src/openevos/health/protection.py`: policies, proposals and verifications.
  - `safety/command_validator.py`: the SafetyManager owns the registry and adds its interlock.
  - Runners: `acquisition/scan_runner.py`, `timelapse.py`, `zstack.py` and `mda_runner.py` each run inside the guard.
  - `acquisition/capture.py`: a single capture checks the registry; new `run_capabilities`.
  - `health/service.py`: proposals, confirm, decline, lift, stop runs, acknowledge, verification and the journal.
  - `health/assess.py`: a confirmed restriction reads RESTRICTED, by the SafetyManager.
  - `health/export.py`: `restrictions.json` in the bundle.
  - `ui/pages/health.py`: the Protection panel.
- **Tests:**
  - `tests/test_health_phase2.py`: the registry, the interlock, and end to end on the simulator.
  - `tests/test_ui_health_page.py`: the Protection panel.

## Context

Phase 1's rules say WATCH and nothing more. The kit's Phase 2 lets a WATCH become a restriction, capability-scoped,
through the SafetyManager, never as a second safety controller (`SAFETY_AND_RECOVERY.md`: "Health observes and
recommends"). The operator has repeatedly asked for protection that is proportionate. A lock that costs a session is
worse than the fault it guards against (see ADR-0057, 0059, 0061, 0067). So the question was how far Health may go
by itself. The operator's answer: nothing by itself. Health proposes, the operator confirms, and release is
automatic once a check passes.

The code had two facts to work with:

- **Commands do not say which run they belong to.** Runs submit their moves through the same controller as the UI.
  So an interlock cannot tell a scan's stage move from a manual one. Restricting "multi-position runs" therefore has
  to happen where a run starts, not per command.
- **Every run already has a safe Stop.** `RunControl.stop` ends a scan, time-lapse, Multi-D or Z-stack at its next
  checkpoint: the current move or image finishes and everything taken is written. A stopped dataset is kept and
  named as stopped. The kit's "safe-boundary stop and writer flush" is this existing Stop. Phase 2 reuses it and tests
  it.

## Decision

### 1. The registry belongs to the SafetyManager

`SafetyManager.restrictions` is a `RestrictionRegistry`. It is the only place a Health restriction takes effect, and
it has two doors:

- **Commands.** `HealthRestrictionInterlock` (`interlock:health`) refuses the commands a restriction names (device id
  + command kind) in the SafetyManager's normal interlock pass. Interlocks never run for a safe-direction command, so
  a restriction cannot stop a light going off or live view stopping.
- **Runs and captures.** Scan, time-lapse, Multi-D and Z-stack runs enter `run_guard` with the capabilities they need:
  `multi_position` (more than one position), `data_saving`, `focus_z`, and `imaging:<camera id>`. A restricted
  capability refuses the run with `RestrictionRefused` before anything moves. `CaptureService.check_capture` refuses
  a single capture on a restricted camera.

A restriction stores the kit's lock record:

- the policy and its version, and what is restricted, in words;
- the trigger: the rule, its version, the key, the assessment id, the evidence event ids, the limit, the baseline,
  and the provenance;
- the capabilities and commands it covers;
- the release conditions, and who confirmed it;
- its state (pending, active or released), with times;
- who released it, the verification that passed, and an append-only history.

Restrictions are kept in `<config>/Logs/health-restrictions.json`, so an app restart does not release them. A pending
one becomes active at the restart, because no run survives one.

### 2. Never cut a run short by itself

A restriction confirmed while a run needs what it covers is **pending**. It takes effect when that run ends. The
confirm dialog offers two choices: let the run finish first, or stop it at its next safe point. Stopping uses the
run's own Stop, so the current move or image finishes and everything taken is kept. A pending restriction keeps the
"Stop the run now…" button on the panel. This is the kit's "no scientific frame loss": no frame already taken is
lost, and no image is cut mid-exposure.

### 3. Three narrow policies

Each policy restricts one capability, says what stays available, and has a verification Health can run **without
moving the instrument**:

| Policy (v1) | From rule | Restricts | Stays available | Verification (automatic release) |
|---|---|---|---|---|
| `xy.multi_position.following_error` | `stage.following_error` | multi-position runs | stationary imaging, single-position Z-stacks, manual moves | the axis offset at rest is back within its baseline: rolling median at or below p95, ≥ 30 readings over 5 min after activation |
| `camera.capture.incomplete_frames` | `camera.incomplete_frames` | captures and runs on that camera (`camera.snap` to it) | live view; the other camera | under 1 % of ≥ 200 frames incomplete in 60 s of live view |
| `storage.data_saving.time_to_full` | `storage.time_to_full` (data drive) | starting new runs that save data | live view, single captures, moves; a running run | the data drive is not projected to fill within 6 h, over 5 min of readings |

The other Phase 1 rules stay WATCH only: command bursts, link errors, reconnects, drift-warning recurrence and landing
re-approaches. Verifying them would need a test move or a command. The kit forbids automating a diagnostic move, and
a release with nothing to verify would be no verification at all.

### 4. Ask, confirm, verify, release

- **Propose.** For each open WATCH a policy covers, Health lists a proposal on the Health page and journals
  `protection.proposed`. Nothing is restricted. A test leaves WATCHes with policies open over many samples and checks
  that no restriction is ever created without the operator.
- **Confirm.** The operator presses **Confirm…**. The dialog states what is restricted, why, what stays available,
  the release conditions, and any running run. It journals `protection.confirmed`. The registry journals
  `restriction.active` or `restriction.pending` through Health.
- **Decline.** No restriction for that WATCH. A new WATCH proposes again. Journals `protection.declined`.
- **Acknowledge.** The operator saw a WATCH. It clears nothing: the kit's "acknowledge alert ≠ clear latch". Journals
  `assessment.acknowledged`.
- **Verify and release.** Every sample, Health runs each active restriction's verification. The rule's WATCH
  resolving is not enough. When the verification passes, Health calls `release(by="verification", verification=…)`.
  The registry refuses that call without a passed verification. It journals `restriction.released` with the result.
  A failed check is recorded in the restriction's history when its outcome changes.
- **Lift.** The operator may lift a restriction at any time with **Lift…**, after a confirmation. This is recorded as
  "lifted by the operator, without verification". The operator imposed it, so lifting it is not a safety bypass. The
  StageMonitor's locks are untouched, and this panel cannot release them.
- **Escalate.** A restriction confirmed again for the same policy and key within 24 h of a release is marked
  recurring. It journals `service.escalated`: a technician should inspect.

The Protection panel's buttons appear only when there is something to decide. With nothing proposed or restricted,
the page's only button is still the diagnostic export. Every action asks first. None sends a device command.

### 5. What the page shows

A confirmed restriction reads RESTRICTED on its capability. The authority is "SafetyManager", and the policy and its
reason are quoted. It also appears under "Why is operation restricted?" with its release conditions. Proposals,
restrictions (with their latest verification) and unacknowledged WATCHes are listed in the Protection panel. The
diagnostic bundle gains `restrictions.json`: every restriction with its history, and the open proposals.

## Consequences

- **Exit "no safety bypass".**
  - Health adds a refusal and never removes one. The SafetyManager is still the only place commands are allowed.
  - Restrictions skip safe-direction commands. A test shows a restricted LED refusing "on" and allowing "off".
  - The StageMonitor's lock and ADR-0067's recovery are unchanged. A Health refusal is not a drive trip, so it never
    starts a recovery.
- **Exit "no scientific frame loss".** A test confirms a camera restriction during a running Z-stack with "stop at the
  next safe point". The stack stops with its slices kept. The restriction takes effect after it, and a capture on that
  camera is then refused.
- **Exit "release requires verification".**
  - The registry refuses a Health release without a passed verification.
  - The end-to-end test injects the drift offset, confirms, and refuses a two-position scan before any frame reaches
    the drives. It then removes the offset and shows the restriction staying in force after the WATCH resolves. It is
    released only after the 5-minute verification passes.
  - Operator lifts are allowed and recorded as such.
- **Runs gain a refusal path.** Every runner can now raise `RestrictionRefused` at its start, before it moves
  anything. It reaches the page through the run task's normal error path. With no restriction, the guard costs one
  lock acquisition per run.
- **Restrictions persist.** A restriction stays in force across restarts until it is verified or lifted. The file is
  plain JSON. Deleting it would lift everything at the next start with no record, so lift through the page instead.

## Not done (later phases or the physical gate)

- **Qualifying on the AMAFD1000.** The policies' limits and verifications are provisional, as in Phase 1. Running
  them on the instrument needs the operator's approval in chat. So does validating pause, stop and writer flush, power
  interruption, camera disconnect and storage exhaustion (the kit's physical gates).
- **A policy restricting a single device's commands after a command burst.** It needs a verification that does not
  command the device; none exists yet.
- **Single-image saves under the storage restriction.** They stay allowed; it restricts new runs only.
- **Later phases:** streaming analytics (Phase 3) and the diagnostic assistant (Phase 4).
