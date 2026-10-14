"""Account API behind the dashboard's Help page and the manual update prompt:
``/manual/account-api`` (proxied from ``/account/manual/api``).

GET ``?view=status`` | ``pages[&full=1]`` | ``page&id=<page id>[&source=bundle]``: any
signed-in account (the manual is not secret; the update prompt is only
offered to the operator). POST ``{"action": "update" | "snooze" |
"decline", "overwrite": [page ids]}``: operator account only, with the
session's CSRF token.
"""
from __future__ import annotations

from typing import Any, Callable

from starlette.routing import Route

from ....core.pluginkit import CapError
from ....remote.account_api import account_api, json_error, json_reply

PATH = "/manual/account-api"
UNAVAILABLE = "The manual plugin is not loaded on this hub."


def routes(get_plugin: Callable[[], Any], accounts):
    """``get_plugin()`` returns the loaded ``manual`` hub plugin or None."""

    @account_api(accounts, admin=False, sign_in="Sign in to read the manual.")
    async def api(request, user, body):
        plugin = get_plugin()
        if plugin is None:
            return json_error(UNAVAILABLE, 503)
        admin = bool(user.get("admin"))
        try:
            if body is None:
                q = request.query_params
                view = q.get("view") or "status"
                if view == "status":
                    data = plugin.status_view()
                    if not admin:
                        data = {k: data[k] for k in ("version", "pages", "knowledge", "imported")}
                        data["prompt"] = False
                elif view == "pages":
                    data = plugin.list_pages()
                    if q.get("full") in ("1", "true"):     # bodies too, for search
                        for row in data["pages"]:
                            row["body"] = plugin.get(row["id"])["body"]
                elif view == "page":
                    data = plugin.get(q.get("id") or "", q.get("source") or "auto")
                else:
                    return json_error(f"unknown view {view!r}", 400)
                return json_reply({"csrf": user["csrf"], "admin": admin, **data})
            if not admin:
                return json_error("Only the operator account can update the manual copy.", 403)
            who = "human:" + str(user.get("username") or user["id"])
            overwrite = body.get("overwrite")
            result = await plugin.do_update(str(body.get("action") or ""),
                                            overwrite if isinstance(overwrite, list) else None, who)
            return json_reply({"ok": True, "result": result})
        except (CapError, ValueError, KeyError) as error:
            return json_error(str(error), 400)

    return [Route(PATH, api, methods=["GET", "POST"])]
