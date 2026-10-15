"""``rook hub serve`` (``rook-hub``): the dashboard and the MCP server in one process.

A hub used to run two Python processes: the dashboard (``rook-dashboard``,
aiohttp, port 7005) and the MCP server (``rook-mcp``, FastMCP on uvicorn, port
8765), each with its own interpreter, band client, enrollment/account/chat
stores and authorizer, and the dashboard reaching the MCP side's account APIs
over loopback HTTP. On a small VM that is most of the memory. This runs both
in one process:

* one aiohttp server. The dashboard is the app; the MCP paths a public hub
  routes to the MCP server (:data:`PUBLIC_MCP_PATHS`: ``/mcp``, ``/band``,
  ``/tokens/account-api``, ``/authorize``, ``/token``, ``/.well-known/*``,
  ``/skill/*``, ``/healthz``) are handed to the FastMCP ASGI app through
  rook/hub/asgi_bridge.py before any dashboard middleware sees them, so one
  port serves the whole site with the same split a reverse proxy made.
* paths a hub plugin serves on the site (rook/hub/site.py: the voice
  plugin's ``/voice/*``) go to that plugin the same way, before dashboard
  middleware: they bring their own auth.
* optionally (``--mcp-bind``, default ``127.0.0.1:8765``, ``off`` to drop it)
  the old MCP port too, serving exactly what ``rook-mcp`` served there, so
  Cloudflare routes, LAN workers dialing ``ws://<hub>:8765/band`` and agent
  configs keep working unchanged.
* one band client (and its one authorizer), one enrollment store, one account
  store, one settings store and, when both sides point at the same file, one
  chat store, shared by both sides.
* the dashboard's proxies to ``/<x>/account-api`` and ``/internal/mask`` call
  the MCP app in memory (rook/remote/upstream.py) with the same request they
  sent over HTTP, so session, operator and CSRF checks are unchanged.

``rook-dashboard`` and ``rook-mcp`` still run as two processes (compat mode)
for existing deployments; see docs/operations/hub-deploy.md for the migration.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import uuid
from typing import Any

from aiohttp import web

log = logging.getLogger("rook.hub.serve")

# What a public hub's reverse proxy sends to the MCP server rather than the
# dashboard (README "Going beyond localhost"): exact paths, then prefixes.
# The other MCP-side routes (``/<x>/account-api`` but tokens, ``/internal/
# mask``) were only ever reachable on loopback; here the dashboard calls
# them in memory and the main port does not serve them.
PUBLIC_MCP_PATHS = ("/mcp", "/band", "/tokens/account-api", "/authorize", "/token",
                    "/healthz")
PUBLIC_MCP_PREFIXES = ("/mcp/", "/.well-known/", "/skill/")

DEFAULT_MCP_BIND = "127.0.0.1:8765"
SHUTDOWN_TIMEOUT = 5.0
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def is_mcp_path(path: str) -> bool:
    return path in PUBLIC_MCP_PATHS or path.startswith(PUBLIC_MCP_PREFIXES)


def _unrouted(request) -> bool:
    """True when the dashboard's router has no route for this path (any
    method): only then may a hub plugin's site mount answer it."""
    exc = getattr(request.match_info, "http_exception", None)
    return exc is not None and getattr(exc, "status", None) == 404


# -- the dashboard's view of the shared band client -----------------------------

class DashboardBand:
    """The shared band client as the dashboard uses it.

    Everything is the client's, except a call to the hub's own node (worker
    ``rook``): in two processes the dashboard's call to it crossed the band,
    so it was checked for the dashboard caller, then again as a band call
    (no authenticated principal) and run under the hub's band risk ceiling
    and journaled. Calling the node in memory must not widen that, so such a
    call takes the same two checks and the node's band path here.
    """

    def __init__(self, client) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._client, name, value)

    async def call(self, cap: str, args: dict | None = None, target: str | None = None,
                   timeout: float = 15.0, identity: str | None = None, principal=None) -> dict:
        node = getattr(self._client, "_local", None)
        if node is None or target != node.worker_id:
            return await self._client.call(cap=cap, args=args, target=target, timeout=timeout,
                                           identity=identity, principal=principal)
        from ..band_mcp.client import _authorize, _denial, _settle
        from .authz import BAND_UNAUTHENTICATED
        authz = getattr(self._client, "authz", None)
        sent = _authorize(authz, cap, target, node.entry(), identity, principal, local=False)
        if sent is not None and sent.denied:
            return _denial(sent, target)
        sent, refused = await _settle(authz, sent, args, target)
        if refused is not None:
            return refused
        got = _authorize(authz, cap or "", node.worker_id, node.entry(), identity,
                         BAND_UNAUTHENTICATED, local=True)
        got, refused = await _settle(authz, got, args, target)
        if refused is not None:
            return refused
        if got is not None and got.denied:
            body = got.denial()
        else:
            body = await asyncio.wait_for(
                node.dispatch(cap, args or {}, identity, source="band"), timeout)
        return {"id": uuid.uuid4().hex, "from": node.worker_id, **body}


# -- options ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    from ..paths import data_path
    from ..remote.bootstrap import add_arguments
    env = os.environ.get
    ap = argparse.ArgumentParser(
        prog="rook hub serve",
        description="Rook hub: the dashboard and the MCP server in one process "
                    "(the relay, telesthete-hub, stays its own process).",
        epilog="Every option can also be set with the ROOK_* variable named in its help. "
               "The dashboard's options are those of rook-dashboard; the MCP server's "
               "those of rook-mcp, except its --bind, which is --mcp-bind here.")
    add_arguments(ap)
    g = ap.add_argument_group("MCP server")
    g.add_argument("--mcp-bind", default=env("ROOK_MCP_BIND", DEFAULT_MCP_BIND),
                   help="also serve the MCP server alone on this host:port, as rook-mcp "
                        "did, for existing proxy routes, agent configs and workers "
                        "dialing ws://<hub>:8765/band (ROOK_MCP_BIND, default "
                        f"{DEFAULT_MCP_BIND}); 'off' serves MCP on --port only")
    g.add_argument("--hub", default=None,
                   help="relay host:port, rook-mcp's spelling of --hub-host/--hub-port "
                        "(ROOK_HUB; used when ROOK_HUB_HOST/ROOK_HUB_PORT are unset)")
    g.add_argument("--allowed-hosts", default=env("ROOK_ALLOWED_HOSTS", ""),
                   help="comma-separated public Host headers /mcp accepts (ROOK_ALLOWED_HOSTS). "
                        "Loopback and the dashboard's --domain are always allowed.")
    g.add_argument("--public-url", default=env("ROOK_MCP_PUBLIC_URL", ""),
                   help="public https URL of the MCP server (ROOK_MCP_PUBLIC_URL); enables "
                        "the OAuth front door for claude.ai's web connector")
    g.add_argument("--persist-path",
                   default=env("ROOK_MCP_PERSIST")
                   or data_path("oauth.json", "/var/lib/rook-band-mcp/oauth.json"),
                   help="JSON file for API tokens; the journal, chat, vault and other MCP "
                        "stores live beside it (ROOK_MCP_PERSIST)")
    g.add_argument("--static-token", default=env("ROOK_MCP_STATIC_TOKEN", ""),
                   help="fixed bearer token for /mcp (ROOK_MCP_STATIC_TOKEN); also gates "
                        "/healthz")
    g.add_argument("--journal-path", default=env("ROOK_MCP_JOURNAL", ""),
                   help="sqlite file for the call journal (ROOK_MCP_JOURNAL; default "
                        "journal.db beside --persist-path)")
    g.add_argument("--admin-password", default=env("ROOK_MCP_AUTH_PASSWORD", ""),
                   help=argparse.SUPPRESS)
    g.add_argument("--mcp-max-body", type=int,
                   default=int(env("ROOK_MCP_MAX_BODY", str(16 * 1024 * 1024))),
                   help="largest request body the MCP paths accept, in bytes; larger "
                        "ones get 413 (ROOK_MCP_MAX_BODY, default 16 MiB; 0 = no limit)")
    g.add_argument("-v", "--verbose", action="count", default=0)
    return ap


# Flags that lock an MCP-owned setting (the Settings page says "flag --x").
_MCP_FLAG_KEYS = {"--hub": "core.mcp.relay", "--mcp-bind": "core.mcp.listen",
                  "--allowed-hosts": "core.mcp.allowed_hosts",
                  "--public-url": "core.mcp.public_url",
                  "--admin-password": "core.mcp.admin_password",
                  "--static-token": "core.mcp.static_token", "--psk": "core.band.key",
                  "--persist-path": "core.store.persist_path",
                  "--journal-path": "core.store.journal_path"}


def _hostport(value: str, default_port: int) -> tuple[str, int]:
    host, _, port = (value or "").rpartition(":")
    if not host:
        host, port = value, ""
    host = host.strip("[]")
    return host, int(port or default_port)


def parse_args(argv: list[str]) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    ap = build_parser()
    args = ap.parse_args(argv)
    env = os.environ

    def given(flag: str) -> bool:
        return any(a == flag or a.startswith(flag + "=") for a in argv)

    # Relay: --hub-host/--hub-port (ROOK_HUB_HOST/PORT) as the dashboard reads
    # it; rook-mcp's --hub/ROOK_HUB when only that is set (an MCP unit's
    # environment moved over as is).
    hub = args.hub if args.hub is not None else env.get("ROOK_HUB")
    if hub:
        host, port = _hostport(hub, 7474)
        if not (given("--hub-host") or env.get("ROOK_HUB_HOST")):
            args.hub_host = host
        if not (given("--hub-port") or env.get("ROOK_HUB_PORT")):
            args.hub_port = port
        if (args.hub_host, args.hub_port) != (host, port):
            log.warning("relay %s:%d (--hub-host/--hub-port) wins over --hub/ROOK_HUB %s",
                        args.hub_host, args.hub_port, hub)
    mcp_bind = (args.mcp_bind or "").strip()
    if mcp_bind.lower() in ("", "off", "0", "none", "no"):
        args.mcp_bind_host, args.mcp_bind_port = None, None
    else:
        args.mcp_bind_host, args.mcp_bind_port = _hostport(mcp_bind, 8765)
    args.psks = [p.strip() for p in (args.band_psk or "").split(",") if p.strip()]
    # The dashboard seeds and records only the first key (it always did).
    args.band_psk = args.psks[0] if args.psks else ""
    args.flag_settings = {}
    for flag, key in _MCP_FLAG_KEYS.items():
        if given(flag):
            attr = "band_psk" if flag == "--psk" else flag[2:].replace("-", "_")
            args.flag_settings[key] = (flag, getattr(args, attr, None))
    return ap, args


def _mcp_args(args, domain: str) -> argparse.Namespace:
    """The namespace McpService reads (rook-mcp's names)."""
    allowed = [h.strip() for h in (args.allowed_hosts or "").split(",") if h.strip()]
    # The merged port serves /mcp under the dashboard's own public host.
    if domain and domain not in allowed:
        allowed.append(domain)
    return argparse.Namespace(
        allowed_hosts=",".join(allowed), public_url=args.public_url,
        persist_path=args.persist_path, static_token=args.static_token,
        journal_path=args.journal_path, hub_host=args.hub_host, hub_port=args.hub_port,
        psks=args.psks, flag_settings=args.flag_settings)


# -- the hub -----------------------------------------------------------------------

class Hub:
    """One hub process: start once, stop once."""

    def __init__(self, args, argv: list[str]) -> None:
        self.args = args
        self.argv = list(argv)
        self.client = None
        self.server = None          # rook.remote.bootstrap.CombinedServer
        self.mcp = None             # rook.band_mcp.server.McpService
        self.lifespan = None
        self.bridge = None
        self._mcp_runner: web.AppRunner | None = None
        self._unregister = None
        self._uninstall = None

    def hub_node(self):
        """The hub node (worker ``rook``) the MCP side built, or None."""
        mcp = getattr(self.mcp, "mcp", None) if self.mcp is not None else None
        return getattr(mcp, "_rook_hub", None) if mcp is not None else None

    def upstream_origins(self) -> list[str]:
        """Loopback origins the dashboard's proxies may name for the MCP
        side: the default ``ROOK_*_ADMIN_URL`` port and ``--mcp-bind``'s."""
        ports = {8765}
        if self.args.mcp_bind_port:
            ports.add(self.args.mcp_bind_port)
        return [f"http://{h}:{p}" for p in sorted(ports) for h in LOOPBACK_NAMES]

    async def start(self) -> None:
        args = self.args
        from ..band_mcp.client import MultiBandClient
        from ..band_mcp.memory_diagnostics import install
        from ..band_mcp.server import (McpService, chat_path_for, journal_path_for,
                                       seed_enrollment)
        from ..remote import dashboard_settings as _ds
        from ..remote import upstream
        from ..remote.bootstrap import server_from_args
        from ..remote.enrollment import EnrollmentStore
        from .asgi_bridge import AsgiBridge, Lifespan

        try:
            self._uninstall = install()
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            # No signal handlers here: not the main thread, or Windows (no
            # SIGUSR1/SIGUSR2, and no add_signal_handler).
            self._uninstall = None
        settings_store = _ds.open_store()
        enrollment = EnrollmentStore()
        conflicts = seed_enrollment(enrollment, args.psks)
        self.client = MultiBandClient(psks=enrollment.transport_psks(),
                                      hub_host=args.hub_host, hub_port=args.hub_port)

        # One chat store when both sides name the same file (the usual case).
        from ..band_mcp.chat_rooms import ChatStore
        from ..paths import data_path
        store_dir = os.path.dirname(journal_path_for(args.persist_path, args.journal_path)) or "."
        mcp_chat = chat_path_for(store_dir)
        dash_chat = _ds.chat_db_path(settings_store,
                                     data_path("chat.db", "/var/lib/rook-band-mcp/chat.db"))
        chat = ChatStore(mcp_chat)
        same = os.path.realpath(mcp_chat) == os.path.realpath(dash_chat)
        if not same:
            log.warning("dashboard chat %s and MCP chat %s differ; each side keeps its own "
                        "(set ROOK_CHAT_DB to share one)", dash_chat, mcp_chat)

        self.server = server_from_args(args, self.argv, enrollment=enrollment,
                                       band=DashboardBand(self.client),
                                       chat=chat if same else None,
                                       settings_store=settings_store)
        await self.client.start()
        # This process hands site paths to hub plugins (the middleware below),
        # so plugins that serve them (voice) may load; say so before the MCP
        # side builds the hub node and its plugins.
        from .site import enable_serving
        enable_serving()
        self.mcp = McpService(_mcp_args(args, self.server.domain), self.client, enrollment,
                              accounts=self.server._accounts.store,
                              startup_conflicts=conflicts, banned=self.server._ban_match,
                              settings_store=settings_store, chat=chat)
        await self.mcp.start()
        self.lifespan = Lifespan(self.mcp.app)
        await self.lifespan.start()
        self.bridge = AsgiBridge(self.mcp.app, lifespan=self.lifespan,
                                 max_body=args.mcp_max_body or None)
        self._unregister = upstream.register_local(self.upstream_origins(), self.mcp.app)

        bridge = self.bridge
        hub_node = self.hub_node

        @web.middleware
        async def mcp_paths(request: web.Request, handler):
            # Before the dashboard's own middlewares: MCP paths carry MCP auth
            # (bearer tokens, OAuth), never the dashboard login.
            if is_mcp_path(request.path):
                return await bridge.handle(request)
            # Paths a hub plugin serves (rook/hub/site.py: /voice/*) bring
            # their own auth too (voice keys), so they skip the dashboard's.
            node = hub_node()
            site = getattr(node, "site", None) if node is not None else None
            served = site.match(request.path) if site is not None else None
            # A path the dashboard routes stays the dashboard's, whatever a
            # plugin mounted (the router resolved it before middlewares ran).
            if served is not None and _unrouted(request):
                return await served(request)
            return await handler(request)

        self.server._app.middlewares.insert(0, mcp_paths)
        # A service stop (SIGTERM) should not wait a minute on long-polls.
        self.server.shutdown_timeout = SHUTDOWN_TIMEOUT
        await self.server.start()
        log.info("rook hub: dashboard and MCP on %s:%d (/mcp)", args.bind, args.port)

        if args.mcp_bind_host:
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", bridge.handle)
            from ..remote.bootstrap import InstallerAccessLogger
            self._mcp_runner = web.AppRunner(app, access_log_class=InstallerAccessLogger,
                                             shutdown_timeout=SHUTDOWN_TIMEOUT)
            await self._mcp_runner.setup()
            await web.TCPSite(self._mcp_runner, args.mcp_bind_host, args.mcp_bind_port).start()
            log.info("rook hub: MCP also on http://%s:%d/mcp (--mcp-bind)",
                     args.mcp_bind_host, args.mcp_bind_port)

    async def stop(self) -> None:
        """Stop everything start() started, each part once, in reverse.

        Order: stop accepting connections, refuse MCP requests still arriving
        on kept-alive connections (503), end the /band peers and the MCP
        sessions (the lifespan), then let the servers drain and clean up.
        Each step runs even if an earlier one failed."""
        runners = [r for r in (self._mcp_runner, getattr(self.server, "_runner", None))
                   if r is not None]

        async def stop_listening():
            for runner in runners:
                for site in list(runner.sites):
                    await site.stop()     # closes the listening socket only

        def refuse():
            if self.bridge is not None:
                self.bridge.stopping = True

        async def ws_peers():
            if self.mcp is not None and self.mcp.ws_bridge is not None:
                await self.mcp.ws_bridge.stop()

        async def mcp_runner():
            runner, self._mcp_runner = self._mcp_runner, None
            if runner is not None:
                await runner.cleanup()

        async def dashboard():
            if self.server is not None:
                await self.server.stop()

        def unregister():
            undo, self._unregister = self._unregister, None
            if undo is not None:
                undo()

        async def mcp_service():
            if self.mcp is not None:
                await self.mcp.stop()

        async def band_client():
            if self.client is not None:
                await self.client.stop()

        def diagnostics():
            undo, self._uninstall = self._uninstall, None
            if undo is not None:
                undo()

        async def lifespan():
            if self.lifespan is not None:
                await self.lifespan.stop()   # ends MCP sessions and their streams

        for step in (stop_listening, refuse, ws_peers, lifespan, mcp_runner, dashboard,
                     unregister, mcp_service, band_client, diagnostics):
            try:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("rook hub: stop step %s failed; continuing", step.__name__)


async def serve(args, argv: list[str], stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    handled = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass   # Windows / not the main thread: Ctrl-C still cancels
    hub = Hub(args, argv)
    try:
        await hub.start()
        await stop.wait()
        log.info("rook hub: stopping")
    finally:
        await hub.stop()
        for sig in handled:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> None:
    from ..remote.bootstrap import prepare
    argv = list(sys.argv[1:] if argv is None else argv)
    ap, args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    if not args.verbose:
        # rook-mcp logged at WARNING: keep its per-session chatter out of the journal.
        for name in ("rook.band_mcp", "mcp"):
            logging.getLogger(name).setLevel(logging.WARNING)
    if args.admin_password:
        print("rook hub: ROOK_MCP_AUTH_PASSWORD/--admin-password is ignored: mint tokens "
              "on the dashboard's Tokens page (operator account).", file=sys.stderr)
    prepare(ap, args)
    try:
        asyncio.run(serve(args, argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
