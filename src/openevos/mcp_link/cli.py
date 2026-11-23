"""`openevos-mcp`: a headless, simulator-only OpenEVOS session behind the read-only MCP link.

For trying the tools and for CI. It never opens hardware: cameras, LEDs, stage and focus are all
simulated, and every reported value says so (`synthetic: true`). To observe the real instrument, run
the app with `openevos --mcp` instead, so there is still exactly one owner of the hardware.

    openevos-mcp                      # stdio (Claude Desktop config, `claude mcp add ... -- openevos-mcp`)
    openevos-mcp --http --port 8765   # loopback HTTP with a bearer token (prints the connect command)
"""

from __future__ import annotations

import argparse
import sys

from openevos import __version__


def connect_hint(url: str, token: str, remote_urls: list[str] | None = None) -> str:
    from openevos.mcp_link.local_http import token_path

    remote = "".join(f"  from the allowed PCs: {u}\n" for u in remote_urls or [])
    return (f"OpenEVOS MCP link (read-only) at {url}\n{remote}"
            f"  Claude Code:  claude mcp add --transport http openevos {url} "
            f'--header "Authorization: Bearer {token}"\n'
            f"  token file: {token_path()} (keep it private; delete it to issue a new token)")


def start_for_session(session, *, port: int, allow_clients: tuple[str, ...] = ()):
    """Start the HTTP link for an existing session (used by the app). Returns (service, view, hint)."""
    from openevos.mcp_link.local_http import LocalMcpService, load_or_create_token
    from openevos.mcp_link.server import build_server
    from openevos.mcp_link.view import InstrumentView

    view = InstrumentView(session)
    try:
        token = load_or_create_token()
        svc = LocalMcpService(build_server(view, version=__version__), port=port, token=token,
                              allow_clients=allow_clients).start()
    except BaseException:
        view.close()  # a link that did not start leaves nothing listening to the session
        raise
    return svc, view, connect_hint(svc.url, token, svc.remote_urls)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openevos-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true", help="serve loopback Streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8765, help="HTTP port on 127.0.0.1 (default 8765; 0 = any free)")
    ap.add_argument("--fps", type=float, default=5.0, help="simulated preview rate")
    args = ap.parse_args(argv)

    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.mcp_link.server import build_server
    from openevos.mcp_link.view import InstrumentView

    # simulator only: no settings file, no telemetry port, simulated cameras and light
    session = build_sim_session(ThreadClock(), fps=args.fps, cameras=None, illumination=None)
    try:
        if not args.http:
            import anyio

            view = InstrumentView(session)
            try:
                anyio.run(build_server(view, version=__version__).run_stdio_async)
            finally:
                view.close()
            return 0
        import threading

        svc, view, hint = start_for_session(session, port=args.port)
        print(hint, file=sys.stderr, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            svc.stop()
            view.close()
        return 0
    finally:
        session.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
