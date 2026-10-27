"""Account API behind the dashboard's Feed page: ``/attention/account-api``
(proxied as ``/account/attention/api``, rook/remote/knowledge_web.py
AttentionWeb).

Owner accounts only, same session and CSRF rules as the other account APIs:
the Feed holds the summaries of the person's phone notifications.

GET ``?before=&source=&important=&limit=``: :meth:`AttentionService.stream`.
POST ``{action: sort, app, pkg?, priority}`` (urgent, family, normal or
ignore) or ``{action: unsort, id}``: the person's say over how the phones sort
an app; the next round pushes it to every phone.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply

log = logging.getLogger("rook.hub.plugins.attention.web")

PATH = "/attention/account-api"
UNAVAILABLE = "The Feed is unavailable: the attention plugin is not loaded on this hub."


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(n) else max(low, min(n, high))


def routes(get_node: Callable[[], Any], accounts):
    @account_api(accounts, sign_in="Sign in to see your Feed.",
                 operator_only="Only an owner can see the Feed.")
    async def api(request, user, data):
        node = get_node()
        plugin = node.plugin("attention") if node is not None else None
        if plugin is None:
            return json_error(UNAVAILABLE, 503)
        svc = plugin.service
        try:
            if data is None:
                q = request.query_params
                source = q.get("source") or ""
                if source and source not in ("phone", "alert", "decision", "work"):
                    return json_error("source: phone, alert, decision or work", 400)
                # On the loop, like the cap: it reads the approvals relay and the plugin's
                # state, which live here; the knowledge read is one bounded query.
                out = svc.stream(int(_number(q.get("limit"), 60, 1, 200)),
                                 _number(q.get("before"), 0, 0, 1e11), source,
                                 q.get("important") in ("1", "true"), plugin.knowledge())
                return json_reply({"csrf": user["csrf"], **out})
            by = f"{user.get('username') or 'the owner'} on the dashboard"
            action = data.get("action")
            if action == "sort":
                row = svc.sort(data.get("app"), data.get("pkg") or "", data.get("priority"), by)
                return json_reply({"ok": True, "sorted": row})
            if action == "unsort":
                if not svc.unsort(str(data.get("id") or ""), by):
                    return json_error("That sorting is already gone.", 404)
                return json_reply({"ok": True})
            return json_error("Use sort or unsort.", 400)
        except ValueError as e:
            return json_error(str(e), 400)
        except Exception:
            log.exception("attention account API failed")
            return json_error("The request failed; see the hub log.", 500)

    return [Route(PATH, api, methods=["GET", "POST"])]
