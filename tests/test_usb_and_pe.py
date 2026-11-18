import pytest
from conftest import INF_TEXT, make_pe

from openevos.inspector.collectors.drivers import decode_inf, parse_inf
from openevos.inspector.collectors.usb_devices import parse_usb_identity, serial_from_instance
from openevos.inspector.pe import parse_pe_bytes


@pytest.mark.parametrize("iid,hw,expect", [
    ("USB\\VID_0403&PID_6001\\A12345BC", ["USB\\VID_0403&PID_6001&REV_0600"], ("0403", "6001", "0600", None, "A12345BC")),  # noqa: E501
    ("USB\\VID_046D&PID_C52B&MI_00\\6&2B3C&0&0000", ["USB\\VID_046D&PID_C52B&REV_1211&MI_00"], ("046D", "C52B", "1211", "00", None)),  # noqa: E501
    ("USB\\VID_046D&PID_C52B\\5&1A2B&0&3", [], ("046D", "C52B", None, None, None)),
    ("FTDIBUS\\VID_0403+PID_6001+A12345BCA\\0000", ["FTDIBUS\\COMPORT&VID_0403&PID_6001"], ("0403", "6001", None, None, "A12345BCA")),  # noqa: E501
    ("USB\\vid_abcd&pid_00ef\\x", [], ("ABCD", "00EF", None, None, "x")),
])
def test_parse_usb_identity(iid, hw, expect):
    u = parse_usb_identity(iid, hw)
    assert (u.vid, u.pid, u.rev, u.mi, u.serial_in_instance) == expect
    assert u.is_interface == (expect[3] is not None)


def test_non_usb_and_root_hub_have_no_identity():
    assert parse_usb_identity("ACPI\\PNP0303\\4&1d40&0", ["ACPI\\PNP0303"]) is None
    assert parse_usb_identity("USB\\ROOT_HUB30\\4&2A3B&0&0", ["USB\\ROOT_HUB30"]) is None


def test_serial_from_location_id_is_none():
    assert serial_from_instance("USB\\VID_1234&PID_5678\\6&AAAA&0&1") is None


# ---------------------------------------------------------------------------- PE
def test_pe_native_x86_and_x64():
    a = parse_pe_bytes(make_pe(0x14C, 0x10B))
    assert (a.machine, a.pe_format, a.effective_bitness, a.is_managed) == ("x86", "PE32", "32", False)
    b = parse_pe_bytes(make_pe(0x8664, 0x20B))
    assert (b.machine, b.pe_format, b.effective_bitness) == ("x64", "PE32+", "64")


@pytest.mark.parametrize("flags,bitness", [(0x1, "any"), (0x1 | 0x2, "32"), (0x1 | 0x20000, "32"), (0x0, "32")])
def test_pe_dotnet_bitness(flags, bitness):
    info = parse_pe_bytes(make_pe(0x14C, 0x10B, clr_flags=flags))
    assert info.is_managed and info.effective_bitness == bitness


def test_pe_rejects_non_pe_and_truncated():
    assert parse_pe_bytes(b"hello world" * 10) is None
    assert parse_pe_bytes(make_pe()[:0x50]) is None


# ---------------------------------------------------------------------------- INF
def test_inf_utf16_parse_and_string_resolution():
    raw = b"\xff\xfe" + INF_TEXT.encode("utf-16-le")
    parsed = parse_inf(decode_inf(raw))
    assert parsed["provider"] == "Life Technologies"
    assert parsed["class"] == "Image"
    assert parsed["driver_ver"] == "03/01/2013,1.0.0.0"
    assert parsed["usb_ids"] == ["1234:5678", "1234:9999"]
    assert "USB\\VID_1234&PID_5678&MI_00" in parsed["hardware_ids"]


def test_inf_utf16_without_bom_and_latin1():
    assert parse_inf(decode_inf(INF_TEXT.encode("utf-16-le")))["provider"] == "Life Technologies"
    assert parse_inf(decode_inf("[Version]\nProvider=Caf\xe9\n".encode("latin-1")))["provider"] == "Café"
