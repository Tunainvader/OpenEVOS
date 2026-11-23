"""The Desktop shortcut runs the app under pythonw, where sys.stdout and sys.stderr are None. On 2026-11-23 the
shortcut with --mcp exited two seconds after opening COM3, with no message: uvicorn's log formatter called
sys.stdout.isatty(). Simulated hardware, temporary files, loopback only."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.mcp_link.server import build_server
from openevos.mcp_link.view import InstrumentView

TOKEN = "t" * 43


@contextmanager
def no_console():
    """pythonw's streams, inside the test body: pytest's capture puts its own back at each phase, so a fixture
    setting them would be undone before the test runs."""
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = None
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=30)
    yield s
    s.shutdown()


def test_the_mcp_link_starts_without_console_streams(sim):
    from openevos.mcp_link.local_http import LocalMcpService

    view = InstrumentView(sim)
    try:
        with no_console():
            assert sys.stdout is None and sys.stderr is None
            svc = LocalMcpService(build_server(view), port=0, token=TOKEN).start()
        svc.stop()
    finally:
        view.close()


def test_console_output_goes_to_a_log_file_under_pythonw(tmp_path):
    from openevos.ui.app import _keep_console_output

    with no_console():
        path = _keep_console_output(tmp_path / "ui_settings.json")
        stream = sys.stdout
        try:
            assert stream is not None and stream is sys.stderr
            print("launch note")
            print("a startup traceback", file=sys.stderr)
        finally:
            if stream is not None:
                stream.close()
    assert path is not None and path.parent == tmp_path / "Logs" and path.name.startswith("console-")
    text = path.read_text("utf-8")
    assert "pid" in text and "launch note" in text and "a startup traceback" in text


def test_a_console_is_left_alone(tmp_path):
    from openevos.ui.app import _keep_console_output

    before = (sys.stdout, sys.stderr)
    assert _keep_console_output(tmp_path / "ui_settings.json") is None
    assert (sys.stdout, sys.stderr) == before
    assert not (tmp_path / "Logs").exists()


def test_an_mcp_link_that_cannot_start_does_not_stop_the_app(sim, tmp_path, monkeypatch, capsys):
    from openevos.acquisition.session_log import SessionLog
    from openevos.mcp_link import cli
    from openevos.ui.app import _start_mcp_link

    def broken(session, *, port, **_options):  # allow_clients etc. (--mcp-allow, fdce3ea)
        raise RuntimeError(f"MCP link failed to start on http://127.0.0.1:{port}/mcp (port in use?)")

    monkeypatch.setattr(cli, "start_for_session", broken)
    log = SessionLog(sim.core.bus, tmp_path)
    try:
        link, note = _start_mcp_link(sim, 8765, fault_log=log)
    finally:
        log.close()
    assert link is None and note == "MCP link not started: MCP link failed to start on " \
                                    "http://127.0.0.1:8765/mcp (port in use?)"
    assert note in capsys.readouterr().err
    rows = [json.loads(line) for line in log.path.read_text("utf-8").splitlines()]
    assert {"kind": "launch", "step": "mcp", "message": note}.items() <= next(
        r for r in rows if r.get("step") == "mcp").items()


def test_the_token_stays_out_of_a_console_log(monkeypatch, capsys):
    from openevos.mcp_link import cli
    from openevos.ui.app import _start_mcp_link

    link = (SimpleNamespace(url="http://127.0.0.1:8765/mcp"), object(), f'--header "Authorization: Bearer {TOKEN}"')
    monkeypatch.setattr(cli, "start_for_session", lambda session, *, port, **_options: link)
    assert _start_mcp_link(None, 8765, show_token=False) == (link, None)
    err = capsys.readouterr().err
    assert "http://127.0.0.1:8765/mcp" in err and TOKEN not in err
    _start_mcp_link(None, 8765)  # a console (python.exe) still gets the connect command
    assert TOKEN in capsys.readouterr().err


def test_a_link_that_did_not_start_closes_its_view(sim, tmp_path, monkeypatch):
    from openevos.mcp_link import cli, local_http

    closed = []
    real_close = InstrumentView.close

    def close(self):
        closed.append(self)
        real_close(self)

    def refuse(self, timeout_s=10.0):
        raise RuntimeError("MCP link failed to start (port in use?)")

    monkeypatch.setattr(local_http, "token_path", lambda: tmp_path / "mcp_token")  # never the operator's token
    monkeypatch.setattr(InstrumentView, "close", close)
    monkeypatch.setattr(local_http.LocalMcpService, "start", refuse)
    with pytest.raises(RuntimeError, match="port in use"):
        cli.start_for_session(sim, port=0)
    assert len(closed) == 1
