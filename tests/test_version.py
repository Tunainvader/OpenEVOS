"""The package version shown in the window title and audit events matches pyproject.toml."""

from __future__ import annotations

import re
from pathlib import Path

import openevos


def test_package_version_matches_pyproject():
    # regex, not tomllib: the project still supports Python 3.10
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text("utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert declared and openevos.__version__ == declared.group(1)
