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
                 journal_path: str | None = None,
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

    # Call journal — persistent record of every band call fired through the MCP
    # so long-running / timed-out output doesn't vanish. Defaults next to the
    # token persist file; falls back to a temp path if that dir isn't set.
    from .journal import Journal
    if journal_path is None:
        base = os.path.dirname(persist_path) if persist_path else "/var/lib/rook-band-mcp"
        journal_path = os.path.join(base or ".", "journal.db")
    journal = Journal(journal_path)

    # Handoff / universal-session store — shares the journal's dir + thread ids.
    from .sessions import SessionStore
    sessions = SessionStore(os.path.join(
        os.path.dirname(journal_path) or ".", "sessions.db"))
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
        evicted after ~90s of silence. Either ``worker_id`` or ``name`` can
        be passed to ``rook_call`` to target a worker; ids change whenever a
        worker restarts, names are stable.
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

    def _fail(msg: str) -> str:
        return json.dumps({"ok": False, "error": msg}, indent=2)

    def _caller_identity() -> str:
        """Identity to stamp on band calls, derived from the authenticated
        bearer token's name (resolved from the raw token via the TokenStore).
        Shape is ``agent:<token-name>`` so worker audit logs read cleanly;
        falls back to ``anonymous`` when there's no auth context (e.g. a local
        unguarded run). This is the breadcrumb the worker records — not an
        access gate. Keyed off ``AccessToken.token`` rather than a ``subject``/
        ``claims`` field so it's robust across ``mcp`` versions."""
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token
            tok = get_access_token()
            raw = getattr(tok, "token", None) if tok is not None else None
            if raw:
                name = store.identity_for(raw)
                if name:
                    return f"agent:{name}"
        except Exception:
            pass
        return "anonymous"

    def _resolve_target(spec: str) -> tuple[str | None, str | None]:
        """Resolve a worker id OR name to a live worker id.

        Returns ``(worker_id, None)`` on success, ``(None, error)`` otherwise.
        """
        roster = client.workers
        spec = spec.strip()
        if spec in roster:
            return spec, None
        named = sorted(wid for wid, w in roster.items()
                       if (w.get("name") or "").lower() == spec.lower())
        if len(named) == 1:
            return named[0], None
        if len(named) > 1:
            return None, (f"worker name {spec!r} is ambiguous — {len(named)} live "
                          f"workers share it: {named}. Pass one of these ids.")
        known = sorted({w.get("name") or wid for wid, w in roster.items()})
        return None, (f"unknown worker {spec!r}: not a live worker id or name. "
                      f"Live workers: {', '.join(known) or 'none'}. Ids change "
                      f"when a worker restarts — re-check rook_workers.")

    @mcp.tool()
    async def rook_call(cap: str, args: dict | None = None,
                        worker_id: str | None = None,
                        worker: str | None = None,
                        timeout: float = 15.0) -> str:
        """Invoke a capability on the band and return the reply.

        Args:
            cap: dot-namespaced capability name (e.g. ``"shell.exec"``).
            args: keyword arguments passed to the handler.
            worker_id: target worker — accepts the hex id from ``rook_workers``
                OR the worker name (e.g. ``"kaiju"``); names are resolved
                against the live roster. If omitted, the first worker on the
                band that has the capability replies (first reply wins) — so
                always pass a target when it matters which machine runs this.
            worker: alias for ``worker_id`` (same id-or-name resolution).
            timeout: seconds to wait for the reply.

        Returns the reply dict as JSON: either
        ``{"id","from","ok":true,"result":...}`` or
        ``{"id","from","ok":false,"error":"..."}``. Targeting mistakes
        (unknown worker, missing capability, no reply) come back the same
        way, as ``{"ok": false, "error": "<explanation>"}``.
        """
        roster = client.workers
        spec = worker_id or worker
        if worker_id and worker and worker_id.strip() != worker.strip():
            return _fail(f"worker_id={worker_id!r} and worker={worker!r} "
                         f"disagree — pass just one of them.")

        target: str | None = None
        if spec:
            target, err = _resolve_target(spec)
            if err:
                return _fail(err)
            w = roster[target]
            if cap not in w.get("caps", []):
                holders = sorted({ww.get("name") or wid
                                  for wid, ww in roster.items()
                                  if cap in ww.get("caps", [])})
                return _fail(f"worker {w.get('name')!r} ({target[:8]}…) does not "
                             f"have capability {cap!r}. Workers that do: "
                             f"{', '.join(holders) or 'none on the band'}.")
        elif not any(cap in w.get("caps", []) for w in roster.values()):
            prefix = cap.split(".", 1)[0] + "."
            similar = sorted({c for w in roster.values()
                              for c in w.get("caps", [])
                              if c.startswith(prefix)})
            hint = f" Similar caps: {', '.join(similar)}." if similar else ""
            return _fail(f"no live worker has capability {cap!r}.{hint} "
                         f"See rook_caps for the full list.")

        identity = _caller_identity()
        # Messages sent through the MCP identify their origin by the caller's
        # token identity (falling back to "MCP"), so chat/notify show who's
        # talking instead of a generic label.
        if cap in ("chat.send", "msg.send"):
            args = dict(args or {})
            args.setdefault("sender", identity if identity != "anonymous" else "MCP")
        worker_name = (roster[target].get("name") if target in roster else target)
        try:
            reply = await client.call(cap=cap, args=args, target=target,
                                      timeout=timeout, identity=identity)
        except asyncio.TimeoutError:
            where = (f"worker {roster[target].get('name')!r}" if target in roster
                     else "any worker") if target else f"any worker with {cap!r}"
            # A timeout is exactly the "falls into the ether" case — journal it
            # so the call is at least on the record even though we got no reply.
            timeout_reply = {"ok": False, "timeout": True,
                             "error": f"no reply within {timeout:.0f}s"}
            cid = journal.record(cap=cap, worker=worker_name, identity=identity,
                                 args=args, reply=timeout_reply)
            return _fail(f"no reply from {where} within {timeout:.0f}s "
                         f"(journal id {cid}). The worker may be offline or "
                         f"still executing — a slow call keeps running and its "
                         f"side effects may still land, so check its output "
                         f"before retrying. For long-running caps raise `timeout`.")
        cid = journal.record(cap=cap, worker=worker_name, identity=identity,
                             args=args, reply=reply)
        # Surface the journal id so a caller that later loses this output can
        # fetch it back with rook_journal(call_id=...).
        if isinstance(reply, dict):
            reply = {**reply, "_journal_id": cid}
        return json.dumps(reply, indent=2)

    @mcp.tool()
    async def rook_journal(call_id: str | None = None,
                           worker: str | None = None,
                           cap_prefix: str | None = None,
                           since_secs: float | None = None,
                           only_failures: bool = False,
                           limit: int = 30) -> str:
        """Query the call journal — the persistent record of every ``rook_call``
        fired through this MCP, so output from a long-running or timed-out call
        isn't lost when the tool result is discarded.

        Args:
            call_id: fetch one specific call (from a prior reply's
                ``_journal_id``) — returns it WITH its full stored output.
            worker: filter to calls targeting this worker name.
            cap_prefix: filter by capability prefix (e.g. ``"shell."``).
            since_secs: only calls newer than this many seconds ago.
            only_failures: restrict to calls that failed or timed out.
            limit: max entries (listings omit the full reply body to stay
                light; a single ``call_id`` lookup always includes it).

        Returns a JSON object ``{count, entries:[...]}``. Each entry:
        ``{call_id, ts, identity, cap, worker, thread_id, ok, error?}``, plus
        ``reply`` when a single call_id was requested. Use this to recover
        "what did that call actually return" after the fact.
        """
        since = None
        if since_secs is not None:
            import time as _t
            since = _t.time() - float(since_secs)
        entries = journal.query(
            call_id=call_id, worker=worker, cap_prefix=cap_prefix, since=since,
            ok=(False if only_failures else None), limit=limit,
            include_reply=bool(call_id))
        return json.dumps({"count": len(entries), "entries": entries}, indent=2)

    @mcp.tool()
    async def rook_handoff_save(goal: str, thread_id: str | None = None,
                                state: str = "", decisions: list | str | None = None,
                                next_steps: list | str | None = None,
                                artifacts: list | str | None = None,
                                supersedes: list | str | None = None,
                                transcript_ref: str | None = None) -> str:
        """Save a handoff so any agent can pick this session up later.

        A handoff is the structured state of a piece of work — not a full
        transcript. Provide the ``goal``, the current ``state`` (where things
        stand), ``decisions`` made, ``next_steps``, and any ``artifacts``
        touched (files, hosts, URLs). Omit ``thread_id`` to start a new thread
        (one is returned); pass an existing ``thread_id`` to update it — the
        prior handoff becomes history and this becomes current. Write one at the
        end of a work session, or whenever you hand off to another agent.
        """
        res = sessions.save(
            thread_id=thread_id, author=_caller_identity(), goal=goal, state=state,
            decisions=decisions, next_steps=next_steps, artifacts=artifacts,
            supersedes=supersedes, transcript_ref=transcript_ref)
        return json.dumps(res, indent=2)

    @mcp.tool()
    async def rook_handoff_get(thread_id: str) -> str:
        """Fetch a session's current handoff to continue it.

        Returns the current handoff plus prior (superseded) ones as history.
        EVERY handoff carries a ``freshness`` banner — heed it: a ``SUPERSEDED``
        or ``STALE`` marker means the state may no longer be true, so verify
        before acting on it rather than treating it as current fact.
        """
        return json.dumps(sessions.get(thread_id), indent=2)

    @mcp.tool()
    async def rook_handoff_list(limit: int = 20, active_only: bool = True) -> str:
        """List recent session threads (latest handoff per thread) with their
        goals and freshness. Use this to find a thread to resume; then
        rook_handoff_get(thread_id) for its full state."""
        return json.dumps(sessions.list_recent(limit=limit, active_only=active_only),
                          indent=2)

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
        journal_path=args.journal_path or None,
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

    # /tokens admin UI (mint/list/revoke bearer tokens).
    for r in reversed(build_api_token_routes(store)):
        app.router.routes.insert(0, r)

    # Thin OAuth front-door for claude.ai's web connector (which won't take a
    # bare bearer header). The client secret it asks for IS a rook token, and
    # the access_token it gets back is that same token — no separate OAuth
    # lifecycle, tokens stay the single source of truth. Wraps the whole app so
    # /authorize, /token, and the auth-server metadata are always public;
    # everything else (/mcp, /tokens) passes straight through. See oauth_shim.
    if args.public_url:
        from .oauth_shim import OAuthShim
        app = OAuthShim(app, store, args.public_url)

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
                         "Advertised as resource-server metadata, and enables "
                         "the thin OAuth front-door (oauth_shim) so claude.ai's "
                         "web connector can attach using a rook token as the "
                         "client secret.")
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
    ap.add_argument("--journal-path",
                    default=os.environ.get("ROOK_MCP_JOURNAL", ""),
                    help="sqlite file for the call journal (default: "
                         "journal.db next to --persist-path).")
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
