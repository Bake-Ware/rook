"""Account API behind the dashboard's Plugins page: ``/plugins/account-api``.

Operator account only, with the same session and CSRF rules as the other
account APIs (``rook.remote.account_api``); the dashboard proxies
``/account/plugins/api`` here (rook/remote/knowledge_web.py PluginsWeb).
Every change is journaled with the operator's identity and switches on
workers are attributed to it on the worker too.

GET ``?view=matrix`` (``band``, ``refresh=1``) | ``presets`` | ``catalog``.
POST ``{"action": ...}``: ``toggle`` (``target`` hub or a worker, ``module``,
``enable``), ``toggle_all`` (``module``, ``enable``, ``workers?``), ``reload``
(``target``, ``module``), ``default`` (``band``, ``module``, ``value``
true/false/null), ``apply_presets`` (``presets``, ``band``) and
``marketplace_install`` (always refused: not implemented).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.routing import Route

from ..remote.account_api import account_api, json_reply

log = logging.getLogger("rook.hub.plugins_web")

PATH = "/plugins/account-api"
UNAVAILABLE = ("Plugins are unavailable: hub plugins are off (ROOK_HUB_PLUGINS=0) or "
               "failed to start.")


def routes(get_node: Callable[[], Any], accounts):
    """``get_node()`` returns the live hub node (or None)."""
    from .plugin_admin import admin_for

    @account_api(accounts, sign_in="Sign in to manage plugins.",
                 operator_only="Only an owner can manage plugins.")
    async def api(request, user, body):
        node = get_node()
        if node is None:
            return json_reply({"error": UNAVAILABLE, "csrf": user["csrf"]}, 503)
        admin = admin_for(node)
        actor = "human:" + str(user.get("username") or user["id"])
        try:
            if body is None:
                q = request.query_params
                view = q.get("view") or "matrix"
                refresh = q.get("refresh") in ("1", "true")
                if view == "matrix":
                    data = await admin.matrix(band=q.get("band") or None, refresh=refresh)
                elif view == "presets":
                    band = admin.resolve_band(q.get("band") or None)
                    data = {"presets": admin.presets(band), "band": band,
                            "bands": [b for b in admin._bands() if b.get("active")]}
                elif view == "catalog":
                    data = await admin.catalog(refresh=refresh)
                else:
                    return json_reply({"error": f"unknown view {view!r}"}, 400)
                return json_reply({"csrf": user["csrf"], **data})
            action = str(body.get("action") or "")
            module = str(body.get("module") or "").strip()
            if action in ("toggle", "toggle_all", "reload", "default") and not module:
                return json_reply({"error": "module is required"}, 400)
            if action == "toggle":
                target = str(body.get("target") or "hub")
                enable = bool(body.get("enable"))
                if target in ("hub", "rook"):
                    res = await admin.toggle_hub(module, enable, actor)
                else:
                    res = await admin.toggle_worker(target, module, enable, actor)
            elif action == "toggle_all":
                workers = body.get("workers")
                res = await admin.toggle_all(module, bool(body.get("enable")), actor,
                                             [str(w) for w in workers]
                                             if isinstance(workers, list) else None)
            elif action == "reload":
                res = await admin.reload(str(body.get("target") or "hub"), module, actor)
            elif action == "default":
                res = admin.set_default(body.get("band"), module, body.get("value"), actor)
            elif action == "apply_presets":
                presets = body.get("presets")
                if not isinstance(presets, list):
                    return json_reply({"error": "presets must be a list of preset ids"}, 400)
                res = await admin.apply_presets([str(p) for p in presets], body.get("band"),
                                                actor)
            elif action == "marketplace_install":
                return json_reply(admin.install(str(body.get("id") or ""), actor), 501)
            else:
                return json_reply({"error": "Use toggle, toggle_all, reload, default, "
                                            "apply_presets or marketplace_install"}, 400)
            return json_reply(res)
        except LookupError as e:
            return json_reply({"error": str(e).strip("'\"")}, 404)
        except (ValueError, TypeError, PermissionError) as e:
            return json_reply({"error": str(e)}, 400)
        except Exception:
            log.exception("plugins account API failed")
            return json_reply({"error": "The plugins request failed; see the MCP log."}, 500)

    return [Route(PATH, api, methods=["GET", "POST"])]
