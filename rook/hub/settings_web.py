"""Account API behind the dashboard's Settings area: ``/settings/account-api``.

Same session and CSRF rules as the other account APIs (vault, guidance); the
dashboard proxies to it. The operator account sees and changes everything;
any other signed-in account sees and changes only its own preferences (user
scope). Secret values are write-only here: this API never returns one.
"""

from __future__ import annotations

import hmac
import logging

from starlette.responses import JSONResponse
from starlette.routing import Route

from ..remote.account_web import NO_STORE
from .settings_service import SettingsError

log = logging.getLogger("rook.hub.settings_web")


def _json(data, status=200):
    return JSONResponse(data, status, headers=NO_STORE)


def _persona(svc):
    """The persona hub plugin behind the Settings > Persona page."""
    node = getattr(svc, "node", None)
    plugin = node.plugin("persona") if node is not None else None
    if plugin is None:
        raise SettingsError("The persona plugin is not loaded on this hub.")
    return plugin


def _home(svc):
    """The home agent hub plugin behind the Manage > Home agent page."""
    node = getattr(svc, "node", None)
    plugin = node.plugin("home") if node is not None else None
    if plugin is None:
        raise SettingsError("The home agent plugin is not loaded on this hub.")
    return plugin


def routes(get_service, accounts):
    """``get_service()`` returns the live SettingsService (or None)."""

    async def api(request):
        user = accounts.session(request.cookies.get("rook_account", ""))
        if not user:
            return _json({"error": "Sign in to change settings."}, 401)
        svc = get_service()
        if svc is None:
            return _json({"error": "Settings are unavailable: hub plugins are off "
                                   "(ROOK_HUB_PLUGINS=0) or failed to start."}, 503)
        admin = bool(user.get("admin"))
        uid = str(user["id"])
        label = str(user.get("name") or user.get("username") or uid)
        actor = "human:" + str(user.get("username") or uid)
        try:
            if request.method == "GET":
                q = request.query_params
                view = q.get("view") or ("overview" if admin else "user")
                target = q.get("target") or ""
                if not admin and view != "user":
                    return _json({"error": "Only the operator account can open this page; "
                                           "My preferences is yours.", "csrf": user["csrf"]}, 403)
                if view == "overview":
                    data = svc.overview()
                elif view == "hub":
                    data = svc.hub_page()
                elif view == "band":
                    data = svc.band_page(target)
                elif view == "worker":
                    data = await svc.worker_page(target)
                elif view == "plugin":
                    data = svc.plugin_page(target)
                elif view == "user":
                    data = svc.user_page(uid, label)
                elif view == "persona":
                    data = _persona(svc).page()
                elif view == "home":
                    data = _home(svc).page(svc)
                elif view == "search":
                    data = {"results": svc.search(q.get("q", ""))}
                elif view == "history":
                    data = {"history": svc.store.history(key=q.get("key") or None,
                                                         scope=q.get("scope") or None,
                                                         target=q.get("target") or None,
                                                         limit=int(q.get("limit") or 50))}
                else:
                    return _json({"error": f"unknown view {view!r}"}, 400)
                return _json({"csrf": user["csrf"], "admin": admin, "user": uid, **data})

            data = await request.json()
            if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("csrf", "")),
                                                                     user["csrf"]):
                return _json({"error": "Form expired; reload the page."}, 403)
            action = data.get("action")
            scope = data.get("scope") or None
            target = data.get("target") or ""
            if not admin:
                # Your own preferences only.
                if action not in ("set", "reset", "dry_run") or scope != "user":
                    return _json({"error": "Only the operator account can change this."}, 403)
                target = uid
            elif scope == "user" and not target:
                target = uid
            if action in ("set", "dry_run"):
                res = svc.set(str(data.get("key", "")), data.get("value"), scope=scope,
                              target=target, actor=actor, note=str(data.get("note") or ""),
                              source="ui", dry_run=action == "dry_run",
                              expect_rev=data.get("rev"))
            elif action == "reset":
                res = svc.reset(str(data.get("key", "")), scope=scope, target=target,
                                actor=actor, note=str(data.get("note") or ""), source="ui")
            elif action == "apply_worker":
                res = await svc.apply_worker(str(data.get("worker", "")), actor)
            elif action == "plugin":
                res = await svc.plugin_toggle(str(data.get("worker", "")),
                                              str(data.get("module", "")),
                                              bool(data.get("enable")), actor)
            elif action in ("persona_save", "persona_assign", "persona_delete"):
                res = _persona(svc).page_action(data, actor)
            elif action in ("home_save", "home_models", "home_test"):
                res = await _home(svc).page_action(svc, data, actor)
            else:
                return _json({"error": "Use set, dry_run, reset, apply_worker, plugin or "
                                       "persona_* or home_*"}, 400)
            return _json(res)
        except PermissionError as e:
            return _json({"error": str(e)}, 403)
        except SettingsError as e:
            return _json({"error": str(e)}, 400)
        except (ValueError, TypeError) as e:
            return _json({"error": str(e)}, 400)
        except Exception:
            log.exception("settings account API failed")
            return _json({"error": "Settings request failed; see the MCP log."}, 500)

    return [Route("/settings/account-api", api, methods=["GET", "POST"])]
