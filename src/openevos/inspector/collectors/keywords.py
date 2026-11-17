"""Keyword matching for EVOS-related names.

These are search terms, not facts: a hit means "worth a human look", and findings built on a
keyword alone are never stronger than PROBABLE. Short tokens use word boundaries to limit noise.
"""

from __future__ import annotations

import re

# Brand / historical vendor names associated with the EVOS product line. Verify any hit manually.
EVOS_KEYWORDS: dict[str, re.Pattern[str]] = {
    # Not "openevos": otherwise this tool's own install, source and scan folders are reported
    # as EVOS remnants (seen on the first real Windows run, 2026-11-12).
    "evos": re.compile(r"(?<!open)evos", re.IGNORECASE),
    "invitrogen": re.compile(r"invitrogen", re.IGNORECASE),
    "life technologies": re.compile(r"life[\s_\-]?tech(nologies)?", re.IGNORECASE),
    "thermo fisher": re.compile(r"thermo[\s_\-]?fisher", re.IGNORECASE),
    "advanced microscopy group": re.compile(r"advanced[\s_\-]?microscopy", re.IGNORECASE),
    "amg": re.compile(r"(?<![a-z0-9])amg(?![a-z0-9])", re.IGNORECASE),
    "amafd1000": re.compile(r"amafd\s?1000", re.IGNORECASE),
    "amc1000": re.compile(r"amc\s?1000", re.IGNORECASE),
    "onstage incubator": re.compile(r"onstage", re.IGNORECASE),
}


def keyword_hits(*texts: str | None) -> list[str]:
    hits: list[str] = []
    for name, rx in EVOS_KEYWORDS.items():
        if any(t and rx.search(t) for t in texts):
            hits.append(name)
    return hits
