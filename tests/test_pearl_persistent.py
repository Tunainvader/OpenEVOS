"""Parser for PearlScope's persistent-data stream, tested on SYNTHETIC streams only.

The encoder below writes the layout derived from the decompiled readers (schema doc). No values
from a real instrument are in this repository.
"""

from __future__ import annotations

import struct

import pytest

from openevos.maintenance import pearl_persistent as pp


def s(text: str) -> bytes:
    return struct.pack("<i", len(text)) + text.encode("utf-16le")


def sec(marker: bytes) -> bytes:
    return marker + struct.pack("<d", 1.0)


def encode(apo: bool, pgr_pairs=((10, 20),), slots=pp.TURRET_SLOTS) -> bytes:
    out = sec(b"coca") + struct.pack("<3d", 1.5, 0.0, 2.5)
    out += sec(b"moca") + struct.pack("<i", len(pgr_pairs)) + b"".join(struct.pack("<2H", *p) for p in pgr_pairs)
    out += sec(b"mcba") + b"".join(struct.pack("<d" if t == "d" else "<i", 3 if t == "i" else 0.5)
                                   for t, _ in pp.MCBA_FIELDS)
    out += sec(b"moco") + struct.pack("<3i3d2i", 1, 2, 3, 1.1, 1.2, 0.7, 4, -5)
    out += sec(b"tpco")
    tail = pp.TAIL_KEYS_16 if apo else pp.TAIL_KEYS_PRE16
    for k in range(slots):
        out += s(f"|Objective {k}|")
        for _cam in pp.CAMERAS:
            out += b"".join(s(f"|{k}{key}|") for key in pp.PER_CAMERA_KEYS)
        out += b"".join(s(f"|{key}|") for key in tail)
    out += b"".join(struct.pack("<2i", slot, 1) for slot in (2, 3, 4, 5, 7))
    return out


@pytest.mark.parametrize("apo,layout", [(True, "1.6"), (False, "pre-1.6 (no -APO)")])
def test_both_layouts_parse_exactly(apo, layout):
    data = encode(apo)
    pd = pp.parse(data)
    assert pd.layout == layout and pd.length == len(data)
    assert pd.uEye_color_gains == {"RedGain": 1.5, "GreenGain": 0.0, "BlueGain": 2.5}
    assert pd.pgr_pairs == [(10, 20)]
    assert pd.motor["XAxisMillimetersPerRevolution"] == 1.1 and pd.motor["YAxisRegistration"] == -5
    assert pd.motor["XAxisOffset"] == 1 and pd.motor["ZAxisOffset"] == 3
    assert len(pd.motor_base) == 23 and pd.motor_base["XAxisPositionLimits.lMax"] == 3
    assert pd.motor_base["FilterCubePositioningSpeed"] == 0.5
    assert len(pd.turret_slots) == 8 and pd.turret_slots[0].objectives == "|Objective 0|"
    assert pd.turret_slots[3].per_camera[pp.CAMERAS[1]]["-Parfocalities"] == "|3-Parfocalities|"
    assert ("-APO" in pd.turret_slots[0].tail) is apo
    assert pd.turret_positions == [(1, 2, 1), (2, 3, 1), (3, 4, 1), (4, 5, 1), (5, 7, 1)]
    assert pd.capability_source == "CANDIDATE"


def test_leftover_bytes_are_rejected():
    with pytest.raises(pp.ParseError, match="no known layout"):
        pp.parse(encode(True) + b"\x00")


def test_truncated_stream_is_rejected():
    with pytest.raises(pp.ParseError):
        pp.parse(encode(False)[:-1])


@pytest.mark.parametrize("old,new", [(b"moco", b"mocx"), (b"tpco", b"tpcx")])
def test_wrong_marker_is_rejected(old, new):
    with pytest.raises(pp.ParseError):
        pp.parse(encode(True).replace(old, new, 1))


def test_wrong_version_is_rejected():
    data = bytearray(encode(True))
    data[4:12] = struct.pack("<d", 2.0)
    with pytest.raises(pp.ParseError):
        pp.parse(bytes(data))


def test_cli(tmp_path, capsys):
    f = tmp_path / "decoded.bin"
    f.write_bytes(encode(False))
    assert pp.main([str(f), "--out", str(tmp_path / "p.json")]) == 0
    assert "pre-1.6" in capsys.readouterr().out and (tmp_path / "p.json").exists()
    f.write_bytes(b"junk")
    assert pp.main([str(f)]) == 1
