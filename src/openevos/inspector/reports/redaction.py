"""Value-based redaction for exports and AI evidence packages (plan §7.5).

Sensitive values (computer name, device serial numbers, user names in paths) are collected first,
then every occurrence in every string of the export is replaced by a stable keyed token, e.g.
`<redacted:3f9a1c2e>`. The same value maps to the same token in every file and across scans made
with the same key, so redacted baseline/connected scans can still be diffed.

The key lives in the OpenEVOS user config directory (created on first --redact). This is the only
file the Inspector writes outside the chosen output directory.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

from openevos.inspector.collectors import ScanResults
from openevos.inspector.evidence import sha256_of

_USER_PATH = re.compile(r"(?i)(?:[a-z]:\\users\\|/home/|/users/)([^\\/]+)")
_GENERIC_USERS = {"public", "default", "default user", "all users", "administrator", "defaultapppool"}
_MIN_LEN = 5
_SKIP_KEYS = frozenset({"id", "evidence_ids", "sha256", "rule_id", "scan_id", "cites_findings", "vid", "pid", "rev", "mi"})  # noqa: E501


def load_or_create_key(path: Path | None = None) -> bytes:
    if path is None:
        from platformdirs import user_config_dir

        path = Path(user_config_dir("OpenEVOS", appauthor=False)) / "redaction.key"
    if path.exists():
        return bytes.fromhex(path.read_text("ascii").strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex(), "ascii")
    return key


def collect_sensitive_values(results: ScanResults, extra: list[str] | None = None) -> set[str]:
    vals: set[str] = set(extra or [])
    if results.system and results.system.computer_name:
        vals.add(results.system.computer_name)
    for d in results.devices:
        if d.usb and d.usb.serial_in_instance:
            vals.add(d.usb.serial_in_instance)
    for p in results.serial_ports:
        if p.serial_number:
            vals.add(p.serial_number)
    return {v for v in vals if v and len(v) >= _MIN_LEN}


class Redactor:
    def __init__(self, key: bytes, values: set[str]):
        self.key = key
        self.values = sorted(values, key=len, reverse=True)
        self._rx = (
            re.compile("|".join(re.escape(v) for v in self.values), re.IGNORECASE) if self.values else None
        )

    def token(self, value: str) -> str:
        mac = hmac.new(self.key, value.upper().encode("utf-8"), hashlib.sha256).hexdigest()[:8]
        return f"<redacted:{mac}>"

    def text(self, s: str) -> str:
        if self._rx is not None:
            s = self._rx.sub(lambda m: self.token(m.group(0)), s)

        def _user(m: re.Match) -> str:
            name = m.group(1)
            if name.lower() in _GENERIC_USERS or name.startswith("<redacted:"):
                return m.group(0)
            return m.group(0)[: m.start(1) - m.start(0)] + self.token(name)

        return _USER_PATH.sub(_user, s)

    def obj(self, o: Any, key: str | None = None) -> Any:
        if key in _SKIP_KEYS:
            return o  # identifiers and hashes: never rewrite (a hex serial could collide)
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x, key) for x in o]
        if isinstance(o, dict):
            return {k: self.obj(v, k) for k, v in o.items()}
        return o

    def register_dict(self, reg: dict) -> dict:
        """Redact an EvidenceRegister dump and re-seal each evidence item's hash."""
        out = self.obj(reg)
        for ev in out.get("evidence", {}).values():
            ev["sha256"] = sha256_of(ev["data"])
            ev["redacted"] = True
        return out
