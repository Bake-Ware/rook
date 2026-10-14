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
import math
import time
from collections import deque
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply
from . import layout as L
from . import overview
from .feeds import FeedError

log = logging.getLogger("rook.hub.plugins.ui.web")

PATH = "/ui/account-api"
UNAVAILABLE = "The dashboard plugin (ui) is not loaded on this hub."
#: Saves (theme and start page) one account may make per window; the start
#: page editor saves once per Save click, so this only stops a runaway client.
SAVES_PER_WINDOW, SAVE_WINDOW = 30, 600.0
#: History entries kept per account for these settings: each start page save
#: stores the whole layout, so the history of a busy account would grow.
HISTORY_KEEP = 20


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


def _limit(value) -> int:
    """A feed's item count from the request: 1 to 30, or ValueError."""
    if value is None or value == "":
        return 8
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("limit must be a number from 1 to 30")
    return max(1, min(int(value), 30))


def routes(get_node: Callable[[], Any], accounts):
    saves: dict[str, deque] = {}

    def save_allowed(uid: str) -> bool:
        now = time.monotonic()
        recent = saves.setdefault(uid, deque())
        while recent and now - recent[0] > SAVE_WINDOW:
            recent.popleft()
        if len(recent) >= SAVES_PER_WINDOW:
            return False
        recent.append(now)
        return True

    def save(svc, key, value, uid, user, note):
        """Store a user-scope value unless it is unchanged, and keep its
        history short. Returns whether anything was written."""
        if _user_value(svc, key, uid) == value and svc.store.get(key, "user", uid) is not None:
            return False
        svc.set(key, value, scope="user", target=uid, actor=_actor(user), source="ui", note=note)
        try:
            svc.store.prune_history(key, "user", uid, keep=HISTORY_KEEP)
        except Exception:
            log.exception("pruning the history of %s failed", key)
        return True

    def parts():
        node = get_node()
        plugin = node.plugin("ui") if node is not None else None
        return node, plugin, getattr(node, "settings", None)

    @account_api(accounts, admin=False, sign_in="Sign in to use the dashboard.")
    async def api(request, user, data):
        try:
            return await handle(request, user, data)
        except ImportError:
            log.exception("ui account API: a dependency is missing")
            return json_error("This hub is missing a component this needs (see the hub log).", 503)
        except Exception:
            log.exception("ui account API failed")
            return json_error("The request failed; see the hub log.", 500)

    async def handle(request, user, data):
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
                limit = _limit(data.get("limit"))
            except ValueError as e:
                return json_error(str(e), 400)
            try:
                feed = await ui.feed(str(data.get("url") or ""), limit, account=uid)
            except FeedError as e:
                return json_reply({"ok": False, "error": str(e)})
            return json_reply({"ok": True, "feed": feed})
        if svc is None:
            return json_error("Settings are unavailable on this hub, so this cannot be saved.", 503)
        if action in ("theme", "startpage", "startpage_reset") and not save_allowed(uid):
            return json_error("Too many saves in a short time; wait a few minutes and try again.", 429)
        try:
            if action == "theme":
                theme = str(data.get("theme") or "")
                if theme not in ui.themes():
                    return json_error("Unknown theme.", 400)
                saved = save(svc, "ui.theme", theme, uid, user, "theme picker")
                return json_reply({"ok": True, "saved": saved, "theme": ui.theme_css(theme)})
            if action == "startpage":
                layout = L.check(data.get("layout"))
                saved = save(svc, "ui.startpage", layout, uid, user, "start page editor")
                return json_reply({"ok": True, "saved": saved, "startpage": layout})
            if action == "startpage_reset":
                svc.reset("ui.startpage", scope="user", target=uid, actor=_actor(user), source="ui")
                svc.store.prune_history("ui.startpage", "user", uid, keep=HISTORY_KEEP)
                return json_reply({"ok": True, "startpage": L.default()})
        except L.LayoutError as e:
            return json_error(str(e), 400)
        except (ValueError, TypeError, PermissionError) as e:
            return json_error(str(e), 400)
        return json_error("Use theme, startpage, startpage_reset or feed.", 400)

    return [Route(PATH, api, methods=["GET", "POST"])]
