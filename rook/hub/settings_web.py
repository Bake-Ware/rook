"""Account API behind the dashboard's Settings area: ``/settings/account-api``.

Same session and CSRF rules as the other account APIs (vault, guidance); the
dashboard proxies to it. The operator account sees and changes everything;
any other signed-in account sees and changes only its own preferences (user
scope). Secret values are write-only here: this API never returns one.
"""

from __future__ import annotations

import logging

from starlette.routing import Route

from ..core import restart
from ..remote.account_api import account_api, json_reply
from .settings_service import SettingsError

log = logging.getLogger("rook.hub.settings_web")


_json = json_reply


def _restart(actor: str) -> dict:
    """Settings > Restart (operator): this process stops and its service
    manager starts it again (rook/core/restart.py)."""
    if not restart.available():
        raise SettingsError("This hub can't restart itself from here: restart its service.")
    why = restart.supervised()
    if why:
        raise SettingsError(f"Not restarted: {why}.")
    restart.request(actor)
    return {"ok": True, "note": "Restarting. The page reconnects when the hub is back."}


def _persona(svc):
    """The persona hub plugin behind the Settings > Persona page."""
    node = getattr(svc, "node", None)
    plugin = node.plugin("persona") if node is not None else None
    if plugin is None:
        raise SettingsError("The persona plugin is not loaded on this hub.")
    return plugin


def _home(svc):
    """The home agent hub plugin behind the Intelligence sources > Home agent page."""
    node = getattr(svc, "node", None)
    plugin = node.plugin("home") if node is not None else None
    if plugin is None:
        raise SettingsError("The home agent plugin is not loaded on this hub.")
    return plugin


def _voice(svc):
    """The voice hub plugin behind Intelligence sources > Voice, or None when it is not
    loaded (the page then says why)."""
    node = getattr(svc, "node", None)
    return node, (node.plugin("voice") if node is not None else None)


def _llm(svc):
    """The model providers hub plugin behind Intelligence sources > Model providers."""
    node = getattr(svc, "node", None)
    plugin = node.plugin("llm") if node is not None else None
    if plugin is None:
        raise SettingsError("The model providers plugin is not loaded on this hub.")
    return plugin


def _memory(svc):
    """The memory hub plugin behind the Work > Memory page."""
    node = getattr(svc, "node", None)
    plugin = node.plugin("memory") if node is not None else None
    if plugin is None:
        raise SettingsError("Memory is off on this hub. Turn it on under Settings > Plugins "
                            "(hub plugin memory) or Settings > Memory.")
    return plugin


def routes(get_service, accounts):
    """``get_service()`` returns the live SettingsService (or None)."""

    # Any signed-in account; non-operators are limited to user scope below.
    @account_api(accounts, admin=False, sign_in="Sign in to change settings.")
    async def api(request, user, body):
        svc = get_service()
        if svc is None:
            return _json({"error": "Settings are unavailable: hub plugins are off "
                                   "(ROOK_HUB_PLUGINS=0) or failed to start."}, 503)
        admin = bool(user.get("admin"))
        uid = str(user["id"])
        label = str(user.get("name") or user.get("username") or uid)
        actor = "human:" + str(user.get("username") or uid)
        try:
            if body is None:
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
                elif view == "voice":
                    from .plugins.voice import page_data
                    node, plugin = _voice(svc)
                    data = page_data(svc, node, plugin)
                elif view == "llm":
                    data = await _llm(svc).page(svc)
                elif view == "memory":
                    data = _memory(svc).page()
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

            data = body
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
            elif action in ("voice_check", "voice_transcribe"):
                node, plugin = _voice(svc)
                if plugin is None:
                    st = (getattr(getattr(node, "host", None), "status", {}) or {}).get("voice") or {}
                    raise SettingsError("Voice is not running on this hub. "
                                        + str(st.get("reason") or ""))
                res = await plugin.page_action(svc, data, actor)
            elif action in ("llm_save", "llm_delete", "llm_check", "llm_models"):
                res = await _llm(svc).page_action(svc, data, actor)
            elif str(action or "").startswith("memory_"):
                res = await _memory(svc).page_action(data, actor)
            elif action == "restart":
                res = _restart(actor)
            else:
                return _json({"error": "Use set, dry_run, reset, apply_worker, plugin, restart or "
                                       "persona_*, home_*, memory_*, voice_* or llm_*"}, 400)
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
