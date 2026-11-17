"""USB identity parsing and topology helpers (pure functions; no OS access).

Plan §4.2: keep *physical device* identity and *interface* identity separate. A composite USB
device appears in Windows as a parent (USB\\VID_x&PID_y) plus one child per interface (&MI_nn).
"""

from __future__ import annotations

import re
from collections import defaultdict

from openevos.inspector.schemas import PnpDevice, UsbIdentity

_VIDPID = re.compile(
    r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})(?:[&+]REV_([0-9A-F]{4}))?(?:[&+]MI_([0-9A-F]{2}))?",
    re.IGNORECASE,
)
_MI = re.compile(r"[&+]MI_([0-9A-F]{2})", re.IGNORECASE)
USB_ENUMERATORS = frozenset({"USB", "HID", "FTDIBUS", "USBSTOR", "USBPRINT"})


def enumerator(instance_id: str) -> str:
    return instance_id.split("\\", 1)[0].upper()


def parse_usb_identity(instance_id: str, hardware_ids: list[str] | tuple[str, ...] = ()) -> UsbIdentity | None:
    if enumerator(instance_id) not in USB_ENUMERATORS and not any(
        h.upper().startswith("USB\\") for h in hardware_ids
    ):
        return None
    match = None
    for candidate in [*hardware_ids, instance_id]:
        match = _VIDPID.search(candidate)
        if match:
            break
    if not match:
        return None
    vid, pid, rev, mi = (g.upper() if g else None for g in match.groups())
    if mi is None:
        m2 = _MI.search(instance_id)
        mi = m2.group(1).upper() if m2 else None
    return UsbIdentity(
        vid=vid, pid=pid, rev=rev, mi=mi, is_interface=mi is not None,
        serial_in_instance=serial_from_instance(instance_id),
    )


def serial_from_instance(instance_id: str) -> str | None:
    """Return the iSerialNumber Windows embedded in the instance ID, if any.

    USB\\VID_0403&PID_6001\\A12345B   -> 'A12345B'  (device reported a serial)
    USB\\VID_046D&PID_C52B\\5&2a3b&0&3 -> None       (location-derived ID: contains '&')
    """
    parts = instance_id.split("\\")
    enum = parts[0].upper() if parts else ""
    if enum == "USB" and len(parts) >= 3 and "MI_" not in parts[1].upper():
        tail = parts[2]
        return tail if tail and "&" not in tail else None
    if enum == "FTDIBUS" and len(parts) >= 2:
        segs = parts[1].split("+")
        return segs[2] if len(segs) >= 3 and segs[2] else None
    return None


def is_usb_tree_member(dev: PnpDevice) -> bool:
    return enumerator(dev.instance_id) in USB_ENUMERATORS or (dev.device_class or "").upper() == "USB"


def build_tree(devices: list[PnpDevice], present_only: bool = True) -> list[tuple[int, PnpDevice]]:
    """Depth-first (depth, device) list of the USB topology for display."""
    nodes = {
        d.instance_id.upper(): d
        for d in devices
        if is_usb_tree_member(d) and (d.present is not False or not present_only)
    }
    children: dict[str, list[PnpDevice]] = defaultdict(list)
    roots: list[PnpDevice] = []
    for d in nodes.values():
        parent = (d.parent_instance_id or "").upper()
        if parent and parent in nodes:
            children[parent].append(d)
        else:
            roots.append(d)
    out: list[tuple[int, PnpDevice]] = []

    def visit(d: PnpDevice, depth: int, seen: set[str]) -> None:
        key = d.instance_id.upper()
        if key in seen:
            return
        seen.add(key)
        out.append((depth, d))
        for c in sorted(children.get(key, []), key=lambda x: x.instance_id):
            visit(c, depth + 1, seen)

    seen: set[str] = set()
    for r in sorted(roots, key=lambda x: x.instance_id):
        visit(r, 0, seen)
    return out


def physical_parent(dev: PnpDevice, by_id: dict[str, PnpDevice]) -> PnpDevice:
    """Walk up from an interface (MI_nn) node to its composite parent with the same VID/PID."""
    cur = dev
    for _ in range(8):
        parent = by_id.get((cur.parent_instance_id or "").upper())
        if (
            parent is None or parent.usb is None or cur.usb is None
            or parent.usb.vid_pid != cur.usb.vid_pid
        ):
            return cur
        cur = parent
    return cur
