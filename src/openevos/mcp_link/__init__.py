"""MCP link: lets an AI assistant (Claude Code, Claude Desktop, ...) observe a running OpenEVOS session.

Phase 1 is READ-ONLY by construction (ADR-0003, plan §13.3; ADR-0054):

* `view.InstrumentView` is the only object the MCP tools can reach. It holds the session and
  calls read methods only; `tests/test_mcp_link.py` scans this package's source and fails on any
  call that changes instrument state (execute, submit, stop, move, set_*, acquire, ...).
* No tool publishes a command; the test also asserts no command lifecycle event appears while
  every tool runs.
* The HTTP endpoint binds to 127.0.0.1 only, requires a bearer token and rejects foreign
  Host/Origin headers (DNS-rebinding protection).

Modules:
    view    read-only facade over `acquisition.session.Session` (Qt-free)
    server  the MCP tools, built on the official `mcp` SDK (v2 `MCPServer`)
    http    local Streamable HTTP service with token auth, run in a daemon thread
    cli     `openevos-mcp`: headless simulator server (stdio or HTTP) for development and CI
"""

from openevos.mcp_link.view import InstrumentView

__all__ = ["InstrumentView"]
