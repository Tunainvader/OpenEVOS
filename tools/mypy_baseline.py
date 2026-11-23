"""mypy against a recorded baseline (audit APP-20): only NEW type errors fail.

    python tools/mypy_baseline.py            # check: exit 1 if mypy reports an error that is not in the baseline
    python tools/mypy_baseline.py --update   # record the current errors as the baseline (after a reviewed change)

Errors are matched on (file, error code, message) with counts, not on line numbers, so editing a file does not turn its
old errors into "new" ones. Config: [tool.mypy] in pyproject.toml. Code under STRICT_PATHS (what commands the
instrument) gets no baseline allowance at all: any error there fails.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "mypy-baseline.txt"
#: the code that commands the instrument: held at zero mypy errors, with stricter settings ([[tool.mypy.overrides]] in
#: pyproject.toml); the baseline never covers an error here (2026-11-24)
STRICT_PATHS = (
    "src/openevos/core/",
    "src/openevos/safety/",
    "src/openevos/hal/",
    "src/openevos/backends/technosoft/",
    "src/openevos/backends/ledboard/",
    "src/openevos/acquisition/controller.py",
    "src/openevos/acquisition/session.py",
)
LINE = re.compile(r"^(?P<file>[^:]+):\d+(?::\d+)?: error: (?P<msg>.*?)\s+\[(?P<code>[a-z0-9-]+)\]$")


def current() -> Counter:
    # check=False: mypy exits 1 whenever it reports errors; the baseline comparison decides
    out = subprocess.run([sys.executable, "-m", "mypy", "--no-color-output", "--no-error-summary"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", check=False).stdout
    keys = Counter()
    for raw in out.splitlines():
        m = LINE.match(raw.strip())
        if m:
            keys[f"{m['file'].replace(chr(92), '/')} [{m['code']}] {m['msg']}"] += 1
    return keys


def load() -> Counter:
    keys = Counter()
    if BASELINE.exists():
        for ln in BASELINE.read_text("utf-8").splitlines():
            if ln and not ln.startswith("#"):
                n, _, key = ln.partition(" ")
                keys[key] = int(n)
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="record the current errors as the baseline")
    a = ap.parse_args(argv)
    now = current()
    if a.update:
        lines = ["# mypy baseline (tools/mypy_baseline.py --update): count, then file [code] message"]
        lines += [f"{n} {k}" for k, n in sorted(now.items())]
        BASELINE.write_text("\n".join(lines) + "\n", "utf-8")
        print(f"baseline: {sum(now.values())} errors in {len(now)} distinct entries")
        return 0
    base = load()
    strict = {k: n for k, n in now.items() if k.startswith(STRICT_PATHS)}
    if strict:  # no baseline allowance here, even for an error that is listed in it
        print(f"{sum(strict.values())} mypy error(s) in code held to zero (STRICT_PATHS):")
        for k, n in sorted(strict.items()):
            print(f"  {n}x {k}")
        return 1
    new = {k: n - base.get(k, 0) for k, n in now.items() if n > base.get(k, 0)}
    fixed = sum(max(0, n - now.get(k, 0)) for k, n in base.items())
    if new:
        print(f"{sum(new.values())} new mypy error(s) not in the baseline:")
        for k, n in sorted(new.items()):
            print(f"  {n}x {k}")
        return 1
    print(f"mypy: no new errors ({sum(now.values())} known; {fixed} fixed since the baseline"
          + (": run with --update to shrink it" if fixed else "") + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
