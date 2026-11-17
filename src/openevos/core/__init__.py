"""OpenEVOS application core (plan §5.2 B): services between the UI and the HAL.

* `device_manager` — configuration-driven backend selection and instrument start-up
* `channels`       — imaging channel presets derived from what the devices report
* `controller`     — MicroscopeController: the operations the UI calls (all via the gateway)
* `acquisition`    — multi-dimensional acquisition plan and engine (channels × positions × Z × T)
* `diagnostics`    — read-only loader for Inspector scan folders
* `audit`          — audit trail of every device command
* `instrument`     — gateway-era instrument assembly (was `device_manager` before the v0.2.0 merge)

Phase 2.1 command core (merged from v0.2.0; ADR-0007 state axes, ADR-0008 execution model):

* `models`             — typed commands, three-axis device snapshots, command lifecycle
* `event_bus`          — per-instance bus with an explicit dispatcher hook (Qt marshalling)
* `runtime_state_store`— authoritative, versioned, immutable device snapshots
* `device_manager`     — backend registration, single-owner rule, one worker per device
* `command_scheduler`  — resource reservations; `command_router` — validated lifecycle
* `frame_transport`    — bounded preview vs. lossless scientific path
* `application`        — composition root (`build_core`)

The core never imports PySide6 or a hardware transport (tests/test_phase2_import_guard.py).
"""
