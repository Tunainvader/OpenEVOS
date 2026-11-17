"""Real EVOS Gen III LED board backend for the app (ADR-0048)."""

from openevos.backends.ledboard.illumination import (
    DEVICE_ID,
    RealLedIllumination,
    present_application_interfaces,
)

__all__ = ["DEVICE_ID", "RealLedIllumination", "present_application_interfaces"]
