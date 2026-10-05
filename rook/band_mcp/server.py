"""FastMCP server exposing a Telesthete band as MCP tools.

Tools:
    rook_workers()                 — list workers seen on the band
    rook_caps()                    — list capabilities per worker
    rook_call(cap, args?, worker_id?, timeout?) — fire a capability call
    rook_console_open/read/write/close/list/search — long-running commands as
        named, band-visible, permanently searchable terminal sessions

Run:
    ROOK_BAND_PSK=mysecret python -m rook.band_mcp \\
        --hub 127.0.0.1:7474 --bind 127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import re as _re
import json
import logging
import os
import sys
import time

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
                 enrollment=None,
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
            # Loopback on any port, so --bind can move off 8765.
            "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]:*",
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
    _store_dir = os.path.dirname(journal_path) or "."
    sessions = SessionStore(os.path.join(_store_dir, "sessions.db"))

    # Site-hosted chat rooms + presence + voicemail (design §3).
    from .chat_rooms import ChatStore
    # ROOK_CHAT_DB wins, as it does for the dashboard; otherwise beside the
    # journal. The dashboard follows this choice through the settings store's
    # runtime report, so both serve the same rooms (settings P4).
    chat = ChatStore(os.environ.get("ROOK_CHAT_DB") or os.path.join(_store_dir, "chat.db"))

    # Console rooms — named, searchable terminal sessions pumped off the band.
    from .console_rooms import ConsoleStore
    console = ConsoleStore(os.path.join(_store_dir, "console.db"))
    # Resource-server metadata only (no authorization-server advertised).
    # FastMCP refuses a token verifier without AuthSettings, so a hub with no
    # public URL (local / LAN only) advertises its loopback address instead.
    base_url = (public_url or "http://127.0.0.1").rstrip("/")
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(base_url),
        resource_server_url=AnyHttpUrl(base_url + "/mcp"),
        required_scopes=["rook"],
    )

    mcp = FastMCP(
        "rook-band",
        transport_security=sec,
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    # Keep SDK auth/routing, replace only stateful session ownership.
    from .http_sessions import BoundedSessionManager
    mcp._session_manager = BoundedSessionManager(
        app=mcp._mcp_server,
        security_settings=sec,
        json_response=mcp.settings.json_response,
    )

    # Per-call audit attribution (see attribution.py). Every tool call passes
    # through here: a missing/invalid token denies that call; a failure of the
    # attribution machinery itself lets the call through and alerts. Nothing
    # here looks at bands, workers or capabilities.
    from mcp.server.fastmcp.exceptions import ToolError
    from . import attribution as _attr
    from ..hub import authz as _hub_authz
    from ..hub.node import HUB_WORKER_NAME
    _alert = _attr.Alerter()
    _run_tool = mcp._tool_manager.call_tool

    def _audit_row(kind: str, name: str, arguments: dict | None,
                   att: "_attr.Attribution | None", reason: str) -> None:
        try:
            _write_audit_row(kind, name, arguments, att, reason)
        except Exception:  # noqa: BLE001 — audit bookkeeping never blocks a call
            log.exception("audit journal row failed")

    def _write_audit_row(kind, name, arguments, att, reason) -> None:
        a = arguments if isinstance(arguments, dict) else {}
        journal.record(
            cap=f"audit.{kind}", worker=a.get("worker_id") or a.get("worker"),
            identity=att.identity if att else "unauthenticated", args=None,
            reply={"ok": kind != "denied", "error": reason, "tool": name,
                   "cap": a.get("cap")},
            audit=att.audit() if att else {"kind": "denied", "verified": False})

    # Per MCP session: (client name, working dir) as the client reports them.
    _session_facts_cache: dict[str, tuple[str | None, str | None]] = {}

    async def _session_facts(context) -> tuple[str | None, str | None]:
        """Client name from MCP initialize; working dir from roots/list (if the
        client supports it) else an X-Rook-Cwd header. Cached per session;
        any failure just leaves the part unknown."""
        req = sid = None
        try:
            req = context.request_context.request
            sid = req.headers.get("mcp-session-id") if req is not None else None
        except Exception:
            pass
        if sid and sid in _session_facts_cache:
            return _session_facts_cache[sid]
        client_name = cwd = None
        roots_seen = None
        try:
            session = context.request_context.session
            params = session.client_params
            client_name = params.clientInfo.name if params and params.clientInfo else None
            caps = params.capabilities if params else None
            if caps is not None and getattr(caps, "roots", None) is not None:
                from urllib.parse import unquote, urlparse
                listed = await asyncio.wait_for(session.list_roots(), 2.0)
                roots_seen = [str(r.uri) for r in listed.roots]
                for uri in roots_seen:
                    if uri.startswith("file://"):
                        cwd = unquote(urlparse(uri).path) or None
                        break
        except Exception as e:  # noqa: BLE001 — identity parts are best-effort
            log.debug("session facts unavailable: %s", type(e).__name__)
        if cwd is None and req is not None:
            cwd = (req.headers.get("x-rook-cwd") or "").strip() or None
        if sid:
            log.info("mcp session %s: client=%r roots=%r cwd=%r", sid[:8], client_name, roots_seen, cwd)
            if len(_session_facts_cache) > 5000:
                _session_facts_cache.clear()
            _session_facts_cache[sid] = (client_name, cwd)
        return client_name, cwd

    async def _attributed_call_tool(name, arguments, context=None,
                                    convert_result=False):
        def token():
            from mcp.server.auth.middleware.auth_context import get_access_token
            tok = get_access_token()
            return getattr(tok, "token", None) if tok is not None else None

        def request():
            return context.request_context.request if context is not None else None

        try:
            att = _attr.resolve(store, token, request, _caller_host())
        except _attr.Unauthenticated as e:
            _audit_row("denied", name, arguments, None, str(e))
            log.warning("denied MCP call %r: %s", name, e)
            raise ToolError(f"unauthenticated: {e}") from None
        except Exception as e:  # noqa: BLE001 — never let the checker deny
            att = _attr._unverified(f"attribution crashed: {type(e).__name__}")
        if not att.verified:
            _alert(name, att)
            _audit_row("unverified", name, arguments, att, att.reason or "")
        try:
            client_name, cwd = await _session_facts(context)
            att = _attr.compound(att, client_name, _caller_host(), cwd)
        except Exception:  # noqa: BLE001 — never let identity detail block a call
            log.exception("compound identity failed")
        reset = _attr.current.set(att)
        # The authenticated principal (permissions 1): from the verified token
        # only, never from X-Rook-Host or the envelope identity.
        principal = _hub_authz.principal_for_token(att, att.role)
        preset = _hub_authz.current_principal.set(principal)
        try:
            _authorize_tool(name, arguments, principal)
            return await _run_tool(name, arguments, context=context,
                                   convert_result=convert_result)
        finally:
            _hub_authz.current_principal.reset(preset)
            _attr.current.reset(reset)

    def _authorize_tool(name, arguments, principal) -> None:
        """Hub tools are caps on `rook` (permissions 3.5, Appendix A.2).
        Raises ToolError only for a denial in enforce mode."""
        authz = getattr(client, "authz", None)
        cap = _hub_authz.hub_cap_for_tool(name, arguments)
        if authz is None or cap is None:
            return
        node = getattr(mcp, "_rook_hub", None)
        entry = node.entry() if node is not None else {"name": HUB_WORKER_NAME, "roles": ["is_hub"]}
        d = authz.check(cap, node.worker_id if node is not None else HUB_WORKER_NAME, entry,
                        principal=principal, local=True)
        if d.denied:
            raise ToolError(d.denial()["error"] + f" (rule {d.rule}, policy rev {d.rev})")

    mcp._tool_manager.call_tool = _attributed_call_tool

    # Compact replies and per-session notices (see envelope.py).
    from . import envelope as _env
    from . import roster as _roster
    _env.install(mcp)
    _notices = _env.Notices()

    from .guidance import Guidance, apply as _apply_guidance
    guidance = Guidance(os.path.join(_store_dir, "guidance.db"))
    _base_descriptions: dict[str, str] = {}

    def _guidance_apply() -> None:
        try:
            _apply_guidance(mcp, guidance, _base_descriptions)
        except Exception:
            log.exception("applying guidance failed; tools keep their docstrings")
    mcp._rook_guidance = (guidance, _guidance_apply)

    # Secret vault (see vault.py). If it can't open, secrets are unavailable
    # but nothing else is affected.
    from . import vault as _vault_mod
    try:
        vault = _vault_mod.Vault(os.path.join(_store_dir, "vault.db"))
    except Exception:
        log.exception("vault unavailable")
        vault = None
    mcp._rook_vault = vault
    mcp._rook_journal = journal
    mcp._rook_store_dir = _store_dir

    # Reverse masking (secret_mask.py): any known vault value in what an
    # agent gets back from any tool becomes its {{secret:name}} stub; the
    # stores (journal, chat, handoffs, console, knowledge) mask what they
    # write through the same installed vault. rook_secret is exempt: its
    # get action is the audited way to read a raw value.
    from . import secret_mask as _secret_mask
    _secret_mask.install(vault)
    _masked_run = mcp._tool_manager.call_tool
    _UNMASKED_TOOLS = {"rook_secret"}

    # A tool marks its reply as not to be masked by setting "skip" in the
    # dict this holds (rook_call does, for hub caps tagged sensitive, whose
    # whole job is to hand a secret to a service).
    import contextvars as _cv
    _reply_unmasked: "_cv.ContextVar[dict | None]" = _cv.ContextVar("rook_reply_unmasked", default=None)

    def _mask_text(text: str) -> str:
        m = _secret_mask.current_matcher()
        if text[:1] in "[{":
            # Mask the parsed document, not its text: a stub must stand for
            # the decoded string (an escaped form in JSON text is the raw
            # value one level up), and a match must never eat JSON syntax.
            try:
                doc = json.loads(text)
            except ValueError:
                pass
            else:
                masked = m.mask(doc)
                return text if masked == doc else _env.dumps(masked)
        return m.sub(text)

    async def _masked_call_tool(name, arguments, context=None, convert_result=False):
        flag: dict = {}
        tok = _reply_unmasked.set(flag)
        try:
            result = await _masked_run(name, arguments, context=context,
                                       convert_result=convert_result)
        finally:
            _reply_unmasked.reset(tok)
        if name in _UNMASKED_TOOLS or flag.get("skip") or not _secret_mask.current_matcher():
            return result
        try:
            items, structured = (result if isinstance(result, tuple) else (result, None))
            if isinstance(items, list):
                for item in items:
                    text = getattr(item, "text", None)
                    if isinstance(text, str):
                        item.text = _mask_text(text)
            elif isinstance(items, str):
                items = _mask_text(items)
            elif isinstance(items, (dict, tuple)):
                items = _secret_mask.scrub(items)
            if isinstance(result, tuple):
                return (items, _secret_mask.scrub(structured))
            return items
        except Exception:  # noqa: BLE001 — never log the payload
            log.exception("masking the %s reply failed", name)
            return result

    mcp._tool_manager.call_tool = _masked_call_tool

    # Permissions (docs/design/permissions.md): policy evaluation + call
    # tickets inside the band client, so every path that emits a band call
    # is covered. Ships in audit mode: nothing is denied until the operator
    # sets mode: enforce in policy.json.
    try:
        client.authz = _hub_authz.build_authorizer(_store_dir, record=journal.record)
    except Exception:
        log.exception("ROOK AUTHZ ALERT: authorizer unavailable; calls are not evaluated")
    mcp._rook_authz = getattr(client, "authz", None)

    # Hub plugin host (rook.hub): hub-placed plugins, served on the band as the
    # reserved worker "rook" and reachable with rook_call(worker="rook").
    from ..hub.node import HUB_WORKER_NAME, attach_hub_node
    mcp._rook_hub = attach_hub_node(client, _store_dir, vault=vault, journal=journal,
                                    enrollment=enrollment, chat=chat)

    # Shared knowledge and work records: the hub plugins "knowledge" and
    # "task" (rook/hub/plugins; opt-in with their "enabled" setting, env
    # ROOK_KNOWLEDGE=1). The bridge wires in what only it has (the attributed
    # caller, the handoff store, the band enrollment), then adds the plugins'
    # MCP tools (rook_knowledge, rook_concept, rook_project, rook_task). A
    # store that can't open leaves the plugins unloaded: no tools, and
    # nothing here touches the band call path.
    mcp._rook_knowledge = None
    mcp._rook_hygiene = None
    _kplugin = mcp._rook_hub.plugin("knowledge") if mcp._rook_hub is not None else None
    if _kplugin is not None:
        try:
            def _principal():
                att = _attr.current.get()
                return att.audit() if att is not None else None
            def _save_handoff(author, h):
                res = sessions.save(thread_id=h.get("thread_id"), author=author,
                                    goal=h.get("goal", ""), state=h.get("state", ""),
                                    decisions=h.get("decisions"), next_steps=h.get("next_steps"),
                                    artifacts=h.get("artifacts"))
                if not res.get("ok"):
                    raise ValueError(res.get("error") or "handoff save failed")
                return res["thread_id"]
            def _list_handoffs():
                return sessions.list_recent(limit=200).get("threads", [])
            def _close_handoff(thread_id, author, reason):
                res = sessions.close_thread(thread_id, author, reason)
                return bool(res.get("ok")) and not res.get("already")
            _kplugin.principal = _principal
            knowledge = _kplugin.service
            knowledge.handoffs = _save_handoff
            knowledge.handoff_list = _list_handoffs
            knowledge.handoff_close = _close_handoff
            knowledge.enrollment = enrollment
            mcp._rook_knowledge = knowledge
            from .hygiene import Hygiene
            mcp._rook_hygiene = Hygiene(knowledge, client, chat,
                                        lambda: guidance.get("hygiene"), journal)
        except Exception:
            log.exception("wiring the knowledge plugin failed; knowledge tools disabled")
            mcp._rook_knowledge = None
    if mcp._rook_hub is not None:
        from ..hub.mcp_tools import register_plugin_tools
        def _tool_identity():
            att = _attr.current.get()
            return att.identity if att is not None else None
        register_plugin_tools(mcp, mcp._rook_hub, _tool_identity)
        guidance.add_defaults(mcp._rook_hub.guidance_defaults())

    @mcp.tool()
    async def rook_whoami() -> str:
        """Show the identity this MCP attributes your calls to.

        ``agent_id`` is stable across key rotation and label changes;
        ``key_id`` names the specific API key; the shared static key is
        ``kind: "shared"`` with no agent_id. ``identity`` is the string stamped
        on band calls, chat and the journal. Attribution only — it grants or
        restricts nothing. Never returns the token itself.
        """
        att = _attr.current.get()
        return json.dumps(att.audit() if att else {"kind": "unverified"}, indent=2)

    @mcp.tool()
    async def rook_workers(name: str | None = None, cap_prefix: str | None = None,
                           online: bool | None = None,
                           fields: list | str | None = None) -> str:
        """List workers currently visible on the band (see descriptions.py
        for the advertised text; roster.workers_view for the shape).

        ``description`` is persistent human-written role metadata, set with
        ``rook_call(cap="worker.description_set", ...)``; ``hb`` carries live
        heartbeat status a worker opts into. Workers re-announce every 30s and
        are evicted after ~90s of silence.
        """
        hub = getattr(mcp, "_rook_hub", None)
        hosting = hub.plugin("serves") if hub is not None else None
        try:
            return _env.dumps(_roster.workers_view(client.workers, name=name, cap_prefix=cap_prefix,
                                                   online=online, fields=fields,
                                                   serves=hosting.lookup if hosting is not None else None))
        except ValueError as e:
            return _fail(str(e))

    @mcp.tool()
    async def rook_caps(prefix: str | None = None, worker: str | None = None) -> str:
        """Capabilities on the band and who holds them (roster.caps_view)."""
        try:
            return _env.dumps(_roster.caps_view(client.workers, prefix=prefix, worker=worker))
        except ValueError as e:
            return _fail(str(e))

    def _fail(msg: str) -> str:
        return _env.dumps({"ok": False, "error": msg})

    import re as _re
    _ANSI = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    _BOXCHARS = "─│╱╲╳┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬▀▄█▌▐░▒▓⚕☤┊⚡•·"

    # Footer markers that hermes prints AFTER its reply — the reply ends here.
    _HERMES_FOOTER = ("resume this session", "session:", "duration:", "messages:",
                      "hermes --resume")

    def _clean_hermes_stdout(text: str) -> str:
        """Extract hermes's actual reply from its TUI stdout. The layout is:
        echoed Query + ``Initializing`` + tool-call ticks, then a standalone
        ``⚕ Hermes`` banner, then the REPLY, then a session footer (Resume/
        Session/Duration/Messages). We keep the lines between the banner and the
        footer. Falls back to boilerplate filtering if the banner is absent."""
        text = _ANSI.sub("", str(text or ""))
        lines = []
        for raw in text.replace("\r", "\n").split("\n"):
            ln = "".join(c for c in raw if c not in _BOXCHARS).strip()
            if ln:
                lines.append(ln)
        # Banner = a line that reduced to exactly "hermes" (the ⚕ Hermes header).
        banner_idx = next((i for i, ln in enumerate(lines)
                           if ln.lower() == "hermes"), -1)
        if banner_idx >= 0:
            body = []
            for ln in lines[banner_idx + 1:]:
                if ln.lower().startswith(_HERMES_FOOTER):
                    break  # reply ends where the session footer begins
                body.append(ln)
            return " ".join(body).strip()
        # No banner — drop obvious boilerplate + footer and return the rest.
        kept = [ln for ln in lines
                if not ln.lower().startswith(("query:", "initializing agent")
                                             + _HERMES_FOOTER)]
        return " ".join(kept).strip()

    def _caller_host() -> str:
        """Optional per-host suffix for the identity, from an ``X-Rook-Host``
        request header. Several agents commonly share one token (every Claude
        Code on every box using the "claude" token); with the header each
        shows up as ``agent:claude_laptop`` instead of one blurred ``agent:claude``.
        Set it in the MCP client config, e.g. Claude Code's ``.claude.json``:
        ``"headers": {"Authorization": "Bearer …", "X-Rook-Host": "laptop"}``.
        Sanitised to ``[A-Za-z0-9._-]{1,32}``. Audit breadcrumb, not a gate."""
        try:
            req = mcp.get_context().request_context.request
            raw = (req.headers.get("x-rook-host") or "").strip() if req is not None else ""
        except Exception:
            return ""
        raw = _re.sub(r"[^A-Za-z0-9._-]", "", raw)[:32]
        return raw

    def _caller_identity() -> str:
        """Identity to stamp on band calls, chat and the journal —
        ``agent:<token-label>`` or ``agent:<token-label>_<host>`` when the
        client sends ``X-Rook-Host`` (see ``_caller_host``). Set per tool call
        by the attribution wrapper above; ``unverified`` means attribution
        machinery failed and an alert was raised. Audit breadcrumb, not a gate
        — the token check happens in the wrapper, before the tool runs."""
        att = _attr.current.get()
        return att.identity if att is not None else "anonymous"

    def _caller_session() -> str:
        try:
            req = mcp.get_context().request_context.request
            return (req.headers.get("mcp-session-id") or "") if req is not None else ""
        except Exception:
            return ""

    def _auto_link(kind: str, ref, note: str = "", task: str | None = None) -> str | None:
        """Attach an artifact to the caller's claimed task, if any (design §3),
        or to ``task`` when the caller names one. Bookkeeping only; never
        affects the call."""
        k = getattr(mcp, "_rook_knowledge", None)
        if k is None or not ref:
            return None
        try:
            return k.store.auto_link(k.actor(), kind, ref, note=note, task=task)
        except Exception:
            log.exception("auto-link failed")
            return None

    def _actor_name() -> str:
        att = _attr.current.get()
        return (att.actor or att.identity) if att is not None else "anonymous"

    def _claimed_task() -> str | None:
        k = getattr(mcp, "_rook_knowledge", None)
        if k is None:
            return None
        try:
            with k.store.db(False) as db:
                row = db.execute("SELECT task FROM claims WHERE actor=? AND released IS NULL "
                                 "ORDER BY started DESC LIMIT 1", (_actor_name(),)).fetchone()
            return row["task"] if row else None
        except Exception:
            return None

    def _decision_row() -> dict | None:
        d = _hub_authz.last_decision.get()
        return d.journal() if d is not None else None

    def _caller_audit() -> dict | None:
        att = _attr.current.get()
        return att.audit() if att is not None else None

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

    # How long rook_call waits is derived from the call itself: the cap's own
    # ``timeout`` arg, else that cap's declared default on that worker (learned
    # from caps.describe, fetched once per worker), plus a margin for transport.
    _DEFAULT_WAIT = 15.0
    _WAIT_MARGIN = 5.0
    _MAX_WAIT = 3600.0
    _cap_timeouts: dict[str, dict[str, float]] = {}
    _describe_tried: dict[str, float] = {}

    def _learn_timeouts(worker_id: str, described) -> None:
        if not isinstance(described, dict):
            return
        table: dict[str, float] = {}
        for name, spec in described.items():
            for p in (spec.get("params") or []) if isinstance(spec, dict) else []:
                d = p.get("default") if isinstance(p, dict) else None
                if p.get("name") == "timeout" and isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0:
                    table[name] = float(d)
        _cap_timeouts[worker_id] = table

    async def _cap_timeout(target: str, cap: str, args: dict | None) -> float | None:
        """The timeout the call will run under on the worker, if it has one."""
        v = (args or {}).get("timeout")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
        w = client.workers.get(target) or {}
        if ("caps.describe" in w.get("caps", []) and target not in _cap_timeouts
                and time.monotonic() - _describe_tried.get(target, -1e9) > 600):
            _describe_tried[target] = time.monotonic()
            try:
                r = await client.call(cap="caps.describe", args={}, target=target,
                                      timeout=10.0, identity="system:rook-mcp")
                if isinstance(r, dict) and r.get("ok"):
                    _learn_timeouts(target, r.get("result"))
            except Exception:
                log.debug("caps.describe for timeouts failed on %s", target, exc_info=True)
        return _cap_timeouts.get(target, {}).get(cap)

    @mcp.tool()
    async def rook_call(cap: str, args: dict | None = None,
                        worker_id: str | None = None,
                        worker: str | None = None,
                        timeout: float | None = None,
                        hint: bool = False,
                        text: bool = False) -> str:
        """Invoke a capability on the band and return the reply.

        Args:
            cap: dot-namespaced capability name (e.g. ``"shell.exec"``).
            args: keyword arguments passed to the handler.
            worker_id: REQUIRED target worker — the hex id from ``rook_workers``
                OR the worker name (e.g. ``"gpu-01"``); names are resolved
                against the live roster. Calls without a target are refused
                (the error lists which workers have the capability) so a call
                never runs on whichever machine happens to answer first.
            worker: alias for ``worker_id`` (same id-or-name resolution).
            timeout: optional extra wait, in seconds. By default rook_call waits
                as long as the call itself may run: ``args.timeout`` if you pass
                one, else that cap's declared default on the target worker
                (e.g. shell.exec: 30s), plus 5s; 15s for caps with no timeout.
                A smaller value is raised to that, so the wait never ends
                before the worker would. To let a command run longer, raise
                ``args.timeout``; for jobs over a few minutes use
                rook_console_open.
            hint: set ``hint=true`` to get this cap's usage tip in ``_tips``
                again. Tips are sent once per MCP session.
            text: return a successful result as plain text (shell.exec:
                stdout, then stderr / exit code only when non-empty/non-zero).

        Returns compact JSON (envelope.call_reply): ``{"ok","id","from",
        "result"|"error"}`` where ``id`` is the journal id and ``from`` the
        worker's name; ``_task``/``_unread_chat`` notices only when new or
        changed for this MCP session. Targeting mistakes (unknown worker,
        missing capability, no reply) come back as ``{"ok": false, "error"}``.
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
        else:
            # Never let "first responder wins" pick the machine for the caller.
            holders = sorted({w.get("name") or wid for wid, w in roster.items()
                              if cap in w.get("caps", [])}, key=str.lower)
            shown = ", ".join(holders[:25]) + (f", … ({len(holders)} total)"
                                               if len(holders) > 25 else "")
            return _fail(f"specify the target worker: pass worker=\"<name or id>\" "
                         f"(rook_call without a worker is not allowed, so a call "
                         f"never runs on whichever machine answers first). "
                         f"Workers with {cap!r}: {shown}.")

        identity = _caller_identity()
        # Messages sent through the MCP identify their origin by the caller's
        # token identity (falling back to "MCP"), so chat/notify show who's
        # talking instead of a generic label.
        if cap in ("chat.send", "msg.send"):
            args = dict(args or {})
            args.setdefault("sender", identity if identity not in ("anonymous", "unverified") else "MCP")
        worker_name = (roster[target].get("name") if target in roster else target)
        # {{secret:name}} placeholders: substituted only in what's sent to the
        # worker; the journal keeps the placeholder and replies are masked.
        # caps.describe(prefix=) is filtered here, so it works on workers that
        # predate the worker-side prefix arg (build 167 rejects unknown args).
        describe_prefix = None
        if cap == "caps.describe" and isinstance(args, dict) and "prefix" in args:
            args = dict(args)
            describe_prefix = str(args.pop("prefix") or "")
        send_args, used = args, {}
        if args and _vault_mod.PLACEHOLDER.search(json.dumps(args)):
            if vault is None:
                return _fail("args use {{secret:…}} but the vault is unavailable on this hub")
            try:
                send_args, used = vault.substitute(args, _actor_name(), via=f"{cap} on {worker_name}",
                                                   task=_claimed_task())
            except KeyError as e:
                return _fail(f"unknown secret {e.args[0]!r} in args; rook_secret(action='list') shows the names")
            for name in used:
                _auto_link("secret", name, f"used in {cap} on {worker_name}")
        try:
            own = await _cap_timeout(target, cap, args)
        except Exception:
            own = None
        floor = own + _WAIT_MARGIN if own else _DEFAULT_WAIT
        wait = min(max(floor, float(timeout or 0)), _MAX_WAIT)
        _hub_authz.last_decision.set(None)
        jtok = _hub_authz.caller_journals.set(True)
        try:
            reply = await client.call(cap=cap, args=send_args, target=target,
                                      timeout=wait, identity=identity)
        except asyncio.TimeoutError:
            where = f"worker {worker_name!r}"
            # A timeout is exactly the "falls into the ether" case — journal it
            # so the call is at least on the record even though we got no reply.
            timeout_reply = {"ok": False, "timeout": True,
                             "error": f"no reply within {wait:.0f}s"}
            cid = journal.record(cap=cap, worker=worker_name, identity=identity,
                                 args=args, reply=timeout_reply,
                                 audit=_caller_audit(), authz=_decision_row())
            _auto_link("journal", cid, f"{cap} on {worker_name} (timed out)")
            basis = (f"the call's own {own:.0f}s timeout + {_WAIT_MARGIN:.0f}s" if own
                     else f"the {_DEFAULT_WAIT:.0f}s default; {cap!r} declares no timeout")
            return _fail(f"no reply from {where} within {wait:.0f}s ({basis}; "
                         f"journal id {cid}). The worker may be offline, or the "
                         f"call may still be running and its side effects may "
                         f"still land. Check rook_journal(call_id={cid!r}) and the "
                         f"worker before retrying. To let it run longer, raise "
                         f"args.timeout (if the cap takes one) or pass timeout=; "
                         f"for long jobs use rook_console_open.")
        finally:
            _hub_authz.caller_journals.reset(jtok)
        hub = getattr(mcp, "_rook_hub", None)
        sensitive = (hub is not None and target == hub.worker_id and isinstance(reply, dict)
                     and "sensitive" in (getattr(hub.host.registry.meta(cap), "tags", ()) or ()))
        if sensitive:
            # Hub caps tagged sensitive (settings.fetch) exist to hand a vault
            # value to a service: the caller gets it unmasked (here and in the
            # tool-reply wrapper), the journal gets nothing.
            holder = _reply_unmasked.get()
            if holder is not None:
                holder["skip"] = True
        else:
            # Every known vault value in the reply becomes {{secret:name}} (the
            # values this call used are masked even below the length floor).
            # The worker got the real values in send_args; nothing here touches them.
            _masker = _secret_mask.installed()
            if _masker is not None:
                reply = _masker.mask(reply, extra=used)
        if cap == "caps.describe" and isinstance(reply, dict) and reply.get("ok"):
            _learn_timeouts(target, reply.get("result"))
        journal_reply = reply
        if sensitive:
            journal_reply = {"ok": reply.get("ok"), "result": "[sensitive: not journaled]"} \
                if reply.get("ok") else _secret_mask.scrub(reply, extra=used)
        cid = journal.record(cap=cap, worker=worker_name, identity=identity,
                             args=args, reply=journal_reply, audit=_caller_audit(),
                             authz=_decision_row())
        linked = _auto_link("journal", cid, f"{cap} on {worker_name}")
        chat.touch(identity)
        if not isinstance(reply, dict):
            return _env.dumps(reply)
        # Notices ride the reply only when new for this MCP session: the task
        # this call was recorded on, unread chat (voicemail piggyback, no
        # polling) and operator-editable cap advice.
        session = _caller_session() or identity
        notices: dict = {}
        if linked and _notices.fresh(session, "task", linked):
            notices["_task"] = linked
        unread = chat.unread_summary(identity)
        if unread and _notices.fresh(session, "chat", unread):
            notices["_unread_chat"] = unread
        elif not unread:
            _notices.fresh(session, "chat", [])  # so the same room shows again later
        try:
            notices.update(guidance.tips(session, cap, bool(hint)))
        except Exception:
            log.exception("guidance tips failed")
        if cap == "caps.describe" and reply.get("ok") and isinstance(reply.get("result"), dict):
            if not _env.legacy():
                reply = {**reply, "result": _env.describe_compact(reply["result"], describe_prefix or "")}
            elif describe_prefix:
                reply = {**reply, "result": {k: v for k, v in reply["result"].items()
                                             if k.startswith(describe_prefix)}}
        if text:
            plain = _env.call_text(reply, cap, notices)
            if plain is not None:
                return plain
        # ``id`` is the journal id: rook_journal(call_id=id) recovers this reply.
        return _env.dumps(_env.call_reply(reply, cid, worker_name, cap, notices))

    @mcp.tool()
    async def rook_secret(action: str = "list", name: str = "",
                          value: str = "", description: str = "") -> str:
        # Plain `str` annotations matter: FastMCP JSON-parses string arguments
        # whose declared type isn't exactly str, which turned JSON-valued
        # secrets (OAuth client files, tunnel creds) into dicts and rejected them.
        """Credentials for your work, from the hub's vault.

        list: names and descriptions (never values). get name=…: the raw value
        (every read is logged with your identity and claimed task). Prefer not
        reading it at all: put ``{{secret:<name>}}`` inside rook_call args and
        the hub substitutes it on the way to the worker, masks it in the reply
        and journals only the placeholder. set name=… value=… description=…:
        store or replace one (e.g. after rotating it). delete name=….
        log name=?: recent access. Never paste secret values into knowledge
        pages, chat or handoffs; refer to them by vault name.
        """
        if vault is None:
            return _fail("vault unavailable on this hub")
        who = _actor_name()
        try:
            if action == "list":
                return json.dumps({"ok": True, "secrets": vault.list()}, indent=2)
            if action == "log":
                return json.dumps({"ok": True, "access": vault.access_log(name)}, indent=2)
            if not name:
                return _fail(f"{action} needs name=")
            if action == "get":
                val = vault.get(name, who, via="get", task=_claimed_task())
                journal.record(cap="vault.get", worker=None, identity=_caller_identity(),
                               args={"name": name}, reply={"ok": True}, audit=_caller_audit())
                _auto_link("secret", name, "read with rook_secret get")
                return json.dumps({"ok": True, "name": name, "value": val}, indent=2)
            if action == "set":
                res = vault.set(name, value or "", description or "", who)
                redacted = journal.redact_secret(name, value)
                journal.record(cap="vault.set", worker=None, identity=_caller_identity(),
                               args={"name": name}, reply={"ok": True, **res}, audit=_caller_audit())
                return json.dumps({"ok": True, **res, "journal_rows_masked": redacted}, indent=2)
            if action == "delete":
                gone = vault.delete(name, who)
                journal.record(cap="vault.delete", worker=None, identity=_caller_identity(),
                               args={"name": name}, reply={"ok": gone}, audit=_caller_audit())
                return json.dumps({"ok": gone} if gone else {"ok": False, "error": f"no secret {name!r}"}, indent=2)
        except KeyError:
            return _fail(f"no secret {name!r}; rook_secret(action='list') shows the names")
        except ValueError as e:
            return _fail(str(e))
        return _fail("actions: list, get, set, delete, log")

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
            call_id: fetch one specific call (a prior reply's ``id``, or
                ``_journal_id`` in the legacy envelope) — returns it WITH its
                full stored output.
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
    async def rook_handoff_save(goal: str = "", thread_id: str | None = None,
                                state: str = "", decisions: list | str | None = None,
                                next_steps: list | str | None = None,
                                artifacts: list | str | None = None,
                                supersedes: list | str | None = None,
                                transcript_ref: str | None = None,
                                task: str | None = None, status: str = "active") -> str:
        """Save a handoff so any agent can pick this session up later.

        A handoff is the structured state of a piece of work — not a full
        transcript. Provide the ``goal``, the current ``state`` (where things
        stand), ``decisions`` made, ``next_steps``, and any ``artifacts``
        touched (files, hosts, URLs). Omit ``thread_id`` to start a new thread
        (one is returned); pass an existing ``thread_id`` to update it — the
        prior handoff becomes history and this becomes current. Write one at the
        end of a work session, or whenever you hand off to another agent.
        ``task`` (id or slug) links it to that task instead of your claimed
        one. ``status="closed"`` with a ``thread_id`` closes the thread
        (``state`` = why); it then leaves rook_handoff_list.
        """
        if status not in ("active", "closed"):
            return json.dumps({"ok": False, "error": "status must be active or closed"})
        if status == "closed":
            if not thread_id:
                return json.dumps({"ok": False, "error": "closing needs thread_id"})
            return json.dumps(sessions.close_thread(thread_id, _caller_identity(), state or goal),
                              indent=2)
        if not str(goal or "").strip():
            return json.dumps({"ok": False, "error": "goal is required"})
        res = sessions.save(
            thread_id=thread_id, author=_caller_identity(), goal=goal, state=state,
            decisions=decisions, next_steps=next_steps, artifacts=artifacts,
            supersedes=supersedes, transcript_ref=transcript_ref)
        if res.get("ok"):
            linked = _auto_link("handoff", res.get("thread_id"), goal[:200], task=task)
            if linked:
                res["task"] = linked
            elif task:
                res["task_error"] = f"task {task!r} not found; the handoff was saved unlinked"
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

    # -- chat rooms (agent backroom) -----------------------------------------

    @mcp.tool()
    async def rook_chat_start(title: str, invite: list | str | None = None) -> str:
        """Start a chat room (a thread) and invite participants.

        ``invite`` is a list of identities (e.g. ``["agent:hermes_nas"]``)
        or a comma string. You're added automatically. Returns the room id —
        use it with rook_chat_send / rook_chat_read. The room id doubles as a
        thread_id shared with handoffs and the journal."""
        inv = ([s.strip() for s in invite.split(",")] if isinstance(invite, str)
               else list(invite or []))
        chat.touch(_caller_identity())
        return json.dumps(chat.start(title, _caller_identity(), inv), indent=2)

    @mcp.tool()
    async def rook_chat_send(room: str, text: str,
                             mention: list | str | None = None,
                             expects_reply: bool = False) -> str:
        """Post a message to a room.

        ``mention`` (list or comma string) is routing metadata, not text: in a
        room of 3+ only mentioned participants are expected to respond; in a
        2-party room the other party is implicit. Mentioning someone not in the
        room auto-invites them. Set ``expects_reply`` when you want an answer.
        The reply tells you who was addressed and which of them are ``offline``
        (they'll get a voicemail notice on their next call; use rook_chat_wake
        to make an offline agent respond now)."""
        ment = ([s.strip() for s in mention.split(",")] if isinstance(mention, str)
                else list(mention or []))
        ident = _caller_identity()
        chat.touch(ident)
        sender = ident if ident not in ("anonymous", "unverified") else "MCP"
        return json.dumps(chat.send(room, sender, text, ment, expects_reply), indent=2)

    @mcp.tool()
    async def rook_chat_read(room: str, since_seq: int = 0) -> str:
        """Read messages in a room newer than ``since_seq`` (0 = from the start)
        and mark them read. Returns messages with their ``seq`` — pass the
        ``last_seq`` back as ``since_seq`` next time to page forward."""
        ident = _caller_identity()
        chat.touch(ident)
        return json.dumps(chat.read(room, ident, since_seq=since_seq), indent=2)

    @mcp.tool()
    async def rook_chat_rooms() -> str:
        """List your chat rooms, newest-active first, with unread counts."""
        ident = _caller_identity()
        chat.touch(ident)
        return json.dumps(chat.rooms_for(ident), indent=2)

    @mcp.tool()
    async def rook_chat_delete(room: str) -> str:
        """Delete a room and all its messages. Only a participant can; this is
        final (rooms are threads, there's no archive)."""
        ident = _caller_identity()
        chat.touch(ident)
        return json.dumps(chat.delete(room, ident), indent=2)

    @mcp.tool()
    async def rook_presence() -> str:
        """Who's reachable: identities seen recently over the MCP (``online``
        if within ~90s) plus, for reference, the live band workers. Use this to
        see who can respond in chat before mentioning or waking them."""
        chat.touch(_caller_identity())
        import time as _t
        now = _t.time()
        workers = [{"name": w.get("name"), "worker_id": wid,
                    "last_seen_age_secs": round(now - w.get("last_seen", 0.0), 1)}
                   for wid, w in client.workers.items()]
        return json.dumps({"agents": chat.online(), "workers": workers}, indent=2)

    @mcp.tool()
    async def rook_chat_wake(room: str, worker: str, note: str | None = None,
                             timeout: float = 20.0) -> str:
        """Wake an agent to respond in a room now (a deliberate act, not a
        mention).

        Two paths, auto-selected by the target's capabilities:
          * ``hermes.chat`` present (a box running hermes): the room
            transcript is handed to hermes.chat and its reply is posted straight
            back into the room — no spawn command needed, it rides the cap the
            worker already exposes. This is how an @mentioned hermes agent actually answers.
          * else ``agent.wake`` present (`ROOK_WAKE_CMD` set): spawns a fresh
            agent session with the transcript as its brief.
        A worker with neither can't be woken — the mention/voicemail notice
        still reaches it on its next call."""
        target, err = _resolve_target(worker)
        if err:
            return _fail(err)
        w = client.workers.get(target, {})
        caps = w.get("caps", [])
        tail = chat.read(room, None, since_seq=0, mark=False, limit=30)
        if not tail.get("ok"):
            return _fail(tail.get("error", "no such room"))
        ident = _caller_identity()
        chat.touch(ident)
        wname = w.get("name") or worker
        transcript = tail.get("messages", [])

        # Preferred for hermes boxes: bridge to the existing hermes.chat cap and
        # relay the reply into the room. No ROOK_WAKE_CMD required.
        if "hermes.chat" in caps:
            convo = "\n".join(f"[{m.get('sender')}] {m.get('text')}"
                              for m in transcript)
            note_line = (f"\nNote from the person waking you: {note}" if note else "")
            prompt = (
                f"You are a participant in the rook chat room "
                f"'{tail.get('title') or room}'. Someone in the room is waiting "
                f"for your reply.{note_line}\n\n"
                f"Respond to the conversation below as yourself, in plain prose "
                f"addressed to the room. Whatever you write back is posted into "
                f"this room for you automatically — do NOT call any rook chat "
                f"tools (rook_chat_start/send/read/wake, chat.*), do not create "
                f"rooms, and do not describe what you did; just answer.\n\n"
                f"--- conversation ---\n{convo}\n--- end ---")
            try:
                reply = await client.call(
                    cap="hermes.chat", args={"message": prompt},
                    target=target, timeout=max(timeout, 120.0), identity=ident)
            except asyncio.TimeoutError:
                return _fail(f"hermes on {worker!r} didn't reply within "
                             f"{max(timeout, 120.0):.0f}s.")
            res = reply.get("result") or {}
            answer = _clean_hermes_stdout(res.get("stdout", "")) if isinstance(res, dict) else ""
            if not answer:
                return json.dumps({"ok": False, "worker": wname,
                                   "error": "hermes returned no parseable reply",
                                   "raw": res}, indent=2)
            posted = chat.send(room, f"agent:hermes_{wname}", answer, [], False)
            return json.dumps({"ok": True, "via": "hermes.chat", "worker": wname,
                               "posted": posted.get("ok"), "reply": answer}, indent=2)

        # Fallback: spawn a fresh session via the wake cap.
        if "agent.wake" in caps:
            wake_args = {"room": room, "thread_id": room, "title": tail.get("title"),
                         "transcript": transcript, "woken_by": ident, "note": note or ""}
            try:
                reply = await client.call(cap="agent.wake", args=wake_args,
                                          target=target, timeout=timeout, identity=ident)
            except asyncio.TimeoutError:
                return _fail(f"agent.wake on {worker!r} did not confirm within "
                             f"{timeout:.0f}s (it may still be spawning).")
            return json.dumps({"ok": True, "via": "agent.wake", "reply": reply}, indent=2)

        return _fail(f"worker {worker!r} exposes neither hermes.chat nor "
                     f"agent.wake — nothing to wake. It still gets a voicemail "
                     f"notice on its next call.")

    # -- console rooms (named, searchable terminal sessions) ------------------

    async def _proc_call(room: dict, cap: str, args: dict, timeout: float = 15.0):
        """Fire a proc.* call at the worker hosting this room's session."""
        return await client.call(cap=cap, args={**args, "handle": room["handle"]},
                                 target=room["worker"], timeout=timeout,
                                 identity=_caller_identity())

    @mcp.tool()
    async def rook_console_open(worker: str, task: str, cmd: str | None = None,
                                argv: list | None = None, cwd: str | None = None,
                                env: dict | None = None, pty: bool = False) -> str:
        """Start a long-running command on a worker as a **console room** — a
        named, band-visible, permanently searchable terminal session.

        Use this instead of ``rook_call("shell.exec", ...)`` whenever the work
        is slow, interactive, or worth remembering. The call returns as soon as
        the process starts; it then keeps running regardless of any timeout,
        and its output accumulates in the room for you and everyone else on the
        band to read at their own pace.

        ``task`` is REQUIRED and becomes the room's title. Write it as the goal,
        not the command — "set up the llama model on gpu-01", not "bash". It is
        the main thing anyone (or any later documentation pass) will search on,
        so a vague title makes the session unfindable forever.

        Pass ``argv`` (a list, no shell, no quoting) or ``cmd`` (a string via
        ``/bin/sh -c``). Set ``pty=True`` for password prompts, REPLs, or
        anything that needs a real tty.

        Then: ``rook_console_read`` for output, ``rook_console_write`` to answer
        a prompt, ``rook_console_close`` with a summary when you're done.
        """
        if not str(task or "").strip():
            return _fail("task is required — name what this session is FOR "
                         "(it becomes the room title and the thing people "
                         "search on later).")
        if not cmd and not argv:
            return _fail("pass cmd or argv")
        target, err = _resolve_target(worker)
        if err:
            return _fail(err)
        w = client.workers.get(target, {})
        if "proc.start" not in w.get("caps", []):
            return _fail(f"worker {w.get('name')!r} has no proc.* capability — "
                         f"it predates console rooms. Update it, or fall back "
                         f"to rook_call('shell.exec').")
        ident = _caller_identity()
        chat.touch(ident)
        args = {k: v for k, v in
                {"cmd": cmd, "argv": argv, "cwd": cwd, "env": env,
                 "pty": pty, "label": task}.items() if v is not None}
        try:
            reply = await client.call(cap="proc.start", args=args, target=target,
                                      timeout=20.0, identity=ident)
        except asyncio.TimeoutError:
            return _fail(f"no reply from {worker!r} starting the session")
        result = reply.get("result") or {}
        if not reply.get("ok") or not result.get("ok"):
            return json.dumps({"ok": False, "stage": "start",
                               "error": result.get("error") or reply.get("error"),
                               "reply": reply}, indent=2)
        opened = console.open(title=task, worker=target,
                              worker_name=w.get("name") or target,
                              handle=result["handle"], cmd=result.get("cmd", ""),
                              pty=bool(result.get("pty")), opened_by=ident)
        opened["pid"] = result.get("pid")
        task_id = _auto_link("console", opened.get("room") or opened.get("id"), task[:200])
        if task_id:
            opened["task"] = task_id
        opened["note"] = ("Session is live. Output is pumped into this room — "
                          "read it with rook_console_read(room). Close it with "
                          "rook_console_close(room, summary=...) when done.")
        return json.dumps(opened, indent=2)

    @mcp.tool()
    async def rook_console_read(room: str, since_seq: int = 0,
                                tail: bool = False, limit: int = 300) -> str:
        """Read a console room's output from ``since_seq`` onward.

        Pass the returned ``last_seq`` back as ``since_seq`` to page forward.
        Set ``tail=True`` to get the LAST ``limit`` lines instead — what you
        want when attaching to a session that already has thousands, or when
        reading an old frozen room to see how it ended.

        Works identically for live and frozen rooms; ``state`` tells you which.
        """
        chat.touch(_caller_identity())
        return json.dumps(console.read(room, since_seq=since_seq, tail=tail,
                                       limit=limit), indent=2)

    @mcp.tool()
    async def rook_console_write(room: str, text: str, newline: bool = True,
                                 literal: bool = False) -> str:
        """Type into a live console room — this is the session's stdin.

        Sent verbatim, with no shell in between, so quotes and ``$`` need no
        escaping. Use it to answer a prompt ("y", a password, a menu choice) or
        to drive a REPL. Read the room afterwards to see what happened.

        ``{{secret:<name>}}`` types that vault value (the transcript keeps
        the placeholder); an unknown name fails. literal=True sends it as
        written.
        """
        r = console.get(room)
        if r is None:
            return _fail(f"no such console room: {room}")
        if r["state"] != "live":
            return _fail(f"room {room} is {r['state']} — its process has exited, "
                         f"nothing is listening. Open a new console.")
        ident = _caller_identity()
        chat.touch(ident)
        # {{secret:name}} types the value (a password prompt) without the
        # agent ever holding it; the transcript echo keeps the placeholder.
        data = text
        if not literal and _vault_mod.PLACEHOLDER.search(text or ""):
            if vault is None:
                return _fail("text uses {{secret:…}} but the vault is unavailable on this hub")
            try:
                data, used = vault.substitute(text, _actor_name(),
                                              via=f"console {room} on {r['worker_name']}",
                                              task=_claimed_task())
            except KeyError as e:
                return _fail(f"unknown secret {e.args[0]!r}; rook_secret(action='list') shows the names")
            for name in used:
                _auto_link("secret", name, f"typed into console {room}")
            console.remember_typed(room, used)
        try:
            reply = await _proc_call(r, "proc.write",
                                     {"data": data, "newline": newline})
        except asyncio.TimeoutError:
            return _fail(f"worker {r['worker_name']!r} did not confirm the write")
        result = reply.get("result") or {}
        if result.get("ok"):
            # Echo it into the transcript so the room shows who typed what.
            console.append(room, f"$ {text}", stream="in", sender=ident)
        return json.dumps(result or reply, indent=2)

    @mcp.tool()
    async def rook_console_signal(room: str, sig: str = "TERM") -> str:
        """Signal a live session's process group: TERM (polite), KILL (hard),
        INT (ctrl-C), HUP. The room and its transcript survive."""
        r = console.get(room)
        if r is None:
            return _fail(f"no such console room: {room}")
        if r["state"] != "live":
            return _fail(f"room {room} is {r['state']} — process already gone.")
        chat.touch(_caller_identity())
        try:
            reply = await _proc_call(r, "proc.signal", {"sig": sig})
        except asyncio.TimeoutError:
            return _fail(f"worker {r['worker_name']!r} did not confirm the signal")
        return json.dumps(reply.get("result") or reply, indent=2)

    @mcp.tool()
    async def rook_console_close(room: str, summary: str | None = None,
                                 kill: bool = False) -> str:
        """Freeze a console room, with a closing summary. **Write the summary.**

        The room becomes permanent and immutable: still readable, still
        searchable, but no longer live. The transcript alone is poor search
        corpus — pip output rarely contains the words anyone will look for
        later — so the summary is what actually makes this session findable and
        what a documentation pass will read first. Say what you were doing,
        what worked, and what to watch out for.

        If the process is still running, pass ``kill=True`` to stop it; without
        that a live session is left alone and the room stays live.
        """
        r = console.get(room)
        if r is None:
            return _fail(f"no such console room: {room}")
        ident = _caller_identity()
        chat.touch(ident)
        if r["state"] == "live":
            if not kill:
                return _fail(f"room {room} is still live (its process is "
                             f"running). Pass kill=True to stop it and freeze "
                             f"the room, or wait for it to exit.")
            try:
                await _proc_call(r, "proc.close", {})
            except Exception:
                pass
            console.mark_closing(room, None)
        if not summary:
            return _fail(f"room {room} is ready to freeze but needs a summary — "
                         f"call again with summary='what this session did and "
                         f"what came of it'. That text is what makes it findable "
                         f"later; the raw transcript is not.")
        return json.dumps(console.freeze(room, summary=summary, by=ident), indent=2)

    @mcp.tool()
    async def rook_console_list(worker: str | None = None,
                                state: str | None = None, limit: int = 50) -> str:
        """List console rooms, newest-active first, across the whole band.

        Filter by ``worker`` (id or name) or ``state`` (``live``, ``closing``,
        ``frozen``). Use this to find what's running right now; use
        ``rook_console_search`` to find what happened in the past.
        """
        chat.touch(_caller_identity())
        return json.dumps(console.list(worker=worker, state=state, limit=limit),
                          indent=2)

    @mcp.tool()
    async def rook_console_search(query: str, worker: str | None = None,
                                  limit: int = 20) -> str:
        """Full-text search every console session ever run on the band.

        This is the band's operational memory: "how did we set up that model on
        gpu-01" finds the room where it happened, even months later, and returns
        its title, closing summary, exit code and the matching ``seq`` so you
        can jump straight to that point with
        ``rook_console_read(room, since_seq=seq-1)``.

        Titles and summaries are indexed alongside the transcript and rank
        highest, so search for the *task* ("cuda driver install", "postgres
        migration") rather than for exact command text. Filter with ``worker``
        to scope to one machine.
        """
        chat.touch(_caller_identity())
        return json.dumps(console.search(query, worker=worker, limit=limit),
                          indent=2)

    # -- worker config (commit-confirmed OTA) --------------------------------

    def _public_worker_env() -> set:
        hub = getattr(mcp, "_rook_hub", None)
        svc = getattr(hub, "settings", None) if hub is not None else None
        if svc is not None:
            return svc.schema.worker_env_names()
        return set()

    @mcp.tool()
    async def rook_config_get(worker: str) -> str:
        """Read a worker's active config overrides + pending/confirm state."""
        target, err = _resolve_target(worker)
        if err:
            return _fail(err)
        try:
            reply = await client.call(cap="worker.config_get", target=target,
                                      timeout=15.0, identity=_caller_identity())
        except asyncio.TimeoutError:
            return _fail(f"no reply from {worker!r}")
        # The band key and pushed env values never come back (settings P19).
        from ..hub.worker_config import masked_reply
        return json.dumps(masked_reply(reply, _public_worker_env()), indent=2)

    @mcp.tool()
    async def rook_config_apply(worker: str, settings: dict,
                                confirm_within: float = 120.0) -> str:
        """Push config to a worker and confirm it, commit-confirmed (design §1).

        ``settings`` may include ``name``, ``announce_interval``, ``log_level``,
        ``hub``, ``psk``, and ``env`` (dict) — e.g. remotely enable the wake cap
        with ``{"env": {"ROOK_WAKE_CMD": "claude -p {prompt_file}"}}`` or the
        memory vault with ``{"env": {"ROOK_MEMORY_VAULT": "/home/you/vault"}}``.

        The worker stages the config, restarts under it, and must be reconfirmed
        within ``confirm_within`` seconds or it AUTO-REVERTS to its prior config.
        This tool drives that: it applies, waits for the worker to come back on
        the band, verifies it, then confirms — so a change that strands the
        worker rolls back on its own. Returns the final state, with the band
        key and env values masked.
        """
        target, err = _resolve_target(worker)
        if err:
            return _fail(err)
        if not isinstance(settings, dict) or not settings:
            return _fail("settings must be a non-empty object")
        from ..hub.worker_config import apply_confirmed
        res = await apply_confirmed(client, target, settings, identity=_caller_identity(),
                                    confirm_within=confirm_within,
                                    public_env=_public_worker_env())
        return json.dumps(res, indent=2)

    # MCP tools generated from hub caps declared tool=True (hub.x -> rook_hub_x).
    # Each is an alias of rook_call on worker "rook"; existing names win.
    if mcp._rook_hub is not None and mcp._rook_hub.attached:
        from ..hub.mcp_tools import register_cap_tools
        register_cap_tools(mcp, mcp._rook_hub,
                           lambda cap, a: rook_call(cap=cap, args=a, worker=HUB_WORKER_NAME))

    mcp._rook_chat = chat  # the /tokens page edits avatars in this store
    mcp._rook_console = console  # _amain starts the pump against this store

    # Agent skill: MCP resources rook://skill/rook* + GET /skill/rook.skill.
    from . import skill as _skill
    _skill.register(mcp, store, lambda: mcp._rook_knowledge,
                    lambda: mcp._rook_hub.plugin("persona") if mcp._rook_hub is not None else None)

    # Agent guidance: server instructions + tool tips applied now (after every
    # tool, including knowledge, is registered); cap tips ride on
    # rook_call replies. Edited from the site via guidance_web.
    _guidance_apply()

    # Persona (hub plugin "persona"): the caller's resolved persona rides
    # after the server guidance in each session's initialize instructions.
    from . import persona_connect
    persona_connect.install(mcp, store, lambda: (mcp._rook_hub.plugin("persona")
                                                 if mcp._rook_hub is not None else None))
    return mcp, store


async def _amain(args) -> None:
    from .memory_diagnostics import install
    uninstall_diagnostics = install()
    from ..remote.enrollment import EnrollmentStore
    enrollment = EnrollmentStore()
    # The environment's key only seeds an empty enrollment database; after
    # that the database (rotated from the dashboard) is authoritative.
    ignored = enrollment.import_config(args.psks, seed_only=True)
    startup_conflicts = []
    if ignored:
        log.warning("ROOK_BAND_PSK/--psk holds %d key(s) the enrollment database does not "
                    "use; ignored (the environment only seeds the first band; rotate keys "
                    "on the dashboard Bands page and remove the variable)", len(ignored))
        from ..core.settings import fingerprint
        startup_conflicts.append({
            "key": "core.band.key", "env": "ROOK_BAND_PSK",
            "note": "ROOK_BAND_PSK holds a key the enrollment database does not use "
                    f"(fp {', '.join(fingerprint(k) for k in ignored)}); ignored. "
                    "The environment only seeds the first band."})
    if not enrollment.transport_psks():
        log.warning("no band key yet: waiting for the dashboard /setup page or ROOK_BAND_PSK "
                    "(the MCP joins bands from the shared enrollment database)")
    client = MultiBandClient(psks=enrollment.transport_psks(), hub_host=args.hub_host,
                             hub_port=args.hub_port)
    await client.start()

    async def watch_enrollment():
        while True:
            try:
                await client.sync_bands(enrollment.transport_psks())
            except Exception:
                log.exception("could not sync band enrollment")
            await asyncio.sleep(2)

    enrollment_task = asyncio.create_task(watch_enrollment())

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
        enrollment=enrollment,
    )
    if mcp._rook_hub is not None:
        mcp._rook_hub.startup_conflicts = startup_conflicts
        mcp._rook_hub.startup_flags = getattr(args, "flag_settings", {})
        await mcp._rook_hub.start()
    app = mcp.streamable_http_app()
    from ..remote.accounts import AccountStore
    from .guidance_web import routes as guidance_routes
    _guidance, _reapply = mcp._rook_guidance
    for route in guidance_routes(_guidance, _reapply, lambda: list(mcp._tool_manager._tools),
                                 AccountStore(enrollment)):
        app.router.routes.insert(0, route)
    from .vault_web import routes as vault_routes
    from . import vault as _vault_mod
    for route in vault_routes(mcp._rook_vault,
                              lambda n, v: mcp._rook_journal.redact_secret(n, v or ""),
                              AccountStore(enrollment)):
        app.router.routes.insert(0, route)
    from ..hub.settings_web import routes as settings_routes
    for route in settings_routes(lambda: getattr(mcp._rook_hub, "settings", None),
                                 AccountStore(enrollment)):
        app.router.routes.insert(0, route)
    if mcp._rook_knowledge is not None:
        # Operator Knowledge page API (proxied by the dashboard). Background
        # embedding indexing runs in the knowledge plugin's start().
        from ..hub.plugins.knowledge.web import routes as knowledge_routes
        for route in reversed(knowledge_routes(mcp._rook_knowledge, AccountStore(enrollment))):
            app.router.routes.insert(0, route)
    hygiene_task = (asyncio.create_task(mcp._rook_hygiene.run())
                    if mcp._rook_hygiene is not None else None)

    # Console pump — drains live worker proc sessions into their console rooms.
    from .console_pump import ConsolePump
    pump = ConsolePump(client, mcp._rook_console)
    pump.start()

    # Wire up WS bridge for remote Telesthete Band workers.
    ws_bridge: WSBandBridge | None = None
    try:
        from .ws_band import WSBandBridge
        # The WS /band bridge forwards raw encrypted packets and is band-agnostic,
        # so a single bridge serves every band; the psk arg is unused.
        ws_bridge = WSBandBridge(app, args.hub_host, args.hub_port,
                                 args.psks[0] if args.psks else "")
        ws_bridge.start()
    except Exception as e:
        log.warning("WS band bridge failed to start: %s", e)

    # /healthz for the watchdog: session-table counters and band roster size.
    # Needs the static token (the public hostname reaches it too).
    from .healthz import route as healthz_route
    app.router.routes.insert(0, healthz_route(mcp._session_manager, client, args.static_token or ""))

    # /internal/mask: the dashboard masks vault values through this process
    # (it holds no vault key itself). Token file beside the stores.
    try:
        from .mask_web import ensure_token as _mask_token, route as _mask_route
        app.router.routes.insert(0, _mask_route(_mask_token(mcp._rook_store_dir)))
    except Exception:
        log.exception("secret mask endpoint unavailable; dashboard writes will not be masked")

    from .account_tokens import build_account_token_routes
    for route in reversed(build_account_token_routes(store, getattr(mcp, "_rook_chat", None))):
        app.router.routes.insert(0, route)

    # /tokens admin UI (mint/list/revoke bearer tokens).
    for r in reversed(build_api_token_routes(store, getattr(mcp, "_rook_chat", None))):
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
        for bg in (hygiene_task,):
            if bg is not None:
                bg.cancel()
                await asyncio.gather(bg, return_exceptions=True)
        enrollment_task.cancel()
        try:
            await enrollment_task
        except asyncio.CancelledError:
            pass
        # Tear down WS bridge before stopping transport.
        await pump.stop()
        if mcp._rook_hub is not None:
            await mcp._rook_hub.stop()
        if ws_bridge is not None:
            await ws_bridge.stop()
        await client.stop()
        uninstall_diagnostics()


def main() -> None:
    from ..paths import data_path
    ap = argparse.ArgumentParser(prog="rook-band-mcp")
    ap.add_argument("--hub", default=os.environ.get("ROOK_HUB", "127.0.0.1:7474"),
                    help="telesthete hub host:port (or env ROOK_HUB)")
    ap.add_argument("--psk", default=os.environ.get("ROOK_BAND_PSK", ""),
                    help="band pre-shared key (or env ROOK_BAND_PSK); optional. It only "
                         "seeds an empty enrollment database: bands, rotations and "
                         "revocations come from the enrollment database the dashboard "
                         "shares (ROOK_DATA_DIR). A comma-separated list seeds several.")
    ap.add_argument("--bind", default=os.environ.get("ROOK_MCP_BIND", "127.0.0.1:8765"),
                    help="HTTP bind host:port for the MCP server (or env ROOK_MCP_BIND)")
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
                    default=os.environ.get("ROOK_MCP_PERSIST")
                    or data_path("oauth.json", "/var/lib/rook-band-mcp/oauth.json"),
                    help="JSON file for persistent API tokens; the journal, chat, "
                         "vault and other stores live beside it (default: "
                         "$ROOK_DATA_DIR/oauth.json, else /var/lib/rook-band-mcp).")
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

    # Settings given as flags lock their key like an environment variable
    # does; recorded so the Settings page can say "flag --bind".
    _flag_keys = {"--hub": "core.mcp.relay", "--bind": "core.mcp.listen",
                  "--allowed-hosts": "core.mcp.allowed_hosts", "--public-url": "core.mcp.public_url",
                  "--admin-password": "core.mcp.admin_password",
                  "--static-token": "core.mcp.static_token", "--psk": "core.band.key",
                  "--persist-path": "core.store.persist_path",
                  "--journal-path": "core.store.journal_path"}
    args.flag_settings = {}
    for flag, key in _flag_keys.items():
        if any(a == flag or a.startswith(flag + "=") for a in sys.argv[1:]):
            args.flag_settings[key] = (flag, getattr(args, flag[2:].replace("-", "_"), None))

    # Zero or more seed PSKs (comma-separated). Without one the MCP serves the
    # bands in the shared enrollment database, like the dashboard (settings P3).
    args.psks = [p.strip() for p in (args.psk or "").split(",") if p.strip()]

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
