"""The dashboard's Trust page API (docs/design/trust.md, rook/hub/trust.py).

* ``GET /api/trust``: the map (entries, exceptions, the borrowed-state
  ledger, proposals, the house agent's unacknowledged changes), the summary
  agents get, and recent history. ``?count=1`` returns only
  ``{"waiting": n}`` (pending proposals + unacknowledged house changes; the
  navigation badge polls it).
* ``POST /api/trust``: ``{"op", "data", "words"}``, one change, recorded with
  the person's words (required).
* ``GET /account/trust/assets/trust.js``: the page module (static).

Both API routes need a *person* who is a band owner or admin, as the
Approvals page does (same login, same weak-credential refusal, same-origin
JSON POSTs). This is the only place the trust map is loosened: no cap, MCP
tool, relay or setting does that.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from .approvals_web import ApprovalsWeb

log = logging.getLogger("rook.remote.trust_web")

NO_STORE = {"Cache-Control": "no-store"}
_ASSETS = {"trust.js": "application/javascript"}


class TrustWeb:
    def __init__(self, server) -> None:
        self.server = server
        self._ap = ApprovalsWeb(server)

    def install(self, app: web.Application) -> None:
        app.router.add_get("/api/trust", self.get)
        app.router.add_post("/api/trust", self.post)
        app.router.add_get("/account/trust/assets/{name}", self.asset)

    def _store(self):
        ap = self._ap._approvals()
        st = getattr(ap, "trust", None)
        if st is None:
            from ..hub.trust import default_path, store_for
            st = store_for(default_path())
        return st

    @staticmethod
    def _err(msg: str, status: int) -> web.Response:
        return web.json_response({"error": msg}, status=status, headers=NO_STORE)

    async def asset(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in _ASSETS:
            raise web.HTTPNotFound()
        path = Path(__file__).parents[1] / "web" / name
        return web.Response(text=path.read_text(encoding="utf-8"), content_type=_ASSETS[name],
                            headers=NO_STORE)

    async def get(self, request: web.Request) -> web.Response:
        if self._ap._person() is None:
            return self._err("Only a band owner or admin can see the trust map.", 403)
        st = self._store()
        doc = st.public()
        waiting = doc["pending_proposals"] + len(doc.get("notices") or ())
        if request.query.get("count"):
            return web.json_response({"waiting": waiting}, headers=NO_STORE)
        weak = await self._ap._weak(request)
        return web.json_response({
            "map": doc, "summary": st.summary(), "history": st.history(60),
            "waiting": waiting, "can_edit": not weak,
            "weak_message": ApprovalsWeb.WEAK_MSG.replace("approve held calls",
                                                          "change the trust map") if weak else None,
        }, headers=NO_STORE)

    async def post(self, request: web.Request) -> web.Response:
        from ..hub.trust import Actor, TrustError
        if not ApprovalsWeb._same_origin(request):
            return self._err("cross-origin request refused", 403)
        person = self._ap._person()
        if person is None:
            return self._err("Only a band owner or admin can change the trust map.", 403)
        if await self._ap._weak(request):
            return self._err(ApprovalsWeb.WEAK_MSG, 403)
        if request.content_type != "application/json":
            return self._err("send JSON", 415)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._err("expected a JSON object", 400)
        actor = Actor("person", str(person.label or person.id), principal=person.id,
                      words=str(data.get("words") or ""), session="dashboard")
        try:
            out = self._store().change(str(data.get("op") or ""), data.get("data") or {}, actor)
        except TrustError as e:
            return self._err(str(e), 409)
        log.info("trust map v%s: %s %s by %s", out["version"], out["op"], out["id"], person.id)
        ap = self._ap._approvals()
        if ap is not None:
            ap._journal("trust.change", "rook", person.id,
                        {"ok": True, "version": out["version"], "op": out["op"], "id": out["id"],
                         "words": actor.words[:300]})
        return web.json_response({"ok": True, **out}, headers=NO_STORE)
