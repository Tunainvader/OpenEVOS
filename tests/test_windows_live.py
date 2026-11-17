"""Opt-in smoke test on a real Windows host: `pytest -m windows`.

Runs each allowlisted read-only query once and checks that the output parses. It does not
touch the microscope (none needs to be connected) and changes nothing on the system.
"""

import base64
import subprocess
import sys

import pytest

from openevos.inspector import safe_exec

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows host"),
]


@pytest.mark.parametrize("key", ["pnp_devices", "pnp_device_properties", "signed_drivers", "system_info"])
def test_query_runs_and_parses(key):
    out = safe_exec.run(key)
    assert out is not None
    if key == "pnp_devices":
        assert any("InstanceId" in row for row in out)


def test_events_query():
    assert isinstance(safe_exec.run("pnp_events", hours=1), list)


@pytest.mark.parametrize("key", sorted(safe_exec.QUERIES))
def test_powershell_parser_agrees_with_allowlist(key):
    """Second opinion on validate_script from PowerShell's own AST parser.

    The query is only *parsed*, never executed. Every command it calls must be a Verb-Noun cmdlet
    with an allowed verb; an alias such as `%` or `rm` fails because it has no Verb-Noun form.
    """
    q = safe_exec.QUERIES[key]
    script = q.render(**{name: lo for name, lo, _hi in q.int_params})
    payload = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    parser = (
        f"$src = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{payload}'))\n"
        "$errs = $null\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$null, [ref]$errs)\n"
        "if ($errs) { $errs | ForEach-Object { 'PARSE-ERROR ' + $_.Message } }\n"
        "$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |"
        " ForEach-Object { $_.GetCommandName() }\n"
    )
    proc = subprocess.run(
        [safe_exec._powershell_path(), "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(parser.encode("utf-16-le")).decode("ascii")],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert not [n for n in names if n.startswith("PARSE-ERROR")], names
    assert names, "parser found no commands"
    allowed = {v.lower() for v in safe_exec.ALLOWED_VERBS}
    bad = [n for n in names if "-" not in n or n.split("-", 1)[0].lower() not in allowed]
    assert not bad, f"{key}: commands outside the read-only allowlist: {bad}"


def test_authenticode_on_powershell_itself():
    rows = safe_exec.run("authenticode", stdin_obj=[safe_exec._powershell_path()])
    assert rows and rows[0]["Status"] in {"Valid", "NotSigned", "UnknownError", "HashMismatch", "NotTrusted"}
