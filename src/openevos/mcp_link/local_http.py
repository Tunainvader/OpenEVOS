"""Local Streamable HTTP endpoint for the MCP link (ADR-0054).

Loopback only, bearer token, Host/Origin checks:

* binds 127.0.0.1; any other host is refused at construction;
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


def build_asgi(mcp, *, port: int, token: str, path: str = "/mcp"):
    from mcp.server.transport_security import TransportSecuritySettings

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{LOOPBACK}:{port}", f"localhost:{port}"],
        allowed_origins=[],  # no browser origin is ever allowed; native clients send none
    )
    app = mcp.streamable_http_app(streamable_http_path=path, stateless_http=True, json_response=True,
                                  transport_security=security, host=LOOPBACK)
    return BearerAuth(app, token)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


class LocalMcpService:
    """Run an MCPServer on http://127.0.0.1:<port>/mcp in a background thread."""

    def __init__(self, mcp, *, port: int = DEFAULT_PORT, token: str, host: str = LOOPBACK,
                 path: str = "/mcp"):
        if host != LOOPBACK:
            raise ValueError("the MCP link binds to 127.0.0.1 only (ADR-0054)")
        import uvicorn

        self.port = port or free_port()
        self.path = path
        self.url = f"http://{LOOPBACK}:{self.port}{path}"
        app = build_asgi(mcp, port=self.port, token=token, path=path)
        config = uvicorn.Config(app, host=LOOPBACK, port=self.port, log_level="warning", lifespan="on",
                                access_log=False)
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
