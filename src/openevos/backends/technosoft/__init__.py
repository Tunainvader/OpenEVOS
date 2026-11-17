"""Read-only Technosoft motor telemetry backend (ADR-0023). No commands, no writes to the drives."""

from openevos.backends.technosoft.telemetry import TelemetryService, create_telemetry

__all__ = ["TelemetryService", "create_telemetry"]
