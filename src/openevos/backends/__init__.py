"""Device backends behind the HAL (plan §6.3).

Only `simulation` exists in v0.2. Backends are selected by name from configuration
(`openevos.core.instrument`), never imported by the UI.
"""

#: backend name -> "module:factory". The factory returns a populated DeviceRegistry.
BACKENDS: dict[str, str] = {
    "simulation": "openevos.backends.simulation:create_simulated_instrument",
}

#: optional read-only telemetry sources, added alongside the main backend (ADR-0023).
#: name -> "module:factory"; the factory returns a service exposing `.backends` and `.stop()`.
TELEMETRY: dict[str, str] = {
    "technosoft": "openevos.backends.technosoft:create_telemetry",
}
