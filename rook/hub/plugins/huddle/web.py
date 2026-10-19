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

Chaired huddles (docs/design/huddles.md 5, 8):

* ``list`` {archived?}: archived ones too.
* ``start`` {title, roles?}: a new chaired huddle (roles: ids; none: the
  default roles; [] for a 1:1 with the house agent).
* ``send`` in a chaired huddle is a post: it starts a round.
* ``card`` {room, card, decision: approve|dismiss}: approving runs the card's
  call as this account, under the band's policy.
* ``archive`` {room, archived}.
* ``roles``: the roles to pick from, and the chair's identity.
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
                result = {"huddles": svc.huddles(archived=bool(data.get("archived")))}
            elif action == "start":
                roles = data.get("roles")
                result = await svc.start(PERSON, str(data.get("title") or ""),
                                         roles if isinstance(roles, list) else None)
            elif action == "card":
                from ...authz import principal_for_user
                result = await svc.card(str(data.get("room") or ""), str(data.get("card") or ""),
                                        str(data.get("decision") or ""), "human:" + str(user["id"]),
                                        principal_for_user(user, True))
            elif action == "archive":
                result = svc.archive(str(data.get("room") or ""), bool(data.get("archived", True)))
            elif action == "roles":
                node = get_node()
                rp = node.plugin("role") if node is not None else None
                home = node.plugin("home") if node is not None else None
                result = {"roles": rp.store.list() if rp is not None else [],
                          "chair": home.identity if home is not None else None,
                          "chair_on": bool(home is not None and home.enabled())}
            elif action == "view":
                result = await svc.view(PERSON, since_seq=int(data.get("since_seq") or 0),
                                        limit=min(int(data.get("limit") or 100), 300),
                                        create=True, targets=True, **where)
            elif action == "send":
                mention = data.get("mention") or []
                if isinstance(mention, str):
                    mention = [m.strip() for m in mention.split(",") if m.strip()]
                if where["room"] and svc.store.chaired(where["room"]):
                    result = await svc.post(PERSON, where["room"], str(data.get("text") or ""),
                                            mention)
                else:
                    result = await svc.say(PERSON, str(data.get("text") or ""), mention, **where)
            elif action == "answer":
                result = await svc.answer(int(data.get("question") or 0), str(data.get("text") or ""),
                                          "human:" + str(user["id"]), str(user.get("name") or user["id"]))
            else:
                return json_error("action must be list, view, send, answer, start, card, "
                                  "archive or roles", 400)
        except PermissionError as error:
            return json_error(str(error), 403)
        except (ValueError, KeyError, TypeError) as error:
            return json_error(str(error), 400)
        return json_reply({"ok": True, "result": result})

    return [Route(PATH, api, methods=["GET", "POST"])]
