"""Account API behind the dashboard's Overview, start page and theme picker:
``/ui/account-api`` (proxied as ``/account/ui/api``).

Any signed-in account may use it, for its own theme and start page; the
knowledge half of the Overview is for the operator account only, like the
Knowledge page. Same session and CSRF rules as the other account APIs.

GET ``?view=state`` (default): the account's theme (with its CSS), the theme
list, its start page layout and the widget types plugins add.
GET ``?view=overview``: rook.hub.plugins.ui.overview.build.
POST ``{action: theme, theme}`` / ``{action: startpage, layout}`` /
``{action: startpage_reset}`` / ``{action: feed, url, limit}``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply
from . import layout as L
from . import overview
from .feeds import FeedError

log = logging.getLogger("rook.hub.plugins.ui.web")

PATH = "/ui/account-api"
UNAVAILABLE = "The dashboard plugin (ui) is not loaded on this hub."


def _actor(user: dict) -> str:
    return "human:" + str(user.get("username") or user["id"])


def _user_value(svc, key: str, uid: str) -> Any:
    if svc is None:
        return None
    try:
        return svc.resolve(key, {"user": uid}).get("value")
    except Exception:
        log.exception("reading %s for an account failed", key)
        return None


def routes(get_node: Callable[[], Any], accounts):
    def parts():
        node = get_node()
        plugin = node.plugin("ui") if node is not None else None
        return node, plugin, getattr(node, "settings", None)

    @account_api(accounts, admin=False, sign_in="Sign in to use the dashboard.")
    async def api(request, user, data):
        node, ui, svc = parts()
        uid = str(user["id"])
        if ui is None:
            return json_error(UNAVAILABLE, 503)
        if data is None:
            view = request.query_params.get("view") or "state"
            if view == "state":
                layout = _user_value(svc, "ui.startpage", uid) or {}
                try:
                    layout = L.check(layout) if layout else L.default()
                except L.LayoutError:
                    layout = L.default()
                theme_id = _user_value(svc, "ui.theme", uid) or "rook-dark"
                return json_reply({"csrf": user["csrf"], "admin": bool(user.get("admin")),
                                   "theme": ui.theme_css(theme_id), "themes": ui.theme_list(),
                                   "startpage": layout, "widgets": ui.widgets(),
                                   "saved": svc is not None})
            if view == "overview":
                know = node.plugin("knowledge")
                service = getattr(know, "service", None)
                if not user.get("admin") or service is None:
                    return json_reply({"knowledge": None,
                                       "reason": "operator" if service is not None else "off"})
                result = await asyncio.to_thread(overview.build, service)
                return json_reply({"knowledge": result})
            return json_error(f"unknown view {view!r}", 400)

        action = data.get("action")
        if action == "feed":
            try:
                feed = await ui.feed(str(data.get("url") or ""), int(data.get("limit") or 8))
            except (FeedError, ValueError, TypeError) as e:
                return json_reply({"ok": False, "error": str(e)})
            return json_reply({"ok": True, "feed": feed})
        if svc is None:
            return json_error("Settings are unavailable on this hub, so this cannot be saved.", 503)
        try:
            if action == "theme":
                theme = str(data.get("theme") or "")
                if theme not in ui.themes():
                    return json_error("Unknown theme.", 400)
                svc.set("ui.theme", theme, scope="user", target=uid, actor=_actor(user),
                        source="ui", note="theme picker")
                return json_reply({"ok": True, "theme": ui.theme_css(theme)})
            if action == "startpage":
                layout = L.check(data.get("layout"))
                svc.set("ui.startpage", layout, scope="user", target=uid, actor=_actor(user),
                        source="ui", note="start page editor")
                return json_reply({"ok": True, "startpage": layout})
            if action == "startpage_reset":
                svc.reset("ui.startpage", scope="user", target=uid, actor=_actor(user), source="ui")
                return json_reply({"ok": True, "startpage": L.default()})
        except L.LayoutError as e:
            return json_error(str(e), 400)
        except (ValueError, TypeError, PermissionError) as e:
            return json_error(str(e), 400)
        return json_error("Use theme, startpage, startpage_reset or feed.", 400)

    return [Route(PATH, api, methods=["GET", "POST"])]
