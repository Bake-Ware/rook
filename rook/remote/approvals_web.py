"""The dashboard's Approvals page API (``docs/design/permissions.md`` 3.11).

* ``GET /api/approvals``: pending holds, recent decisions, active spread
  grants and the approval settings. ``?count=1`` returns only
  ``{"pending": n}`` (the navigation badge polls it).
* ``POST /api/approvals/<id>``: ``{"decision": "approve"|"deny",
  "scope": "call"|"spread"?, "note"?}``.
* ``GET /account/approvals/assets/approvals.js``: the page module (static).

Both API routes sit behind the dashboard's own login (the auth middleware
admits only the operator: an admin account or the dashboard password) and
also require a *person* who is a band owner or admin (``human:*`` with
``require_hub_admin``): no API token, agent or integration can approve a
hold, and there is no MCP tool, cap or hub tool for it. Decisions are
same-origin JSON POSTs (the policy page's convention): a cross-site form
cannot send ``application/json`` without a preflight.

The holds live in ``approvals.db`` in the hub data directory; in two-process
mode the dashboard opens the same file the MCP side writes, so it sees every
hold whichever process created it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

log = logging.getLogger("rook.remote.approvals_web")

NO_STORE = {"Cache-Control": "no-store"}
_ASSETS = {"approvals.js": "application/javascript"}


class ApprovalsWeb:
    def __init__(self, server) -> None:
        self.server = server

    def install(self, app: web.Application) -> None:
        app.router.add_get("/api/approvals", self.list)
        app.router.add_post("/api/approvals/{id}", self.decide)
        app.router.add_get("/account/approvals/assets/{name}", self.asset)

    # -- helpers -----------------------------------------------------------
    def _approvals(self):
        server = self.server
        band = getattr(server, "_band", None)
        authz = getattr(band, "authz", None) or getattr(server, "_authz", None)
        if authz is None:
            try:
                authz = server._authorizer()
            except Exception:
                log.exception("approvals: no authorizer")
                authz = None
        return getattr(authz, "approvals", None)

    @staticmethod
    def _person():
        """The signed-in person allowed to decide holds, or None."""
        from ..hub.authz import current_principal, require_hub_admin
        p = current_principal.get()
        if p is None or p.kind != "human" or not p.verified:
            return None
        if require_hub_admin("approving held calls") is not None:
            return None
        return p

    @staticmethod
    def _same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True  # non-browser client (curl with the dashboard password)
        return urlparse(origin).netloc == request.host

    # -- routes ----------------------------------------------------------------
    async def asset(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in _ASSETS:
            raise web.HTTPNotFound()
        path = Path(__file__).parents[1] / "web" / name
        return web.Response(text=path.read_text(encoding="utf-8"), content_type=_ASSETS[name],
                            headers=NO_STORE)

    async def list(self, request: web.Request) -> web.Response:
        if self._person() is None:
            return web.json_response({"error": "Only a band owner or admin can see approvals."},
                                     status=403, headers=NO_STORE)
        ap = self._approvals()
        if ap is None:
            return web.json_response({"error": "approvals are unavailable on this hub"},
                                     status=503, headers=NO_STORE)
        if request.query.get("count"):
            return web.json_response({"pending": ap.queue.count_pending()}, headers=NO_STORE)
        from ..hub.approvals import public
        cfg = ap.config()
        return web.json_response({
            "pending": [public(h, cfg) for h in ap.queue.pending()],
            "recent": [public(h, cfg) for h in ap.queue.recent(50)],
            "grants": ap.queue.grants(),
            "settings": {"enabled": cfg.enabled, "hold": list(cfg.hold),
                         "wait_secs": cfg.wait_secs, "approval_secs": cfg.approval_secs,
                         "spread_max": cfg.spread_max,
                         "spread_window_secs": cfg.spread_window_secs,
                         "spread_grant_secs": cfg.spread_grant_secs,
                         "contact": list(cfg.contact), "contact_worker": cfg.contact_worker,
                         "voice_max_tier": cfg.voice_max_tier},
        }, headers=NO_STORE)

    async def decide(self, request: web.Request) -> web.Response:
        from ..hub.approvals import HoldError, public
        if not self._same_origin(request):
            return web.json_response({"error": "cross-origin request refused"}, status=403,
                                     headers=NO_STORE)
        person = self._person()
        if person is None:
            return web.json_response({"error": "Only a band owner or admin can approve or "
                                               "deny held calls."}, status=403, headers=NO_STORE)
        if request.content_type != "application/json":
            return web.json_response({"error": "send JSON"}, status=415, headers=NO_STORE)
        ap = self._approvals()
        if ap is None:
            return web.json_response({"error": "approvals are unavailable on this hub"},
                                     status=503, headers=NO_STORE)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return web.json_response({"error": "expected a JSON object"}, status=400,
                                     headers=NO_STORE)
        decision = str(data.get("decision") or "")
        if decision not in ("approve", "deny"):
            return web.json_response({"error": "decision must be approve or deny"}, status=400,
                                     headers=NO_STORE)
        scope = str(data.get("scope") or "call")
        try:
            row = ap.decide(request.match_info["id"], decision == "approve", person.id,
                            scope=scope, note=str(data.get("note") or "")[:200])
        except HoldError as e:
            return web.json_response({"error": str(e)}, status=409, headers=NO_STORE)
        return web.json_response({"ok": True, "hold": public(row, ap.config())},
                                 headers=NO_STORE)
