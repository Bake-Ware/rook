"""Account API behind the dashboard's Jobs page: ``/jobs/account-api``.

The dashboard proxies ``/account/jobs/api`` here with the account cookie
(same session and CSRF rules as Knowledge, Vault and Settings). Any signed-in
account may use it: the operator account acts as an owner, every other
account as a member, and the jobs caps decide what each may do (for example,
``settings`` writes are for owners).

The ``rook band`` terminal panel reaches the same API through the dashboard's
``/api/band/jobs`` (rook/remote/jobs_web.py): the dashboard has already
admitted that login, so it forwards the caller's principal with the bridge's
internal token (``mask.token`` beside the stores, readable only by the hub's
own user) instead of a cookie.

Every call runs ``job.read`` / ``job.write`` in process through the hub node,
as the caller's principal, so attribution and the caps' own checks match the
MCP tool. Errors keep the ``rook_jobs`` shape: ``{"ok": false, "error",
"code", "errors"?}``.
"""
from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Callable

from starlette.responses import JSONResponse
from starlette.routing import Route

from ....remote.account_web import NO_STORE
from ...authz import current_principal, principal_for_user
from ...policy import Principal
from .service import READS, WRITES, error_reply, route

log = logging.getLogger("rook.hub.plugins.jobs.web")

PATH = "/jobs/account-api"
SIGN_IN = "Sign in to use Jobs."
UNAVAILABLE = "Jobs are not running on this hub."


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status, headers=NO_STORE)


def _status(error: Exception) -> int:
    name = type(error).__name__
    if name == "NotAvailable":
        return 501
    if isinstance(error, PermissionError):
        return 403
    if isinstance(error, KeyError):
        return 404
    if isinstance(error, LookupError):
        return 503
    if name == "Conflict":
        return 409
    return 400


def internal_principal(header: str) -> Principal | None:
    """The principal the dashboard forwarded for an admitted ``rook band``
    login (``X-Rook-Principal``: JSON id/role/label). Only a dashboard
    human, owner or member, can be forwarded."""
    try:
        d = json.loads(header or "")
    except ValueError:
        return None
    if not isinstance(d, dict) or not str(d.get("id") or "").startswith("human:"):
        return None
    role = "owner" if d.get("role") == "owner" else "member"
    return Principal(str(d["id"]), "human", role, (f"human:{role}",), label=str(d.get("label") or ""))


def routes(get_node: Callable[[], Any], accounts, internal_token: str | None = None):
    """``get_node()`` returns the live hub node (or None)."""

    def who(request) -> tuple[Principal | None, dict | None]:
        user = accounts.session(request.cookies.get("rook_account", ""))
        if user:
            return principal_for_user(user, bool(user.get("admin"))), user
        auth = request.headers.get("authorization", "")
        if internal_token and hmac.compare_digest(auth.encode(), f"Bearer {internal_token}".encode()):
            return internal_principal(request.headers.get("x-rook-principal", "")), None
        return None, None

    async def api(request):
        principal, user = who(request)
        if principal is None:
            return _json({"error": SIGN_IN}, 401)
        node = get_node()
        plugin = node.plugin("job") if node is not None else None
        if plugin is None or getattr(plugin, "service", None) is None:
            return _json({"error": UNAVAILABLE}, 503)
        if request.method == "GET":
            return _json({"csrf": user["csrf"] if user else "", "admin": principal.fail_open(),
                          "principal": principal.id, "label": principal.label,
                          "timezone": plugin.service.tz(), "reads": list(READS), "writes": list(WRITES)})
        try:
            data = await request.json()
        except ValueError:
            return _json({"error": "Expected a JSON object."}, 400)
        if not isinstance(data, dict):
            return _json({"error": "Expected a JSON object."}, 400)
        if user is not None and not hmac.compare_digest(str(data.get("csrf", "")), user["csrf"]):
            return _json({"error": "Form expired; reload the page."}, 403)
        action = str(data.get("action") or "list")
        cap = route(action)
        args: dict = {"action": action, "id": data.get("id"), "data": data.get("data")}
        if cap == "job.read":
            args["query"] = str(data.get("query") or "")
        tok = current_principal.set(principal)
        try:
            result = await node.invoke(cap, args, principal.id)
        except (ValueError, KeyError, TypeError, PermissionError, LookupError) as error:
            return _json(json.loads(error_reply(error)), _status(error))
        except Exception:
            log.exception("jobs account API failed")
            return _json({"ok": False, "error": "The jobs request failed; see the MCP log."}, 500)
        finally:
            current_principal.reset(tok)
        return _json({"ok": True, "result": result})

    return [Route(PATH, api, methods=["GET", "POST"])]
