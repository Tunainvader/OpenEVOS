"""Instrument Health, Phase 0: trustworthy status (ADR-0068; the Instrument Health & Predictive Maintenance kit v1.0).

Health observes and recommends; it never commands. The StageMonitor and the SafetyManager stay authoritative, and every
hardware action still goes CommandRouter -> SafetyManager -> DeviceManager. This package only reads:

* `inventory`  what each adapter really exposes today (units, rate, source), and what is NOT MONITORED;
* `model`      the six health states, the five provenance labels, subsystems, operating capabilities;
* `assess`     pure functions: device snapshots, the StageMonitor, the SafetyManager's decision -> a HealthReport;
* `journal`    a local SQLite event journal behind a bounded, non-blocking queue;
* `service`    the recorder: subscribes to the EventBus and the RuntimeStateStore, samples the StageMonitor, writes
               the journal and keeps the latest report for the Health page.

If any of it fails, the existing safety behaviour does not change: nothing here is on the command path.
"""
