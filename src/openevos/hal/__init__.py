"""OpenEVOS hardware abstraction layer (plan §6).

* `status_model`   — runtime state, a separate axis from evidence stage (ADR-0004, ADR-0007)
* `types`          — DeviceIdentity, DeviceStatus, Frame, Role and other value types
* `interfaces`     — device contracts: camera, XY stage, focus, turrets, illumination, incubator
* `device_registry`— one device per role
* `backend`        — the command core's DeviceBackend contract (ADR-0008, ADR-0009)
* `adapter`        — runs a simulated HAL device as a DeviceBackend (ADR-0010)

State changes reach a device only through the command core (`core.command_router`); the named
command language the UI speaks lives in `core.command_language`.

No module in this package imports a hardware transport (tests/test_phase2_import_guard.py).
"""
