"""Two stage legs sent back to back (2026-11-21: after an objective change the X return leg was refused twice with
"device activity is moving, not idle": the drive service ended the Y leg's command before it published IDLE).

The simulated chain answers instantly; the real line takes ~16 ms per read, which opens the gap. The chain here
sleeps on every read, as the line does."""

from __future__ import annotations

import time

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.backends.technosoft import protocol as tp
from openevos.core.clock import ThreadClock
from openevos.instrument.settings import AppSettings

READ_S = 0.016


@pytest.fixture
def session(tmp_path):
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings(),
                          calibration_path=tmp_path / "calibration.json",
                          telemetry={"technosoft": {"port": "sim", "poll_interval_s": 0.02, "motion": True}})
    chain = s.telemetry[0].simulated_chain
    real_write = chain.write

    def slow_write(data: bytes) -> int:
        if data in tp.ALLOWED_REQUESTS and data != bytes([tp.SYNC_BYTE]):
            time.sleep(READ_S)
        return real_write(data)

    chain.write = slow_write
    end = time.monotonic() + 10
    while time.monotonic() < end and s.controller.stage_xy() is None:
        time.sleep(0.05)
    yield s
    s.shutdown()


def test_the_next_leg_is_accepted_as_soon_as_the_last_one_ends(session):
    ctl = session.controller
    x0, y0 = ctl.stage_xy()
    for x, y in ((x0 + 2_000.0, y0), (x0 + 2_000.0, y0 + 1_500.0), (x0 + 2_000.0, y0), (x0, y0)):
        ctl.run(ctl.move_stage(x, y), timeout=30.0)  # each leg sent the moment the previous one succeeded
    assert ctl.stage_xy() == pytest.approx((x0, y0), abs=1.0)
