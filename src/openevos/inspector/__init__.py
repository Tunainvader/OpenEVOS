"""OpenEVOS Inspector v0.1 — read-only system, device, driver and software inventory.

The Inspector never opens a device, never transmits on a serial/USB endpoint, and never
writes outside the user-selected output directory. See docs/decisions/ADR-0003.
"""

from openevos import __version__ as TOOL_VERSION

__all__ = ["TOOL_VERSION"]
