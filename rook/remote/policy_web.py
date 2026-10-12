"""Dashboard Permissions page: a minimal policy view, editor and explainer.

Until the settings framework owns the policy (``docs/design/settings.md``),
this page edits the same ``policy.json`` the MCP bridge reads (see
:mod:`rook.hub.policy`), through the same store:

* ``GET /permissions``: the page (mode, revision, lint, JSON editor, explain).
* ``GET /api/policy``: document, revision, mode, source, lint, load error,
  ticket status and recent non-allow decisions from the journal.
* ``POST /api/policy``: ``{"policy": {...}, "note"?}``. Band owners (and the
  shared dashboard password, an owner for compatibility) only; same-origin
  only; refuses a document that leaves nobody with admin on ``rook``;
  journaled as ``audit.policy``.
* ``POST /api/policy/explain``: ``{"principal", "cap", "worker"?, "role"?}``.

These routes sit behind the dashboard's auth middleware like every other
``/api`` route; the principal middleware supplies the caller.
"""

from __future__ import annotations

import html
import json
import logging
from urllib.parse import urlparse

from aiohttp import web

from .web_pages import render

log = logging.getLogger("rook.remote.policy_web")

class PolicyWeb:
    def __init__(self, server) -> None:
        self.server = server

    def install(self, app: web.Application) -> None:
        app.router.add_get("/permissions", self.page)
        app.router.add_get("/api/policy", self.get)
        app.router.add_post("/api/policy", self.save)
        app.router.add_post("/api/policy/explain", self.explain)

    def _authz(self):
        band = getattr(self.server, "_band", None)
        authz = getattr(band, "authz", None) or getattr(self.server, "_authz", None)
        if authz is None:
            authz = self.server._authorizer()
        return authz

    def _recent(self, limit: int = 20) -> list[dict]:
        try:
            from ..band_mcp.journal import Journal
            from ..paths import data_path
            import os
            path = data_path("journal.db", "/var/lib/rook-band-mcp/journal.db")
            if not os.path.exists(path):
                return []
            j = Journal(path)
            try:
                rows = j.query(decision="deny", limit=limit) + j.query(decision="would_deny",
                                                                        limit=limit)
            finally:
                j.close()
            rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
            return rows[:limit]
        except Exception:
            log.debug("recent decisions unavailable", exc_info=True)
            return []

    def _status(self, authz) -> dict:
        signer = authz.signer
        enabled = bool(getattr(signer, "enabled", False))
        store = authz.store
        policy = store.current()
        return {"rev": policy.rev, "mode": policy.mode, "source": store.source,
                "error": store.error, "lint": policy.lint(), "tickets": enabled,
                "op_kid": getattr(signer, "kid", None) if enabled else None}

    async def page(self, request: web.Request) -> web.Response:
        authz = self._authz()
        if authz is None:
            return web.Response(status=503, text="permissions unavailable on this hub")
        st = self._status(authz)
        notes = []
        if st["error"]:
            notes.append(f'<p class="bad">policy file invalid, last good rev kept: {html.escape(st["error"])}</p>')
        for m in st["lint"]:
            notes.append(f'<p class="warn">{html.escape(m)}</p>')
        if not st["tickets"]:
            notes.append('<p class="warn">No root signing key on this hub: calls carry no tickets.</p>')
        # Page markup lives in rook/web/permissions.html; render() escapes
        # every slot except the status notes built (and escaped) above.
        page = render("permissions.html", mode=st["mode"], rev=st["rev"],
                      source=st["source"] or "", status="".join(notes),
                      doc=json.dumps(authz.store.current().doc, indent=2),
                      recent=json.dumps(self._recent(), indent=2) or "-")
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def get(self, request: web.Request) -> web.Response:
        authz = self._authz()
        if authz is None:
            return web.json_response({"error": "permissions unavailable"}, status=503)
        return web.json_response({**self._status(authz), "policy": authz.store.current().doc,
                                  "recent": self._recent()}, headers={"Cache-Control": "no-store"})

    @staticmethod
    def _same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True  # non-browser client (curl with the dashboard password)
        return urlparse(origin).netloc == request.host

    async def save(self, request: web.Request) -> web.Response:
        from ..hub.authz import current_principal, require_hub_admin
        from ..hub.policy import PolicyError, summarize_diff
        if not self._same_origin(request):
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        refused = require_hub_admin("policy.set")
        if refused:
            return web.json_response({"error": refused}, status=403)
        authz = self._authz()
        if authz is None:
            return web.json_response({"error": "permissions unavailable"}, status=503)
        try:
            data = await request.json()
            doc = data.get("policy")
            if not isinstance(doc, dict):
                raise PolicyError("policy must be an object")
            old = authz.store.current()
            new = authz.store.save(doc)
        except (ValueError, PolicyError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except OSError as e:
            return web.json_response({"error": f"could not write policy: {e}"}, status=500)
        p = current_principal.get()
        authz.record_event("audit.policy", "rook", {
            "actor": p.id if p else None, "old_rev": old.rev, "new_rev": new.rev,
            "diff": summarize_diff(old.doc, new.doc), "note": str(data.get("note") or "")[:200]})
        return web.json_response({"ok": True, "rev": new.rev, "mode": new.mode, "lint": new.lint()})

    async def explain(self, request: web.Request) -> web.Response:
        from ..hub.authz import target_from_entry
        from ..hub.plugins.policy import _principal
        authz = self._authz()
        if authz is None:
            return web.json_response({"error": "permissions unavailable"}, status=503)
        try:
            data = await request.json()
            principal = _principal(str(data.get("principal") or ""), data.get("role"))
            cap = str(data.get("cap") or "").strip()
            if not cap:
                raise ValueError("cap is required")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        worker = str(data.get("worker") or "").strip() or None
        roster = getattr(getattr(self.server, "_band", None), "workers", {}) or {}
        wid, entry = worker, {"name": worker} if worker else None
        if worker and worker not in roster:
            named = [k for k, w in roster.items() if (w.get("name") or "").lower() == worker.lower()]
            if len(named) == 1:
                wid, entry = named[0], roster[named[0]]
        elif worker:
            entry = roster[worker]
        d = authz.store.current().evaluate([principal], cap, target_from_entry(wid, entry))
        return web.json_response(d.explain())
