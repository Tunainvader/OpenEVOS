"""Minimal read-only PE header parsing (cross-platform, no Windows APIs).

Answers the question the plan's risk table cares about — "is this SDK/driver 32-bit?" — for
binaries on a live system *or* on a mounted copy of the recovered EVOS disk image.
Files are opened with mode 'rb' only.
"""

from __future__ import annotations

import struct
from pathlib import Path

from openevos.inspector.schemas import PeInfo

PE_EXTENSIONS = frozenset({".exe", ".dll", ".sys", ".ocx", ".ax", ".cpl", ".drv", ".scr"})

_MACHINES = {0x014C: "x86", 0x8664: "x64", 0xAA64: "arm64", 0x01C0: "arm", 0x01C4: "arm", 0x0200: "ia64"}

_CLR_ILONLY = 0x00000001
_CLR_32BITREQUIRED = 0x00000002
_CLR_32BITPREFERRED = 0x00020000

_MAX_HEADER = 64 * 1024


def parse_pe(path: Path) -> PeInfo | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(_MAX_HEADER)
            return parse_pe_bytes(head, fh)
    except OSError:
        return None


def parse_pe_bytes(head: bytes, fh=None) -> PeInfo | None:
    if len(head) < 0x40 or head[:2] != b"MZ":
        return None
    (e_lfanew,) = struct.unpack_from("<I", head, 0x3C)
    if e_lfanew + 24 > len(head) or head[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        return None
    coff = e_lfanew + 4
    machine, n_sections = struct.unpack_from("<HH", head, coff)
    (opt_size,) = struct.unpack_from("<H", head, coff + 16)
    opt = coff + 20
    if opt + 2 > len(head):
        return PeInfo(machine=_MACHINES.get(machine, "unknown"), pe_format="unknown")
    (magic,) = struct.unpack_from("<H", head, opt)
    pe_format = {0x10B: "PE32", 0x20B: "PE32+"}.get(magic, "unknown")
    machine_name = _MACHINES.get(machine, "unknown")

    # Data directory 14 = CLR runtime header (.NET)
    dd_base = opt + (96 if pe_format == "PE32" else 112)
    clr_rva = 0
    if pe_format != "unknown" and dd_base + 15 * 8 <= len(head):
        clr_rva, _clr_size = struct.unpack_from("<II", head, dd_base + 14 * 8)

    if not clr_rva:
        bitness = {"x86": "32", "arm": "32", "x64": "64", "arm64": "64", "ia64": "64"}.get(machine_name, "unknown")
        return PeInfo(machine=machine_name, pe_format=pe_format, effective_bitness=bitness)

    flags = _read_clr_flags(head, fh, opt + opt_size, n_sections, clr_rva)
    clr_flags = None
    bitness = "unknown"
    if flags is not None:
        clr_flags = {
            "il_only": bool(flags & _CLR_ILONLY),
            "32bit_required": bool(flags & _CLR_32BITREQUIRED),
            "32bit_preferred": bool(flags & _CLR_32BITPREFERRED),
        }
        if pe_format == "PE32+":
            bitness = "64"
        elif clr_flags["32bit_required"] or not clr_flags["il_only"]:
            bitness = "32"
        elif clr_flags["32bit_preferred"]:
            bitness = "32"  # AnyCPU-32-bit-preferred runs as 32-bit by default
        else:
            bitness = "any"
    return PeInfo(
        machine=machine_name, pe_format=pe_format, is_managed=True,
        clr_flags=clr_flags, effective_bitness=bitness,
    )


def _read_clr_flags(head: bytes, fh, sec_table: int, n_sections: int, rva: int) -> int | None:
    for i in range(n_sections):
        off = sec_table + i * 40
        if off + 40 > len(head):
            return None
        vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<IIII", head, off + 8)
        span = max(vsize, raw_size)
        if vaddr <= rva < vaddr + span:
            file_off = raw_ptr + (rva - vaddr)
            blob = _read_at(head, fh, file_off, 20)
            if blob is None or len(blob) < 20:
                return None
            (flags,) = struct.unpack_from("<I", blob, 16)
            return flags
    return None


def _read_at(head: bytes, fh, offset: int, n: int) -> bytes | None:
    if offset + n <= len(head):
        return head[offset : offset + n]
    if fh is None:
        return None
    fh.seek(offset)
    return fh.read(n)


def version_info(path: Path) -> dict[str, str]:
    """FileVersion/ProductName/CompanyName etc. via pefile (read-only). Empty dict on failure."""
    try:
        import pefile
    except ImportError:
        return {}
    out: dict[str, str] = {}
    pe = None
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        for fileinfo in getattr(pe, "FileInfo", None) or []:
            entries = fileinfo if isinstance(fileinfo, list) else [fileinfo]
            for entry in entries:
                if getattr(entry, "Key", b"") == b"StringFileInfo":
                    for table in entry.StringTable:
                        for k, v in table.entries.items():
                            ks = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
                            vs = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                            if vs.strip():
                                out[ks] = vs.strip()
    except Exception:  # noqa: BLE001 — malformed resources must never crash a scan
        return out
    finally:
        if pe is not None:
            pe.close()
    return out
