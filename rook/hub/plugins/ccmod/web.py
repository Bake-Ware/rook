"""Account API behind Manage > Claude Code: ``/ccmod/account-api``.

Operator account only (``rook.remote.account_api``: session and CSRF); the
dashboard proxies ``/account/ccmod/api`` here (rook/remote/knowledge_web.py
CcmodWeb). Every change is attributed to ``human:<username>``.

GET ``?view=page`` (rules, workers, rooms, choices, settings, activity) |
``preview`` (``worker``, ``remove=1``): what a push would write there.
POST ``{"action": ...}``: ``save`` (``rule``, ``rev``, ``dry_run``),
``delete`` (``id``), ``toggle`` (``id``, ``enabled``), ``push`` (``worker``?)
and ``remove`` (``worker``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_reply
from ....worker import ccmod_hook as hook
from . import model

log = logging.getLogger("rook.hub.plugins.ccmod.web")

PATH = "/ccmod/account-api"
UNAVAILABLE = ("Claude Code rules are unavailable: the ccmod hub plugin is off or failed to start "
               "(Manage > Plugins).")


def choices() -> dict:
    """The editor's vocabulary, in plain words."""
    return {
        "events": [{"id": e, "label": model.EVENT_LABELS[e],
                    "group": ("rook" if e in model.ROOK_EVENTS else
                              "worker" if e == hook.IDLE else "claude"),
                    "tool": e in hook.TOOL_EVENTS, "text": e in model.TEXT_EVENTS,
                    "minutes": e == hook.IDLE,
                    "actions": [a for a in model.ACTIONS if _fits(a, e)]} for e in model.EVENTS],
        "actions": [{"id": a, "label": model.ACTION_LABELS[a],
                     "where": "hub" if a in model.REMOTE else "claude"} for a in model.ACTIONS],
        "scopes": list(model.SCOPES),
        "fields": list(hook.FIELDS),
    }


def _fits(action: str, event: str) -> bool:
    if action in model.LOCAL and event not in hook.HOOK_EVENTS:
        return False
    if action == "block":
        return event in hook.BLOCKABLE
    if action == "inject":
        return event in hook.INJECTABLE
    return True


def page(plugin) -> dict:
    rules = plugin.store.rules()
    roster = plugin.roster()
    rooms = []
    chat = plugin._node.plugin("chat") if plugin._node is not None else None
    store = getattr(chat, "_store", None)
    if store is not None and getattr(store, "enabled", False):
        try:
            got = store.rooms_for("user:operator", limit=100, include_all=True)
            rooms = [{"id": r["room"], "title": r["title"]} for r in got.get("rooms") or ()]
        except Exception:
            log.exception("ccmod: listing chat rooms failed")
    pol = plugin.policy()
    return {
        "rev": plugin.store.rev,
        "rules": [{**r, "summary": model.describe(r)} for r in rules],
        "workers": plugin.status_rows(),
        "bands": sorted({str(w.get("band")) for w in roster.values() if w.get("band")}),
        "rooms": rooms,
        "huddles": plugin._node is not None and plugin._node.plugin("huddle") is not None,
        "choices": choices(),
        "policy": {"allowlist": pol.allowlist, "allow_exec": pol.allow_exec},
        "activity": plugin.store.activity(limit=60),
        "history": plugin.store.history(limit=30),
    }


def routes(get_plugin: Callable[[], Any], accounts):
    """``get_plugin()`` returns the live ccmod hub plugin (or None)."""

    @account_api(accounts, sign_in="Sign in to manage Claude Code rules.",
                 operator_only="Only the operator account can manage Claude Code rules.")
    async def api(request, user, body):
        plugin = get_plugin()
        if plugin is None:
            return json_reply({"error": UNAVAILABLE, "csrf": user["csrf"]}, 503)
        actor = "human:" + str(user.get("username") or user["id"])
        try:
            if body is None:
                q = request.query_params
                view = q.get("view") or "page"
                if view == "page":
                    data = page(plugin)
                elif view == "preview":
                    data = await plugin.preview(q.get("worker") or "", q.get("remove") in ("1", "true"))
                else:
                    return json_reply({"error": f"unknown view {view!r}"}, 400)
                return json_reply({"csrf": user["csrf"], **data})
            action = str(body.get("action") or "")
            if action == "save":
                res = plugin.save_rule(body.get("rule"), actor, str(body.get("note") or ""),
                                       body.get("rev"), bool(body.get("dry_run")))
            elif action == "delete":
                res = plugin.delete_rule(str(body.get("id") or ""), actor)
            elif action == "toggle":
                cur = plugin.store.get(str(body.get("id") or ""))
                if cur is None:
                    raise KeyError("no such rule")
                cur["enabled"] = bool(body.get("enabled"))
                res = plugin.save_rule(cur, actor, "switched " + ("on" if cur["enabled"] else "off"),
                                       cur.get("rev"))
            elif action == "push":
                res = await plugin.push(str(body.get("worker") or ""), actor)
            elif action == "remove":
                worker = str(body.get("worker") or "")
                if not worker:
                    return json_reply({"error": "worker is required"}, 400)
                res = await plugin.push(worker, actor, remove=True)
            else:
                return json_reply({"error": "Use save, delete, toggle, push or remove"}, 400)
            return json_reply(res)
        except (KeyError, LookupError) as e:
            return json_reply({"error": str(e).strip("'\"")}, 404)
        except (ValueError, TypeError, PermissionError) as e:
            return json_reply({"error": str(e)}, 400)
        except Exception:
            log.exception("ccmod account API failed")
            return json_reply({"error": "The request failed; see the hub log."}, 500)

    return [Route(PATH, api, methods=["GET", "POST"])]
