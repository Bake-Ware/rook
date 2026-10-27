"""Account API behind Settings > Updates: ``/updates/account-api``.

Operator account only, same session and CSRF rules as the other account
APIs; the dashboard proxies ``/account/updates/api`` here
(rook/remote/knowledge_web.py UpdatesWeb).

GET: the overview (read-only; ``check`` below asks GitHub and the dongles).
POST ``{"action": ...}``: ``check``, ``settings`` (``mode``, ``channel``,
``window``), ``acknowledge`` (``tag``), ``apply_hub`` (``tag``),
``prepare_hub`` (``tag``: the signed release and the command to deploy it,
for a ``rook hub`` install the hub cannot switch itself), ``rollback_hub``, ``rollout`` (``selection``, ``build?``, ``canaries?``),
``cancel``, ``release`` (``build``), ``publish_apk`` (``tag``, ``notify``),
``notify_apps`` (``ids?``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_reply

log = logging.getLogger("rook.hub.plugins.updates.web")

PATH = "/updates/account-api"
UNAVAILABLE = ("Updates are unavailable: the updates plugin is off or hub plugins failed "
               "to start.")


def _plugin(node: Any):
    if node is None:
        return None
    for p in node.host.plugins:
        if getattr(p, "NAMESPACE", "") == "updates" and getattr(p, "service", None) is not None:
            return p
    return None


def routes(get_node: Callable[[], Any], accounts):
    from .service import Refused

    @account_api(accounts, sign_in="Sign in to manage updates.",
                 operator_only="Only an owner can manage updates.")
    async def api(request, user, body):
        node = get_node()
        plugin = _plugin(node)
        if plugin is None:
            return json_reply({"error": UNAVAILABLE, "csrf": user["csrf"]}, 503)
        svc = plugin.service
        actor = "human:" + str(user.get("username") or user["id"])
        try:
            if body is None:
                # Read-only: checking GitHub or the dongles is the POST
                # "check" action (CSRF-checked), never a GET.
                return json_reply({"csrf": user["csrf"], **svc.overview()})
            action = str(body.get("action") or "")
            if action == "check":
                res = await svc.check(force=True)
                await svc.refresh_firmware()
            elif action == "settings":
                res = _settings(node, body, actor)
            elif action == "acknowledge":
                res = svc.acknowledge(str(body.get("tag") or ""), actor)
            elif action == "apply_hub":
                res = await svc.apply_hub(str(body.get("tag") or ""), actor)
            elif action == "prepare_hub":
                res = await svc.prepare_hub(str(body.get("tag") or ""), actor)
            elif action == "rollback_hub":
                res = await svc.rollback_hub(actor)
            elif action == "rollout":
                sel = body.get("selection") if isinstance(body.get("selection"), dict) else {}
                can = body.get("canaries")
                res = await svc.start_rollout(sel, actor, build=int(body.get("build") or 0) or None,
                                              canaries=[str(c) for c in can]
                                              if isinstance(can, list) else None)
            elif action == "cancel":
                res = svc.cancel_rollout(actor)
            elif action == "release":
                res = svc.release_build(int(body.get("build") or 0), actor)
            elif action == "publish_apk":
                res = await svc.publish_apk(str(body.get("tag") or ""), actor,
                                            notify=bool(body.get("notify")))
            elif action == "notify_apps":
                ids = body.get("ids")
                res = await svc.notify_apps(actor, [str(i) for i in ids]
                                            if isinstance(ids, list) else None)
            else:
                return json_reply({"error": "Unknown action."}, 400)
            return json_reply({"ok": True, "result": res})
        except Refused as e:
            return json_reply({"error": str(e), **e.data}, 409)
        except (ValueError, TypeError, LookupError) as e:
            return json_reply({"error": str(e)}, 400)
        except Exception as e:
            log.exception("updates account API failed")
            return json_reply({"error": f"The update request failed: {e}"}, 500)

    return [Route(PATH, api, methods=["GET", "POST"])]


def _settings(node: Any, body: dict, actor: str) -> dict:
    """Mode, channel and window from the wizard, through the settings
    service (so they show, with history, on Settings too)."""
    settings = getattr(node, "settings", None)
    if settings is None:
        raise ValueError("the settings service is unavailable")
    out = {}
    for name in ("mode", "channel", "window"):
        if name in body:
            out[name] = settings.set(f"updates.{name}", body[name], actor=actor,
                                     note="Settings > Updates", source="dashboard")["ok"]
    if not out:
        raise ValueError("nothing to change: send mode, channel or window")
    return out
