"""The relay connection (ADR-0062): the app dials out to a relay and serves MCP requests from its loopback link.

A small fake relay (a local WebSocket server speaking the same frames as relay/src/index.ts) stands in for Cloudflare.
Simulator only; the token and the device secret live in a temporary folder.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("websockets")

from websockets.asyncio.server import serve

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.mcp_link import cli, local_http, relay_client
from openevos.mcp_link.relay_client import CHUNK, RelayClient, connect_url, mcp_url

SECRET = "s" * 43


class FakeRelay:
    """A relay on ws://127.0.0.1:<port>/connect that the test drives frame by frame."""

    def __init__(self, secret: str = SECRET):
        self.secret = secret
        self.connections = 0
        self.frames: list[dict] = []
        self._ws = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._replies: dict[str, list[dict]] = {}
        self._done: dict[str, threading.Event] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        def check(connection, request):
            if request.headers.get("Authorization") != f"Bearer {self.secret}":
                return connection.respond(401, "wrong device secret\n")
            return None

        async def handler(ws):
            self.connections += 1
            self._ws = ws
            async for raw in ws:
                m = json.loads(raw)
                self.frames.append(m)
                if m.get("t") == "res":
                    self._replies.setdefault(m["id"], []).append(m)
                    if m["last"]:
                        self._done.setdefault(m["id"], threading.Event()).set()

        async with serve(handler, "127.0.0.1", 0, process_request=check) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._stop = asyncio.Event()
            self._ready.set()
            await self._stop.wait()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def send(self, frame: dict) -> None:
        assert wait_for(lambda: self._ws is not None), "the app never connected"
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(frame)), self._loop).result(5)

    def request(self, body: dict | list, rid: str, *, chunk: int | None = None,
                timeout: float = 20) -> tuple[int, dict]:
        raw = json.dumps(body).encode()
        size = chunk or len(raw) or 1
        parts = [raw[i:i + size] for i in range(0, len(raw), size)]
        self._done[rid] = threading.Event()
        for seq, part in enumerate(parts):
            frame = {"t": "req", "id": rid, "seq": seq, "last": seq == len(parts) - 1,
                     "data": base64.b64encode(part).decode()}
            if seq == 0:
                frame["headers"] = {"content-type": "application/json", "accept": "application/json, text/event-stream",
                                    "mcp-protocol-version": "2025-06-18"}
            self.send(frame)
        assert self._done[rid].wait(timeout), "no reply"
        frames = self._replies[rid]
        body_bytes = b"".join(base64.b64decode(f["data"]) for f in frames)
        return frames[0]["status"], (json.loads(body_bytes) if body_bytes else {})

    def drop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._ws.close(1012, "restart"), self._loop).result(5)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(local_http, "token_path", lambda: tmp_path / "mcp_token")
    monkeypatch.setattr(relay_client, "secret_path", lambda: tmp_path / "mcp_relay_secret")
    (tmp_path / "mcp_relay_secret").write_text(SECRET + "\n")
    return tmp_path


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fps=5.0, cameras=None, illumination=None)
    yield s
    s.shutdown()


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


# ------------------------------------------------------------------------------------------------ URLs
def test_relay_urls():
    assert connect_url("https://openevos-relay.me.workers.dev/") == "wss://openevos-relay.me.workers.dev/connect"
    assert mcp_url("https://openevos-relay.me.workers.dev") == "https://openevos-relay.me.workers.dev/mcp"
    assert connect_url("http://localhost:8787") == "ws://localhost:8787/connect"
    with pytest.raises(ValueError, match="https"):
        connect_url("http://relay.example.org")  # the device secret never crosses a network in clear text
    with pytest.raises(ValueError):
        connect_url("ftp://relay.example.org")


def test_secret_is_created_once_and_private(tmp_path):
    p = tmp_path / "sub" / "secret"
    s1, created = relay_client.load_or_create_secret(p)
    s2, again = relay_client.load_or_create_secret(p)
    assert created and not again and s1 == s2 and len(s1) >= 32
    if os.name == "posix":
        assert (p.stat().st_mode & 0o777) == 0o600


# ------------------------------------------------------------------------------------------------ end to end
def test_requests_through_the_relay_reach_the_read_only_link(private, sim):
    relay = FakeRelay()
    events: list[tuple[str, str]] = []
    svc, view, hint, client = cli.start_for_session(sim, port=0, relay=relay.url,
                                                    relay_listener=lambda k, d: events.append((k, d)))
    try:
        assert client is not None and relay_client.wait_connected(client, 10)
        assert wait_for(lambda: any(f.get("t") == "hello" for f in relay.frames))
        assert "relay:" in hint and client.mcp_url in hint

        status, init = relay.request(INIT, "r1")
        assert status == 200 and init["result"]["serverInfo"]["name"] == "openevos"

        status, tools = relay.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "r2")
        names = {t["name"] for t in tools["result"]["tools"]}
        assert {"describe_api", "get_state", "get_image"} <= names
        assert all(t["annotations"]["readOnlyHint"] for t in tools["result"]["tools"])

        status, state = relay.request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "get_state", "arguments": {}}}, "r3", chunk=40)
        assert status == 200 and not state["result"].get("isError")

        # a tool that does not exist is still a tool error from the link, not something the relay can add
        status, bad = relay.request({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "move_stage", "arguments": {"x_um": 1}}}, "r4")
        assert bad.get("error") or bad["result"].get("isError")

        calls = [d for k, d in events if k == "call"]
        assert "tools/call get_state" in calls and "initialize" in calls
        assert not any("x_um" in d for _, d in events)  # arguments never reach the log
    finally:
        client.stop()
        svc.stop()
        view.close()
        relay.close()


def test_large_replies_are_split_into_frames(private, sim, monkeypatch):
    monkeypatch.setattr(relay_client, "CHUNK", 64 * 1024)  # small frames, so one image needs several
    relay = FakeRelay()
    svc, view, _hint, client = cli.start_for_session(sim, port=0, relay=relay.url)
    try:
        assert relay_client.wait_connected(client, 10)
        relay.request(INIT, "i")
        sim.capture.capture("trans")  # the operator's Capture button (simulated camera and light)
        _status, reply = relay.request({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                        "params": {"name": "get_image", "arguments": {"source": "trans",
                                                                                      "max_px": 1024}}}, "img")
        frames = [f for f in relay.frames if f.get("id") == "img"]
        size = sum(len(base64.b64decode(f["data"])) for f in frames)
        assert not reply["result"].get("isError") and reply["result"]["content"][0]["type"] == "image"
        assert len(frames) == -(-size // (64 * 1024)) >= 2  # several frames, reassembled into one valid reply
    finally:
        client.stop()
        svc.stop()
        view.close()
        relay.close()


def test_reply_framing_round_trip():
    sent: list[str] = []

    class WS:
        async def send(self, s):
            sent.append(s)

    body = os.urandom(CHUNK * 2 + 123)
    asyncio.run(RelayClient._reply(WS(), "x", 200, {"content-type": "application/json"}, body))
    frames = [json.loads(s) for s in sent]
    assert [f["seq"] for f in frames] == [0, 1, 2] and [f["last"] for f in frames] == [False, False, True]
    assert frames[0]["status"] == 200 and "status" not in frames[1]
    assert all(len(f["data"]) < 1024 * 1024 for f in frames)  # under Cloudflare's 1 MiB message cap
    assert b"".join(base64.b64decode(f["data"]) for f in frames) == body
    sent.clear()
    asyncio.run(RelayClient._reply(WS(), "y", 202, {}, b""))
    assert len(sent) == 1 and json.loads(sent[0])["last"] is True


def test_sign_in_code_and_notices_reach_the_listener(private, sim):
    relay = FakeRelay()
    events: list[tuple[str, str]] = []
    svc, view, _hint, client = cli.start_for_session(sim, port=0, relay=relay.url,
                                                    relay_listener=lambda k, d: events.append((k, d)))
    try:
        assert relay_client.wait_connected(client, 10) and wait_for(lambda: relay._ws is not None)
        relay.send({"t": "pair_code", "code": "12345678", "expires_s": 300, "client": "Claude"})
        relay.send({"t": "notice", "event": "connected", "detail": "Claude was allowed to read OpenEVOS (by code)"})
        assert wait_for(lambda: any(k == "notice" for k, _ in events))
        code = next(d for k, d in events if k == "pair_code")
        assert code.startswith("1234 5678") and "Claude" in code and "5 min" in code
    finally:
        client.stop()
        svc.stop()
        view.close()
        relay.close()


def test_wrong_device_secret_is_reported_and_retried(private, sim):
    relay = FakeRelay(secret="t" * 43)
    events: list[tuple[str, str]] = []
    svc, view, _hint, client = cli.start_for_session(sim, port=0, relay=relay.url,
                                                    relay_listener=lambda k, d: events.append((k, d)))
    try:
        assert wait_for(lambda: any(k == "error" and "401" in d for k, d in events))
        assert not client.connected.is_set() and relay.connections == 0
    finally:
        client.stop()
        svc.stop()
        view.close()
        relay.close()


def test_reconnects_after_the_relay_drops(private, sim, monkeypatch):
    monkeypatch.setattr(relay_client, "BACKOFF_S", (0.2,))
    relay = FakeRelay()
    svc, view, _hint, client = cli.start_for_session(sim, port=0, relay=relay.url)
    try:
        assert wait_for(lambda: relay.connections == 1)
        relay.drop()
        assert wait_for(lambda: relay.connections == 2)
        assert relay.request(INIT, "again")[0] == 200
    finally:
        client.stop()
        svc.stop()
        view.close()
        relay.close()


def test_stop_ends_the_thread(private, sim):
    relay = FakeRelay()
    svc, view, _hint, client = cli.start_for_session(sim, port=0, relay=relay.url)
    try:
        assert relay_client.wait_connected(client, 10)
        client.stop()
        assert not client._thread.is_alive()
    finally:
        svc.stop()
        view.close()
        relay.close()


# ------------------------------------------------------------------------------------------------ boundaries
def test_relay_client_never_touches_the_session():
    """The relay path only speaks HTTP to the loopback link: no session, controller, gateway or view import."""
    src = Path(relay_client.__file__).read_text("utf-8")
    imports = {n.module or "" for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imports if m.startswith("openevos")}


def test_app_relay_log_waits_for_the_window(tmp_path):
    from openevos.ui.app import _RelayLog

    rows: list[dict] = []
    log = _RelayLog(SimpleNamespace(write=lambda kind, **d: rows.append({"kind": kind, **d})))
    log("pair_code", "1234 5678 (for Claude, valid 5 min)")
    log("connected", "https://r.example/mcp")
    shown: list[tuple[str, str]] = []
    model = SimpleNamespace(dispatcher=lambda fn: fn(), add_log=lambda level, text: shown.append((level, text)))
    log.attach(model)
    log("call", "tools/call get_state")
    assert shown[0] == ("warn", ("Relay sign-in code: 1234 5678 (for Claude, valid 5 min). Type it on the page Claude "
                                "opened."))
    assert shown[-1] == ("info", "Relay request: tools/call get_state")
    assert [r["event"] for r in rows] == ["connected"]  # codes never go to the fault log
