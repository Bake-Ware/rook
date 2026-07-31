"""FastMCP server exposing a Telesthete band as MCP tools.

Tools:
    rook_workers()                 — list workers seen on the band
    rook_caps()                    — list capabilities per worker
    rook_call(cap, args?, worker_id?, timeout?) — fire a capability call

Run:
    ROOK_BAND_PSK=mysecret python -m rook.band_mcp \\
        --hub 127.0.0.1:7474 --bind 127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .api_tokens_ui import build_api_token_routes
from .client import BandClient, MultiBandClient
from .tokens import StoreTokenVerifier, TokenStore

log = logging.getLogger("rook.band_mcp.server")


def build_server(client: "BandClient | MultiBandClient",
                 allowed_hosts: list[str] | None = None,
                 public_url: str | None = None,
                 admin_password: str | None = None,
                 persist_path: str | None = None,
                 static_token: str | None = None,
                 ) -> tuple[FastMCP, TokenStore]:
    """Build the FastMCP server. Bearer-token-only auth — no OAuth.

    The server validates ``Authorization: Bearer <token>`` against either a
    fixed ``static_token`` or a token minted through the password-gated
    ``/tokens`` admin page (``admin_password`` gates that page; tokens
    persist at ``persist_path``). No OAuth authorization-server routes
    (register/authorize/token) are ever mounted — clients just carry the
    header, no login/registration dance.
    """
    sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts or []) + [
            "127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765",
        ],
    )

    store = TokenStore(admin_password=admin_password, persist_path=persist_path,
                       static_token=static_token or None)
    token_verifier = StoreTokenVerifier(store)
    auth_settings: AuthSettings | None = None
    if public_url:
        # Resource-server metadata only (no authorization-server advertised).
        auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(public_url + "/mcp"),
            required_scopes=["rook"],
        )

    mcp = FastMCP(
        "rook-band",
        transport_security=sec,
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    @mcp.tool()
    async def rook_workers() -> str:
        """List all workers currently visible on the band.

        Returns a JSON array of objects: ``{worker_id, name, caps, plugins,
        last_seen_age_secs}``. Workers re-announce every 30s; entries are
        evicted after ~90s of silence.
        """
        import time
        now = time.time()
        out = []
        for w in client.workers.values():
            out.append({
                "worker_id": w["worker_id"],
                "name": w.get("name"),
                "band": w.get("band"),
                "caps": w.get("caps", []),
                "plugins": w.get("plugins", []),
                "last_seen_age_secs": round(now - w.get("last_seen", 0.0), 2),
            })
        out.sort(key=lambda x: x["name"] or "")
        return json.dumps(out, indent=2)

    @mcp.tool()
    async def rook_caps() -> str:
        """List all dot-namespaced capabilities seen on the band.

        Returns a JSON array of objects: ``{cap, workers}`` where ``workers``
        is the list of worker names that announced this capability.
        """
        by_cap: dict[str, list[str]] = {}
        for w in client.workers.values():
            name = w.get("name") or w["worker_id"]
            for cap in w.get("caps", []):
                by_cap.setdefault(cap, []).append(name)
        out = [{"cap": c, "workers": sorted(set(ws))}
               for c, ws in sorted(by_cap.items())]
        return json.dumps(out, indent=2)

    @mcp.tool()
    async def rook_call(cap: str, args: dict | None = None,
                        worker_id: str | None = None,
                        timeout: float = 15.0) -> str:
        """Invoke a capability on the band and return the reply.

        Args:
            cap: dot-namespaced capability name (e.g. ``"shell.exec"``).
            args: keyword arguments passed to the handler.
            worker_id: target a specific worker by id. If omitted, the first
                worker on the band that has the capability replies; if more
                than one worker has it, the first reply wins.
            timeout: seconds to wait for the reply.

        Returns the reply dict as JSON: either
        ``{"id","from","ok":true,"result":...}`` or
        ``{"id","from","ok":false,"error":"..."}``.
        """
        # Messages sent through the MCP identify their origin as "MCP" (unless
        # the caller set an explicit sender), so chat/notify show who's talking.
        if cap in ("chat.send", "msg.send"):
            args = dict(args or {})
            args.setdefault("sender", "MCP")
        reply = await client.call(cap=cap, args=args, target=worker_id,
                                  timeout=timeout)
        return json.dumps(reply, indent=2)

    return mcp, store


async def _amain(args) -> None:
    client = MultiBandClient(psks=args.psks, hub_host=args.hub_host,
                             hub_port=args.hub_port)
    await client.start()

    allowed_hosts = [h.strip() for h in (args.allowed_hosts or "").split(",")
                     if h.strip()]
    mcp, store = build_server(
        client,
        allowed_hosts=allowed_hosts,
        public_url=args.public_url,
        admin_password=args.admin_password,
        persist_path=args.persist_path,
        static_token=args.static_token or None,
    )
    app = mcp.streamable_http_app()

    # Wire up WS bridge for remote Telesthete Band workers.
    ws_bridge: WSBandBridge | None = None
    try:
        from .ws_band import WSBandBridge
        # The WS /band bridge forwards raw encrypted packets and is band-agnostic,
        # so a single bridge serves every band; the psk arg is unused.
        ws_bridge = WSBandBridge(app, args.hub_host, args.hub_port, args.psks[0])
        ws_bridge.start()
    except Exception as e:
        log.warning("WS band bridge failed to start: %s", e)

    # /tokens admin UI (mint/list/revoke bearer tokens). No OAuth routes are
    # ever mounted — FastMCP itself only registers MCP transport endpoints
    # since we passed token_verifier, not auth_server_provider.
    for r in reversed(build_api_token_routes(store)):
        app.router.routes.insert(0, r)

    import uvicorn
    config = uvicorn.Config(app, host=args.bind_host, port=args.bind_port,
                            log_level="info")
    server = uvicorn.Server(config)
    log.info("rook-band-mcp serving on http://%s:%d/mcp",
             args.bind_host, args.bind_port)
    try:
        await server.serve()
    finally:
        # Tear down WS bridge before stopping transport.
        if ws_bridge is not None:
            await ws_bridge.stop()
        await client.stop()


def main() -> None:
    ap = argparse.ArgumentParser(prog="rook-band-mcp")
    ap.add_argument("--hub", default="127.0.0.1:7474",
                    help="telesthete hub host:port")
    ap.add_argument("--psk", default=os.environ.get("ROOK_BAND_PSK"),
                    help="band pre-shared key (or env ROOK_BAND_PSK). Accepts a "
                         "comma-separated list to join several bands on one hub "
                         "at once — e.g. during a PSK rotation: --psk new,old")
    ap.add_argument("--bind", default="127.0.0.1:8765",
                    help="HTTP bind host:port for the MCP server")
    ap.add_argument("--allowed-hosts",
                    default=os.environ.get("ROOK_ALLOWED_HOSTS", ""),
                    help="comma-separated public Host headers to accept "
                         "(e.g. mcp.example.com). Loopback always allowed.")
    ap.add_argument("--public-url",
                    default=os.environ.get("ROOK_MCP_PUBLIC_URL", ""),
                    help="public https URL (e.g. https://mcp.example.com). "
                         "Advertised as resource-server metadata for MCP "
                         "clients — no OAuth authorization-server metadata "
                         "is ever advertised.")
    ap.add_argument("--admin-password",
                    default=os.environ.get("ROOK_MCP_AUTH_PASSWORD", ""),
                    help="admin password gating the /tokens mint/revoke UI.")
    ap.add_argument("--persist-path",
                    default=os.environ.get("ROOK_MCP_PERSIST",
                                            "/var/lib/rook-band-mcp/oauth.json"),
                    help="JSON file for persistent API tokens.")
    ap.add_argument("--static-token",
                    default=os.environ.get("ROOK_MCP_STATIC_TOKEN", ""),
                    help="fixed bearer token clients send as "
                         "'Authorization: Bearer <token>' — the simplest "
                         "single-integration auth path. Tokens minted via "
                         "/tokens also work at the same time.")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()

    if not args.psk:
        ap.error("--psk or env ROOK_BAND_PSK is required")
    # One or more PSKs (comma-separated) → one band each, all on the same hub.
    args.psks = [p.strip() for p in args.psk.split(",") if p.strip()]

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(level=max(level, logging.DEBUG),
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
                        stream=sys.stderr)

    hub_host, _, hub_port = args.hub.partition(":")
    bind_host, _, bind_port = args.bind.partition(":")
    args.hub_host = hub_host
    args.hub_port = int(hub_port or 7474)
    args.bind_host = bind_host
    args.bind_port = int(bind_port or 8765)

    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
