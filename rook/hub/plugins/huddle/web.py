"""The dashboard's Huddle view API: ``/huddle/account-api``.

The dashboard proxies ``/account/huddle/api`` here with the account cookie
(rook/remote/knowledge_web.py ``HuddleWeb``), with the same session, operator
and CSRF checks as Knowledge (rook/remote/account_api.py). GET returns the
CSRF token; every POST is ``{csrf, action, ...}``:

* ``list``: every huddle, with open question counts.
* ``view`` {room | project | band, since_seq?}: the status strip, questions
  and messages; opening a project's or band's huddle makes it.
* ``send`` {room, text, mention?}: a plain message from the person.
* ``answer`` {question, text}: answer a pinned question (wakes the asker).
"""
from __future__ import annotations

from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply
from .service import PERSON

PATH = "/huddle/account-api"


def routes(get_node: Callable[[], Any], accounts):
    """``get_node()`` returns the live hub node (or None)."""

    def service():
        node = get_node()
        plugin = node.plugin("huddle") if node is not None else None
        return getattr(plugin, "service", None)

    @account_api(accounts, sign_in="Sign in to see huddles.",
                 operator_only="Huddles need the operator account.")
    async def api(request, user, data):
        svc = service()
        if svc is None:
            return json_error("Huddles are off on this hub.", 503)
        if data is None:
            return json_reply({"csrf": user["csrf"], "you": PERSON})
        action = data.get("action")
        where = {k: (str(data[k]) if data.get(k) else None) for k in ("room", "project", "band")}
        try:
            if action == "list":
                result = {"huddles": svc.huddles()}
            elif action == "view":
                result = await svc.view(PERSON, since_seq=int(data.get("since_seq") or 0),
                                        limit=min(int(data.get("limit") or 100), 300), **where)
            elif action == "send":
                mention = data.get("mention") or []
                if isinstance(mention, str):
                    mention = [m.strip() for m in mention.split(",") if m.strip()]
                result = await svc.say(PERSON, str(data.get("text") or ""), mention, **where)
            elif action == "answer":
                result = await svc.answer(int(data.get("question") or 0), str(data.get("text") or ""),
                                          "human:" + str(user["id"]), str(user.get("name") or user["id"]))
            else:
                return json_error("action must be list, view, send or answer", 400)
        except (ValueError, KeyError, TypeError) as error:
            return json_error(str(error), 400)
        return json_reply({"ok": True, "result": result})

    return [Route(PATH, api, methods=["GET", "POST"])]
