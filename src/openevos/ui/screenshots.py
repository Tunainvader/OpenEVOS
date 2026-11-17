"""Offscreen workspace screenshots (documentation and visual review)."""

from __future__ import annotations

import time
from pathlib import Path


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def capture_all_pages(app, win, out: Path, settle_s: float = 1.2) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    win.resize(1680, 1000)
    win.show()
    pump(app, settle_s * 2)
    paths = []
    for key in win.pages:
        win.navigate(key)
        pump(app, settle_s)
        p = out / f"{key}.png"
        win.grab().save(str(p))
        paths.append(p)
    return paths
