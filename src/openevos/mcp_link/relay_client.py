"""Outbound connection from the app to an OpenEVOS relay (ADR-0062).

The app dials out to the relay over a WebSocket (no open port on the microscope PC) and authenticates with the device
secret. Each MCP request the relay passes on is sent, unchanged, to the app's own loopback link
(`http://127.0.0.1:PORT/mcp`, ADR-0054) with the link's bearer token, and the reply goes back the same way. So the
relay reaches exactly what a local client reaches: the read-only tools, behind the same token and Host checks.

Frames (JSON text; bodies base64, at most 512 KiB of raw bytes per frame, because Cloudflare caps a message at 1 MiB):

    relay -> app   {"t": "req", "id", "seq", "last", "data", "headers"?}    an MCP request, in one or more frames
    app -> relay   {"t": "res", "id", "seq", "last", "data", "status"?, "headers"?}
    app -> relay   {"t": "hello", "version", "link"}                         once per connection
    relay -> app   {"t": "pair_code", "code", "expires_s", "client"}         someone is signing in: show the code
    relay -> app   {"t": "notice", "event", "detail"}                        e.g. an app was allowed
    app -> relay   {"t": "ping"}   relay -> app {"t": "pong"}                keep-alive
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_log = logging.getLogger(__name__)
CHUNK = 512 * 1024
SECRET_FILE = "mcp_relay_secret"
FORWARD = ("content-type", "accept", "mcp-protocol-version", "mcp-session-id", "last-event-id")
RETURN = ("content-type", "mcp-session-id")
PING_S = 25.0
BACKOFF_S = (1, 2, 5, 10, 30, 60)

# what the client reports to the app: ("pair_code", "1234 5678 ..."), ("connected", url), ("call", "tools/call probe"),
# ("disconnected", reason), ("notice", detail), ("error", message)
Listener = Callable[[str, str], None]


def secret_path() -> Path:
    from platformdirs import user_config_dir

    return Path(user_config_dir("openevos", appauthor=False)) / SECRET_FILE


def load_or_create_secret(path: Path | None = None) -> tuple[str, bool]:
    """The device secret shared with the relay (`wrangler secret put DEVICE_SECRET`). Owner-only. (secret, created)"""
    path = path or secret_path()
    if path.exists():
        s = path.read_text(encoding="utf-8").strip()
        if len(s) >= 32:
            return s, False
    path.parent.mkdir(parents=True, exist_ok=True)
    s = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(s + "\n")
    return s, True


def connect_url(relay: str) -> str:
    """https://relay.example/ -> wss://relay.example/connect. Plain ws/http only for this PC (development)."""
    u = urlsplit(relay.strip())
    host = (u.hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1")
    if u.scheme in ("https", "wss"):
        scheme = "wss"
    elif u.scheme in ("http", "ws") and local:
        scheme = "ws"
    else:
        raise ValueError(f"relay URL must be https:// (got {relay!r}); plain http only for localhost testing")
    if not host:
        raise ValueError(f"relay URL has no host: {relay!r}")
    return urlunsplit((scheme, u.netloc, "/connect", "", ""))


def mcp_url(relay: str) -> str:
    """The URL to add as a custom connector in Claude."""
    u = urlsplit(connect_url(relay))
    return urlunsplit(("https" if u.scheme == "wss" else "http", u.netloc, "/mcp", "", ""))


def _describe(body: bytes) -> str:
    """'tools/call get_state' for the app's log; never the arguments."""
    try:
        msg = json.loads(body)
    except ValueError:
        return "unreadable request"
    items = msg if isinstance(msg, list) else [msg]
    out = []
    for m in items[:5]:
        if not isinstance(m, dict):
            continue
        method = str(m.get("method", "?"))
        if method == "tools/call":
            method += " " + str((m.get("params") or {}).get("name", "?"))[:64]
        out.append(method)
    return ", ".join(out) or "empty request"


@dataclass
class _Incoming:
    parts: list[bytes] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


class RelayClient:
    """Keep a WebSocket to the relay open in a background thread and serve the requests that arrive on it."""

    def __init__(self, relay: str, secret: str, *, local_url: str, local_token: str, version: str = "",
                 listener: Listener | None = None, timeout_s: float = 55.0):
        import websockets  # noqa: F401  - fail at construction, not in the thread, when the extra is missing

        if len(secret) < 32:
            raise ValueError("device secret too short")
        self.url = connect_url(relay)
        self.mcp_url = mcp_url(relay)
        self._secret = secret
        self._local_url = local_url
        self._local_token = local_token
        self._version = version
        self._listen = listener or (lambda kind, detail: None)
        self._timeout = timeout_s
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None  # the open websockets ClientConnection, while connected
        self.connected = threading.Event()
        self.calls = 0
        self._thread = threading.Thread(target=self._run, name="openevos-mcp-relay", daemon=True)

    # ---------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> RelayClient:
        self._thread.start()
        return self

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            asyncio.run_coroutine_threadsafe(ws.close(1000, "OpenEVOS closing"), loop)
        self._thread.join(timeout_s)

    def _emit(self, kind: str, detail: str) -> None:
        try:
            self._listen(kind, detail)
        except Exception:  # noqa: BLE001 - a broken listener must not drop the connection
            _log.exception("relay listener failed on %s", kind)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _main(self) -> None:
        import websockets

        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, additional_headers={"Authorization": f"Bearer {self._secret}"},
                                              max_size=2 * 1024 * 1024, ping_interval=None,
                                              open_timeout=15, close_timeout=3) as ws:
                    self._ws = ws
                    attempt = 0
                    self.connected.set()
                    self._emit("connected", self.mcp_url)
                    await ws.send(json.dumps({"t": "hello", "version": self._version, "link": "read-only"}))
                    await self._serve(ws)
            except Exception as exc:  # noqa: BLE001 - network trouble of any kind: report, wait, retry
                if self._stop.is_set():
                    break
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 401:
                    self._emit("error", "relay refused the device secret (401): check DEVICE_SECRET")
                else:
                    self._emit("disconnected", str(exc)[:200] or type(exc).__name__)
            finally:
                self._ws = None
                self.connected.clear()
            if self._stop.is_set():
                break
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] * (0.75 + 0.5 * secrets.randbelow(1000) / 1000)
            attempt += 1
            await asyncio.to_thread(self._stop.wait, delay)

    async def _serve(self, ws) -> None:
        pending: dict[str, _Incoming] = {}
        tasks: set[asyncio.Task] = set()

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(PING_S)
                await ws.send('{"t":"ping"}')

        ka = asyncio.create_task(keepalive())
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                t = m.get("t")
                if t == "req" and isinstance(m.get("id"), str):
                    inc = pending.setdefault(m["id"], _Incoming())
                    if m.get("seq") == 0:
                        inc.headers = {k.lower(): str(v) for k, v in (m.get("headers") or {}).items()
                                       if k.lower() in FORWARD}
                    inc.parts.append(base64.b64decode(m.get("data") or ""))
                    if sum(len(p) for p in inc.parts) > 4 * 1024 * 1024:
                        pending.pop(m["id"], None)
                        await self._reply(ws, m["id"], 413, {"content-type": "text/plain"}, b"request too large")
                    elif m.get("last"):
                        pending.pop(m["id"], None)
                        task = asyncio.create_task(self._handle(ws, m["id"], inc))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                elif t == "pair_code":
                    code = str(m.get("code", ""))
                    shown = f"{code[:4]} {code[4:]}" if len(code) == 8 else code
                    self._emit("pair_code", f"{shown} (for {str(m.get('client', 'an app'))[:80]}, "
                                            f"valid {int(m.get('expires_s', 300)) // 60} min)")
                elif t == "notice":
                    self._emit("notice", str(m.get("detail", ""))[:200])
        finally:
            ka.cancel()
            for task in tasks:
                task.cancel()

    async def _handle(self, ws, rid: str, inc: _Incoming) -> None:
        body = b"".join(inc.parts)
        self.calls += 1
        self._emit("call", _describe(body))
        headers = {**inc.headers, "authorization": f"Bearer {self._local_token}"}
        headers.setdefault("content-type", "application/json")
        headers.setdefault("accept", "application/json, text/event-stream")
        try:
            status, out, content = await asyncio.to_thread(self._post, body, headers)
            await self._reply(ws, rid, status, out, content)
        except Exception as exc:  # noqa: BLE001 - the local link failing is reported to the relay, not raised
            await self._reply(ws, rid, 502, {"content-type": "text/plain"},
                              f"OpenEVOS link did not answer: {type(exc).__name__}".encode())

    def _post(self, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        """POST to the loopback link (standard library: no extra dependency). Any HTTP status is a reply."""
        req = urllib.request.Request(self._local_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return r.status, {k: r.headers[k] for k in RETURN if r.headers.get(k)}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k: e.headers[k] for k in RETURN if e.headers.get(k)}, e.read()

    @staticmethod
    async def _reply(ws, rid: str, status: int, headers: dict[str, str], body: bytes) -> None:
        parts = [body[i:i + CHUNK] for i in range(0, len(body), CHUNK)] or [b""]
        for seq, part in enumerate(parts):
            frame = {"t": "res", "id": rid, "seq": seq, "last": seq == len(parts) - 1,
                     "data": base64.b64encode(part).decode("ascii")}
            if seq == 0:
                frame.update(status=status, headers=headers)
            await ws.send(json.dumps(frame))


def revoke_all(relay: str, secret: str, *, timeout_s: float = 15.0) -> int:
    """Remove every sign-in the relay has granted. Returns how many."""
    u = urlsplit(connect_url(relay))
    url = urlunsplit(("https" if u.scheme == "wss" else "http", u.netloc, "/revoke", "", ""))
    req = urllib.request.Request(url, data=b"", headers={"Authorization": f"Bearer {secret}"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return int(json.loads(r.read()).get("revoked", 0))


def wait_connected(client: RelayClient, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.connected.wait(0.05):
            return True
    return False
