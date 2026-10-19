"""The dashboard's Huddles API: ``/huddle/account-api``.

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
* ``roles``: the roles to pick from, the default roles and the chair's
  identity.

The Roles page and the Memory page's role filter (huddles.md 8, 3.3) use the
``role.*`` caps through here, as this operator account (a band owner, so the
roles plugin's admin gate lets it change roles):

* ``role_list`` (roles, presets, agent hosts, persona profiles),
  ``role_get`` {id}, ``role_history`` {id}, ``role_presets``,
  ``role_memory`` {id, query?, limit?};
* ``role_set`` {role, note?, expect_rev?, dry_run?}, ``role_delete`` {id},
  ``role_move`` {id, host}, ``role_memory_edit`` {id, op: remember|forget,
  text?, kind?, memory_id?}.
"""
from __future__ import annotations

import contextlib
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply
from .service import PERSON

PATH = "/huddle/account-api"
HUDDLE_ACTIONS = ("list", "view", "send", "answer", "start", "card", "archive", "roles")
ROLE_ACTIONS = ("role_list", "role_get", "role_history", "role_presets", "role_memory",
                "role_set", "role_delete", "role_move", "role_memory_edit")


def _role_args(action: str, data: dict) -> dict:
    """The cap arguments a role action may pass (nothing else gets through)."""
    rid = str(data.get("id") or "")
    if action in ("role_get", "role_history"):
        return {"id": rid}
    if action == "role_memory":
        return {"id": rid, "query": str(data.get("query") or ""),
                "limit": max(1, min(int(data.get("limit") or 50), 200))}
    if action == "role_set":
        out: dict = {"role": data.get("role"), "note": str(data.get("note") or "")[:200],
                     "dry_run": bool(data.get("dry_run"))}
        if data.get("expect_rev") is not None:
            out["expect_rev"] = int(data["expect_rev"])
        return out
    if action == "role_delete":
        return {"id": rid, "note": str(data.get("note") or "")[:200]}
    if action == "role_move":
        return {"id": rid, "host": str(data.get("host") or ""),
                "note": str(data.get("note") or "")[:200]}
    if action == "role_memory_edit":
        return {"id": rid, "action": str(data.get("op") or ""), "text": str(data.get("text") or ""),
                "kind": str(data.get("kind") or "fact"),
                "memory_id": str(data.get("memory_id") or "")}
    return {}


def routes(get_node: Callable[[], Any], accounts):
    """``get_node()`` returns the live hub node (or None)."""

    def service():
        node = get_node()
        plugin = node.plugin("huddle") if node is not None else None
        return getattr(plugin, "service", None)

    def plugin(ns: str):
        node = get_node()
        return node.plugin(ns) if node is not None else None

    async def role_action(action: str, data: dict, user: dict) -> Any:
        """A ``role.*`` cap as this operator account: its principal for the
        admin gate and the policy, its name as the actor in the role's history."""
        from ...authz import current_principal, principal_for_user
        node = get_node()
        rp = plugin("role")
        if node is None or rp is None:
            raise LookupError("Roles are off on this hub (the roles plugin is not loaded).")
        cap = "role." + action[len("role_"):]
        principal = principal_for_user(user, True)
        tok = current_principal.set(principal)
        try:
            result = await node.invoke(cap, _role_args(action, data),
                                       "human:" + str(user.get("username") or user["id"]))
        finally:
            current_principal.reset(tok)
        if action == "role_list":
            home = plugin("home")
            hosts = []
            with contextlib.suppress(Exception):
                hosts = home.agents.hosts() if home is not None else []
            personas = []
            pp = plugin("persona")
            with contextlib.suppress(Exception):
                personas = [{"id": p["id"], "name": p.get("name") or p["id"]}
                            for p in pp.store.profiles()] if pp is not None else []
            result = {**result, "presets": rp.presets()["presets"], "hosts": hosts,
                      "personas": personas,
                      "chair": home.identity if home is not None else None}
        return result

    @account_api(accounts, sign_in="Sign in to see huddles.",
                 operator_only="Huddles need the operator account.")
    async def api(request, user, data):
        if data is None:
            return json_reply({"csrf": user["csrf"], "you": PERSON,
                               "huddles": service() is not None,
                               "roles": plugin("role") is not None})
        action = data.get("action")
        if action in ROLE_ACTIONS:
            try:
                return json_reply({"ok": True, "result": await role_action(action, data, user)})
            except PermissionError as error:
                return json_error(str(error), 403)
            except LookupError as error:
                return json_error(str(error), 503)
            except (ValueError, KeyError, TypeError) as error:
                return json_error(str(error), 400)
        svc = service()
        if svc is None:
            return json_error("Huddles are off on this hub.", 503)
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
                rp, home, hp = plugin("role"), plugin("home"), plugin("huddle")
                defaults = list((hp.settings.get("default_roles") if hp is not None else None)
                                or [])
                result = {"roles": rp.store.list() if rp is not None else [],
                          "default_roles": defaults,
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
                return json_error("action must be one of " + ", ".join(HUDDLE_ACTIONS + ROLE_ACTIONS),
                                  400)
        except PermissionError as error:
            return json_error(str(error), 403)
        except (ValueError, KeyError, TypeError) as error:
            return json_error(str(error), 400)
        return json_reply({"ok": True, "result": result})

    return [Route(PATH, api, methods=["GET", "POST"])]
