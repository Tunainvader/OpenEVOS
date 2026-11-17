# ADR-0008: Phase 2.1 core execution model, event bus and capability provenance

- **Status:** Accepted for Phase 2. Revisit before the first physical backend.
- **Date:** 2026-11-12
- **Context:** handoff §30 "Architecture" open questions 1–5 and its review findings §13–§16.

## Decisions

### 1. Qt-free core; Qt only at the edge
`openevos.core`, `safety`, `hal` and `backends` import no Qt; a test enforces this. The UI (Phase 2.3) subscribes to the bus with a **dispatcher** that marshals each delivery onto the Qt main thread, for example a queued signal in `ui/qt_event_bridge.py`. Worker threads never touch widgets. The core can therefore be unit- and concurrency-tested headless, and could later serve a non-Qt front end.

### 2. Threads, not asyncio; one serialized worker per device
Backend `start()` calls run on one `DeviceWorker` thread per device, in FIFO order. Backends must return promptly and complete asynchronously. Simulated latency and command timeouts use an **injected clock**: `ThreadClock` in the application, `ManualClock` in tests. Nothing sleeps (a test enforces this too), and lifecycle tests are fully deterministic.

### 3. In-house EventBus instead of `psygnal` (for now)
A ~100-line bus with these properties: per-instance subscriptions, typed frozen payloads, timestamps per instance, an explicit dispatcher hook, and subscriber exceptions isolated from publishers. It adds no dependency and its thread behaviour is spelled out rather than assumed. If `pymmcore-plus` (which uses psygnal) is adopted, its adapter will translate psygnal signals into these events at the backend boundary.

### 4. Command lifecycle
QUEUED → VALIDATING → ACCEPTED → RUNNING → SUCCEEDED, with REJECTED, FAILED, CANCELLING → CANCELLED and TIMED_OUT branches. The rules:

- Validation and resource reservation are **atomic** under one admission lock.
- RUNNING is emitted only when the backend calls `mark_started()`.
- SUCCEEDED for motion requires a **readback within the capability's tolerance**. A mismatch means FAILED plus `recovery_required`.
- Terminal states are sticky. Late or duplicate backend callbacks are ignored and counted.
- Resources are released **before** the terminal event is published, so a subscriber can chain the next command immediately.
- **TIMED_OUT keeps the reservation** and sets `recovery_required`. Only `router.recover_device()` releases it, after the backend re-establishes its observed state.

Callers get a `CommandTicket` (observe, wait, cancel). Backends get an `ExecutionContext` (`mark_started` / `complete` / `fail` / `mark_cancelled`). Neither can drive the other's side.

### 5. Resource interlocks
Each command declares exclusive and shared resources. Long-lived work (acquisitions) holds shared claims on the devices that must stay stable, so a stage move cannot start mid-acquisition and an acquisition cannot start mid-move.

**Not yet designed:** how a running acquisition issues its *own* focus or stage steps, as a Z-stack must. The likely approach is child commands carrying the acquisition's correlation ID, which may use resources held by their parent.

### 6. Capability provenance
Limits, units, frame, calibration and readback tolerance live in `DeviceCapabilities`, never in code. The record's `source` is one of:

- `SIMULATED`: valid only on simulated backends (enforced when the record is constructed).
- `CANDIDATE`: from documentation or vendor software, such as a recovered `PearlScope.ini`. Never sufficient to command hardware.
- `VERIFIED`: required for any physical command, and must cite evidence IDs.

The handoff's literal stage limits (110000 / 75000) are not used anywhere. Simulator ranges are deliberately origin-centred (±50 000 µm) so they can't be mistaken for EVOS travel limits.

### 7. Frames
Frames are adopted once: copied if the source buffer may be reused, then made read-only. After that, preview and storage share the frame safely.

- **Preview** keeps the latest N frames and counts drops.
- **Scientific path** has its own bounded queue and writer thread per acquisition. Queue overflow, a sink error or an out-of-order frame **faults the stream**, publishes a `StorageFaultEvent` with the acquisition ID and frame index, and makes the producer stop. Every frame is accounted for as written, failed or rejected.
- `NpyDirectorySink` is a temporary durable sink until the OME-TIFF/Zarr writer arrives.

## Consequences / deferred
- Handoff open question 1 (execution model) and questions 2–4 (authoritative state, cancellation/timeout recovery, resources) are answered for Phase 2.
- Still open: frame queue sizing for real cameras, the OME-TIFF/Zarr choice, multi-step acquisition ownership, and cross-process ownership (vs a separate Micro-Manager session), which each physical backend must still enforce.
