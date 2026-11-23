"""`openevos-mcp`: a headless, simulator-only OpenEVOS session behind the read-only MCP link.

For trying the tools and for CI. It never opens hardware: cameras, LEDs, stage and focus are all
simulated, and every reported value says so (`synthetic: true`). To observe the real instrument, run
the app with `openevos --mcp` instead, so there is still exactly one owner of the hardware.

    openevos-mcp                      # stdio (Claude Desktop config, `claude mcp add ... -- openevos-mcp`)
    openevos-mcp --http --port 8765   # loopback HTTP with a bearer token (prints the connect command)
    openevos-mcp --http --relay https://openevos-relay.<you>.workers.dev   # also reachable through a relay (ADR-0062)
    openevos-mcp --relay-secret       # print the device secret to give the relay (creates it once)
    openevos-mcp --relay-revoke URL   # remove every sign-in the relay has granted
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


def start_for_session(session, *, port: int, allow_clients: tuple[str, ...] = (), relay: str | None = None,
                      relay_listener=None):
    """Start the HTTP link for an existing session (used by the app), and with `relay` the outbound connection to an
    OpenEVOS relay (ADR-0062). Returns (service, view, hint, relay_client or None)."""
    from openevos.mcp_link.local_http import LocalMcpService, load_or_create_token
    from openevos.mcp_link.server import build_server
    from openevos.mcp_link.view import InstrumentView

    view = InstrumentView(session)
    svc = None
    try:
        token = load_or_create_token()
        svc = LocalMcpService(build_server(view, version=__version__), port=port, token=token,
                              allow_clients=allow_clients).start()
        hint = connect_hint(svc.url, token, svc.remote_urls)
        client = None
        if relay:
            from openevos.mcp_link.relay_client import RelayClient, load_or_create_secret, secret_path

            secret, created = load_or_create_secret()
            client = RelayClient(relay, secret, local_url=svc.url, local_token=token, version=__version__,
                                 listener=relay_listener).start()
            hint += (f"\n  relay: {client.mcp_url} (add it in Claude as a custom connector; sign in with the code "
                     f"OpenEVOS shows)\n  device secret: {secret_path()}"
                     + (" (new: give it to the relay with `wrangler secret put DEVICE_SECRET`)" if created else ""))
    except BaseException:
        if svc is not None:
            svc.stop()
        view.close()  # a link that did not start leaves nothing listening to the session
        raise
    return svc, view, hint, client


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openevos-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true", help="serve loopback Streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8765, help="HTTP port on 127.0.0.1 (default 8765; 0 = any free)")
    ap.add_argument("--fps", type=float, default=5.0, help="simulated preview rate")
    ap.add_argument("--relay", metavar="URL", help="with --http: also connect out to this OpenEVOS relay (ADR-0062)")
    ap.add_argument("--relay-secret", action="store_true",
                    help="print the device secret for the relay (`wrangler secret put DEVICE_SECRET`) and exit")
    ap.add_argument("--relay-revoke", metavar="URL", help="remove every sign-in this relay has granted, and exit")
    args = ap.parse_args(argv)

    if args.relay_secret or args.relay_revoke:
        from openevos.mcp_link.relay_client import load_or_create_secret, revoke_all, secret_path

        secret, created = load_or_create_secret()
        if args.relay_secret:
            print(secret)
            print(f"({'created' if created else 'stored'} in {secret_path()}; keep it private)", file=sys.stderr)
        if args.relay_revoke:
            print(f"revoked {revoke_all(args.relay_revoke, secret)} sign-in(s)", file=sys.stderr)
        return 0
    if args.relay and not args.http:
        ap.error("--relay needs --http")

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

        def show(kind: str, detail: str) -> None:
            print(f"relay {kind}: {detail}", file=sys.stderr, flush=True)

        svc, view, hint, relay = start_for_session(session, port=args.port, relay=args.relay, relay_listener=show)
        print(hint, file=sys.stderr, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            if relay is not None:
                relay.stop()
            svc.stop()
            view.close()
        return 0
    finally:
        session.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
