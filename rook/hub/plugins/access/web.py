"""Account API behind the dashboard's People page: ``/people/account-api``.

The dashboard proxies ``/account/people/api`` here with the account cookie
(same session and CSRF rules as Knowledge and Jobs). Any signed-in account
may use it; what it sees depends on who it is:

* **an owner** (the operator account, or an owner in access.db) sees everyone
  with their access in plain words, what waits on the owners (requests in the
  person's own words, people with no access yet, new machines and bands
  nobody has said who gets) and the history, and changes it: ``grant``,
  ``revoke``, ``role``, ``settle``. Each runs the matching ``users.*`` cap in
  process as this owner, so the caps' own checks and history apply; the
  words kept are "<name> on the People page" plus the owner's note.
  A sign-in agents could also use (a weak session) can look but not change.
* **anyone else** sees their own access and asks for more in their own words
  (``request``: ``users.request``).

GET returns the view; POST ``{csrf, action, ...}`` one change and the new view.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_reply
from ....remote.account_web import COOKIE
from ... import access as _access
from ...authz import current_principal, principal_for_user

log = logging.getLogger("rook.hub.plugins.access.web")

PATH = "/people/account-api"
UNAVAILABLE = "Access is not set up on this hub (no data directory)."
WEAK = ("This sign-in also works for agents, so it can't change who may do what. Sign in with "
        "Google or a password only you know.")
OWNER_ACTIONS = ("grant", "revoke", "role", "settle")
NOTE_MAX = 500


def _store(node: Any) -> _access.AccessStore | None:
    store = getattr(getattr(getattr(node, "client", None), "authz", None), "access", None)
    return store if isinstance(store, _access.AccessStore) else None


def _choices(node: Any, store: _access.AccessStore) -> dict:
    roster = dict(getattr(getattr(node, "client", None), "workers", {}) or {})
    machines = sorted(({"id": w, "name": str(e.get("name") or w)} for w, e in roster.items()
                       if "is_hub" not in (e.get("roles") or ()) and e.get("name") != "rook"),
                      key=lambda m: m["name"].casefold())
    bands = []
    with contextlib.suppress(Exception):
        with store.db(False) as db:
            bands = [{"id": r["id"], "name": r["name"] or r["id"]}
                     for r in db.execute("SELECT id, name FROM bands ORDER BY name")]
    return {"machines": machines, "bands": bands, "kinds": list(_access.KINDS),
            "tiers": ["read", "write", "exec", "admin"], "default_tier": _access.DEFAULT_TIER}


def routes(get_node: Callable[[], Any], accounts):
    """``get_node()`` returns the live hub node (or None)."""

    async def run(node, principal, cap: str, args: dict) -> Any:
        tok = current_principal.set(principal)
        try:
            return await node.invoke(cap, args, principal.id)
        finally:
            current_principal.reset(tok)

    async def view(node, store, principal, owner: bool, user: dict) -> dict:
        if not owner:
            me = await run(node, principal, "users.me", {})
            mine = store.requests(("open", "asked"), user_id=str(user["id"]))
            return {"owner": False, "me": me,
                    "request": {k: mine[0][k] for k in ("id", "words", "state", "created")}
                    if mine else None}
        listed = await run(node, principal, "users.list", {})
        hist = await run(node, principal, "users.history", {"limit": 40})
        return {"owner": True, **listed, "history": hist["history"],
                "choices": _choices(node, store), "me": user["id"]}

    @account_api(accounts, admin=False, sign_in="Sign in to see who may use what.")
    async def api(request, user, body):
        node = get_node()
        store = _store(node)
        if node is None or store is None:
            return json_reply({"error": UNAVAILABLE, "csrf": user["csrf"]}, 503)
        owner = bool(user.get("admin"))
        if not owner:
            with contextlib.suppress(Exception):
                owner = store.is_owner(str(user["id"]))
        principal = principal_for_user(user, owner)
        weak = True
        with contextlib.suppress(Exception):
            weak = bool(accounts.session_weak(request.cookies.get(COOKIE, "")))
        try:
            if body is not None:
                action = str(body.get("action") or "")
                if action in OWNER_ACTIONS:
                    if not owner:
                        return json_reply({"error": "Only an owner changes who may do what.",
                                           "csrf": user["csrf"]}, 403)
                    if weak:
                        return json_reply({"error": WEAK, "csrf": user["csrf"]}, 403)
                    name = str(user.get("name") or user.get("username") or user["id"])
                    note = " ".join(str(body.get("note") or "").split())[:NOTE_MAX]
                    words = f"{name} on the People page" + (f": {note}" if note else "")
                    args = {"words": words}
                    if action == "grant":
                        args.update({k: body[k] for k in ("user", "kind", "ref", "caps", "tier")
                                     if body.get(k) not in (None, "", [])})
                    elif action == "revoke":
                        args["grant"] = str(body.get("grant") or "")
                    elif action == "role":
                        args.update(user=str(body.get("user") or ""),
                                    role=str(body.get("role") or ""))
                    else:
                        args["what"] = str(body.get("what") or "")
                    await run(node, principal, f"users.{action}", args)
                    _settled(node)
                elif action == "request":
                    await run(node, principal, "users.request", {"words": body.get("words")})
                else:
                    return json_reply({"error": f"unknown action {action!r}",
                                       "csrf": user["csrf"]}, 400)
            data = await view(node, store, principal, owner, user)
        except PermissionError as e:
            return json_reply({"error": str(e), "csrf": user["csrf"]}, 403)
        except (ValueError, KeyError, TypeError, LookupError) as e:
            return json_reply({"error": str(e), "csrf": user["csrf"]}, 400)
        except Exception:
            log.exception("people account API failed")
            return json_reply({"error": "The request failed; see the MCP log.",
                               "csrf": user["csrf"]}, 500)
        return json_reply({"csrf": user["csrf"], "can_edit": owner and not weak, **data})

    return [Route(PATH, api, methods=["GET", "POST"])]


def _settled(node: Any) -> None:
    """A change here may answer a question the house agent asked: close it."""
    home = node.plugin("home") if node is not None else None
    front = getattr(home, "front", None)
    with contextlib.suppress(Exception):
        if front is not None:
            front.access._close_settled("an owner on the People page")
