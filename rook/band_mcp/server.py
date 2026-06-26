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

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .api_tokens_ui import build_api_token_routes
from .client import BandClient
from .oauth import InMemoryProvider, StaticTokenVerifier, build_oauth_routes

log = logging.getLogger("rook.band_mcp.server")


def build_server(client: BandClient,
                 allowed_hosts: list[str] | None = None,
                 public_url: str | None = None,
                 auth_password: str | None = None,
                 persist_path: str | None = None,
                 preset_client_id: str | None = None,
                 preset_client_secret: str | None = None,
                 preset_redirect_uris: list[str] | None = None,
                 ) -> tuple[FastMCP, InMemoryProvider | None]:
    """Build the FastMCP server. If `auth_password` is set, OAuth is wired with
    persistence at `persist_path`. If `preset_client_id`/`preset_client_secret`
    are set, that client is pre-installed (claude.ai uses those creds instead
    of dynamic registration)."""
    sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts or []) + [
            "127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765",
        ],
    )

    provider: InMemoryProvider | None = None
    auth_settings: AuthSettings | None = None
    if public_url and auth_password:
        preset = None
        if preset_client_id and preset_client_secret:
            preset = (preset_client_id, preset_client_secret,
                      preset_redirect_uris or [
                          "https://claude.ai/api/mcp/auth_callback",
                          "https://claude.ai/api/oauth/callback",
                      ])
        provider = InMemoryProvider(
            auth_password=auth_password,
            persist_path=persist_path,
            preset_client=preset,
        )
        auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(public_url + "/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["rook"],
                default_scopes=["rook"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["rook"],
        )

    mcp = FastMCP(
        "rook-band",
        transport_security=sec,
        auth_server_provider=provider,
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
        reply = await client.call(cap=cap, args=args, target=worker_id,
                                  timeout=timeout)
        return json.dumps(reply, indent=2)

    return mcp, provider


async def _amain(args) -> None:
    client = BandClient(psk=args.psk, hub_host=args.hub_host,
                        hub_port=args.hub_port)
    await client.start()

    allowed_hosts = [h.strip() for h in (args.allowed_hosts or "").split(",")
                     if h.strip()]
    mcp, provider = build_server(
        client,
        allowed_hosts=allowed_hosts,
        public_url=args.public_url,
        auth_password=args.auth_password,
        persist_path=args.persist_path,
        preset_client_id=args.client_id,
        preset_client_secret=args.client_secret,
    )
    app = mcp.streamable_http_app()

    # Wire up WS bridge for remote Telesthete Band workers.
    ws_bridge: WSBandBridge | None = None
    try:
        from .ws_band import WSBandBridge
        ws_bridge = WSBandBridge(app, args.hub_host, args.hub_port, args.psk)
        ws_bridge.start()
    except Exception as e:
        log.warning("WS band bridge failed to start: %s", e)

    if provider is not None:
        # Prepend so our lenient /token and /oauth/authorize shadow MCP's.
        extras = build_oauth_routes(provider) + build_api_token_routes(provider)
        for r in reversed(extras):
            app.router.routes.insert(0, r)

        from starlette.types import ASGIApp, Receive, Scope, Send
        class _TokenDebug:
            """Log POST bodies + 4xx responses on /token to diagnose OAuth.

            Intentionally noisy. Strip once OAuth is healthy."""

            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] != "http" or scope.get("path") != "/token":
                    await self.app(scope, receive, send)
                    return
                req_body = bytearray()

                async def recv_wrap():
                    msg = await receive()
                    if msg["type"] == "http.request":
                        req_body.extend(msg.get("body", b""))
                    return msg

                resp_status = {"code": 0}
                resp_body = bytearray()

                async def send_wrap(msg):
                    if msg["type"] == "http.response.start":
                        resp_status["code"] = msg["status"]
                    elif msg["type"] == "http.response.body":
                        resp_body.extend(msg.get("body", b""))
                    await send(msg)

                await self.app(scope, recv_wrap, send_wrap)
                hdrs = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                authz = hdrs.get("authorization", "(none)")
                log.info("[/token] auth=%s body=%s status=%s resp=%s",
                         authz[:80], req_body.decode(errors="replace")[:400],
                         resp_status["code"], resp_body.decode(errors="replace")[:400])

        app = _TokenDebug(app)

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
                    help="band pre-shared key (or env ROOK_BAND_PSK)")
    ap.add_argument("--bind", default="127.0.0.1:8765",
                    help="HTTP bind host:port for the MCP server")
    ap.add_argument("--allowed-hosts",
                    default=os.environ.get("ROOK_ALLOWED_HOSTS", ""),
                    help="comma-separated public Host headers to accept "
                         "(e.g. mcp.example.com). Loopback always allowed.")
    ap.add_argument("--public-url",
                    default=os.environ.get("ROOK_MCP_PUBLIC_URL", ""),
                    help="public https URL (e.g. https://mcp.example.com). "
                         "Advertised in OAuth metadata for resource-server "
                         "discovery.")
    ap.add_argument("--auth-password",
                    default=os.environ.get("ROOK_MCP_AUTH_PASSWORD", ""),
                    help="admin password gating OAuth /authorize.")
    ap.add_argument("--persist-path",
                    default=os.environ.get("ROOK_MCP_PERSIST",
                                            "/var/lib/rook-band-mcp/oauth.json"),
                    help="JSON file for persistent OAuth clients + tokens.")
    ap.add_argument("--client-id",
                    default=os.environ.get("ROOK_MCP_CLIENT_ID", ""),
                    help="pre-installed OAuth client id (claude.ai paste-target).")
    ap.add_argument("--client-secret",
                    default=os.environ.get("ROOK_MCP_CLIENT_SECRET", ""),
                    help="pre-installed OAuth client secret.")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()

    if not args.psk:
        ap.error("--psk or env ROOK_BAND_PSK is required")

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
