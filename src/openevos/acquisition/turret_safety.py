"""Where the stage must be for the objective turret to turn (shared by the UI; the drive service enforces it).

Gate 19: an objective struck the stage frame at X/Y 0, 0; a full turret cycle was clean at X 59.5, Y 39.0 mm. The
drive service (backends/technosoft/telemetry.py TURRET_STAGE_BOX_MM) refuses a turret move outside this box; the UI
uses the same box to offer centring the stage first (tests/test_turret_centre.py keeps the two equal).
"""

from __future__ import annotations

TURRET_STAGE_BOX_MM = {"X": (40.0, 80.0), "Y": (20.0, 60.0)}
#: where "centre the stage" goes: the middle of the box, near Gate 19's clean cycle
TURRET_CENTRE_MM = {"X": 60.0, "Y": 40.0}


def outside_turret_box(xy_um: tuple[float, float] | None) -> bool:
    """True when the stage position is known and outside the box (unknown: the drive service decides)."""
    if xy_um is None or xy_um[0] is None or xy_um[1] is None:
        return False
    return any(not lo <= v / 1000.0 <= hi for v, (lo, hi) in zip(xy_um, TURRET_STAGE_BOX_MM.values(), strict=True))
