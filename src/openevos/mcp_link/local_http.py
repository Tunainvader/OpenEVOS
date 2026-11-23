"""Local Streamable HTTP endpoint for the MCP link (ADR-0054).

Loopback by default, bearer token, Host/Origin checks:

* binds 127.0.0.1; with an explicit allowlist of remote client IPs (`allow_clients`, the app's --mcp-allow) it binds
  all interfaces and answers only loopback and those IPs (403 for anyone else) - for an assistant on another PC;
* every request needs ``Authorization: Bearer <token>`` (constant-time compare), else 401;
* the SDK's transport security rejects a foreign Host (421) and any browser Origin (403), which
  stops a web page from reaching the endpoint by DNS rebinding.

The server runs uvicorn in a daemon thread so the Qt app (or the CLI) keeps its own main loop.
"""

from __future__ import annotations

import hmac
import os
import secrets
import socket
import threading
import time
from pathlib import Path

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_FILE = "mcp_token"


def token_path() -> Path:
    from platformdirs import user_config_dir

    return Path(user_config_dir("openevos", appauthor=False)) / TOKEN_FILE


def load_or_create_token(path: Path | None = None) -> str:
    """The token Claude Code is given (`--header "Authorization: Bearer ..."`). Created once, owner-only."""
    path = path or token_path()
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    return tok


class BearerAuth:
    """ASGI middleware: 401 unless the request carries the expected bearer token."""

    def __init__(self, app, token: str):
        if len(token) < 16:
            raise ValueError("token too short")
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self._expected):
                # read the request body before answering: replying early leaves unread bytes in the socket, and
                # Windows then resets the connection, so the client sees ConnectionReset instead of the 401
                while True:
                    msg = await receive()
                    if msg["type"] != "http.request" or not msg.get("more_body"):
                        break
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"www-authenticate", b'Bearer realm="openevos"')]})
                await send({"type": "http.response.body", "body": b"missing or wrong bearer token"})
                return
        await self.app(scope, receive, send)


class ClientAllowlist:
    """ASGI middleware: 403 unless the peer is loopback or one of the allowed remote IPs."""

    def __init__(self, app, clients):
        self.app = app
        self._ok = {LOOPBACK, "::1", *clients}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope.get("client") or ("",))[0] not in self._ok:
            while True:
                msg = await receive()
                if msg["type"] != "http.request" or not msg.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"client address not allowed"})
            return
        await self.app(scope, receive, send)


def local_addresses() -> list[str]:
    """This PC's IPv4 addresses and host name (what a remote client puts in its Host header)."""
    name = socket.gethostname()
    try:
        ips = {str(ai[4][0]) for ai in socket.getaddrinfo(name, None, socket.AF_INET)}
    except OSError:
        ips = set()
    return sorted(ips - {LOOPBACK}) + [name]


def build_asgi(mcp, *, port: int, token: str, path: str = "/mcp", allow_clients: tuple[str, ...] = ()):
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [f"{LOOPBACK}:{port}", f"localhost:{port}"]
    if allow_clients:
        hosts += [f"{a}:{port}" for a in local_addresses()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[],  # no browser origin is ever allowed; native clients send none
    )
    app = mcp.streamable_http_app(streamable_http_path=path, stateless_http=True, json_response=True,
                                  transport_security=security, host=LOOPBACK)
    app = BearerAuth(app, token)
    return ClientAllowlist(app, allow_clients) if allow_clients else app


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


class LocalMcpService:
    """Run an MCPServer on http://127.0.0.1:<port>/mcp in a background thread."""

    def __init__(self, mcp, *, port: int = DEFAULT_PORT, token: str, host: str = LOOPBACK,
                 path: str = "/mcp", allow_clients: tuple[str, ...] = ()):
        if host != LOOPBACK:
            raise ValueError("the MCP link binds to 127.0.0.1, or all interfaces with allow_clients (ADR-0054)")
        import ipaddress

        import uvicorn

        allow_clients = tuple(str(ipaddress.ip_address(a.strip())) for a in allow_clients)  # IPs only, no ranges
        bind = "0.0.0.0" if allow_clients else LOOPBACK  # only allowlisted IPs are answered
        self.port = port or free_port()
        self.path = path
        self.url = f"http://{LOOPBACK}:{self.port}{path}"
        self.remote_urls = [f"http://{a}:{self.port}{path}" for a in local_addresses()] if allow_clients else []
        app = build_asgi(mcp, port=self.port, token=token, path=path, allow_clients=allow_clients)
        # use_colors=False: otherwise uvicorn's log formatter calls sys.stdout.isatty(), and under pythonw (the Desktop
        # shortcut) sys.stdout is None, so the app with --mcp exited at start (2026-11-23)
        config = uvicorn.Config(app, host=bind, port=self.port, log_level="warning", lifespan="on",
                                access_log=False, use_colors=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="openevos-mcp", daemon=True)

    def start(self, timeout_s: float = 10.0) -> LocalMcpService:
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"MCP link failed to start on {self.url} (port in use?)")
            time.sleep(0.02)
        return self

    def stop(self, timeout_s: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout_s)
