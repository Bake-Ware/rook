"""POST /internal/band-call: the dashboard's cap buttons, run by the MCP side.

The dashboard's cap panel and the ``rook band`` terminal send their calls to
the dashboard's ``/api/band/call`` (rook/remote/bootstrap.py), which forwards
them here instead of putting them on the band itself. That way a person's
call takes the same path as an agent's ``rook_call``: the hub's own caps run
in-process on the hub node (a band call to them is refused above ``read``,
since any band key holder could send one), policy is checked for the person,
vault values in the reply are masked, and the call is journaled. Hub caps
tagged ``sensitive`` follow ``rook_call``'s rule: the caller gets the reply
unmasked and the journal gets nothing.

Gated by the internal token in ``mask.token`` (the one ``/internal/mask``
uses). The dashboard forwards the person it admitted in ``X-Rook-Principal``
(JSON id/role/label); only a ``human:*`` principal is accepted, so this route
can't be used to act as an agent or as the hub. The target is the worker id
the dashboard already resolved. ``{{secret:name}}`` placeholders are not
filled in here: the dashboard never could, and a person who needs a secret
value reads it in the vault.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Callable

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import secret_mask

log = logging.getLogger("rook.band_mcp.dashboard_call")

PATH = "/internal/band-call"
MAX_BODY = 1_000_000
MAX_TIMEOUT = 3600.0   # rook_call's ceiling
NO_STORE = {"Cache-Control": "no-store"}


def _reply(obj: Any, status: int = 200) -> Response:
    """JSON that can't fail after the call has run: NaN/Infinity pass (as the
    dashboard's own json_response allowed) and odd types become strings."""
    body = json.dumps(obj, default=str, allow_nan=True)
    return Response(body, status_code=status, media_type="application/json", headers=NO_STORE)


def _sensitive(hub: Any, target: Any, cap: str) -> bool:
    try:
        return bool(hub is not None and target == hub.worker_id
                    and "sensitive" in (getattr(hub.host.registry.meta(cap), "tags", ()) or ()))
    except Exception:  # noqa: BLE001 - unknown means treat as ordinary (masked)
        return False


def route(token: str, get_client: Callable[[], Any], get_journal: Callable[[], Any],
          get_hub: Callable[[], Any] = lambda: None) -> Route:
    from ..hub import authz as hub_authz
    from ..hub.plugins.jobs.web import internal_principal

    async def band_call(request):
        got = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            return JSONResponse({"error": "unauthorized"}, 401)
        principal = internal_principal(request.headers.get("x-rook-principal", ""))
        if principal is None:
            return JSONResponse({"error": "a signed-in person is required"}, 403)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, 413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or not data.get("cap"):
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "expected {\"cap\": ..., \"args\": {...}}"}, 400)
        client = get_client()
        if client is None:
            return JSONResponse({"error": "band client not connected"}, 503)
        cap = str(data["cap"])
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        target = data.get("target") or None
        try:
            timeout = min(max(float(data.get("timeout", 15.0)), 1.0), MAX_TIMEOUT)
        except (TypeError, ValueError):
            timeout = 15.0
        identity = str(data.get("identity") or principal.label or principal.id)[:200]
        entry = client.workers.get(target) if target else None
        worker_name = (entry or {}).get("name") or target or "band"
        journal = get_journal()
        ptok = hub_authz.current_principal.set(principal)
        # This route journals the call itself, so the authorizer needn't; with
        # no journal, let the authorizer keep its own record of decisions.
        jtok = hub_authz.caller_journals.set(journal is not None)
        hub_authz.last_decision.set(None)
        try:
            try:
                reply = await client.call(cap=cap, args=args, target=target, timeout=timeout,
                                          identity=identity, principal=principal)
                status = 200
            except asyncio.TimeoutError:
                reply, status = {"ok": False, "timeout": True,
                                 "error": "timeout waiting for reply"}, 504
            except Exception as e:  # noqa: BLE001 - reported to the person, like before
                reply, status = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500
            decision = hub_authz.last_decision.get()
        finally:
            hub_authz.caller_journals.reset(jtok)
            hub_authz.current_principal.reset(ptok)
        sensitive = isinstance(reply, dict) and _sensitive(get_hub(), target, cap)
        journal_reply = reply
        if sensitive:
            journal_reply = ({"ok": reply.get("ok"), "result": "[sensitive: not journaled]"}
                             if reply.get("ok") else secret_mask.scrub(reply))
        else:
            masker = secret_mask.installed()
            if masker is not None and isinstance(reply, dict):
                try:
                    reply = journal_reply = masker.mask(reply)
                except Exception:  # noqa: BLE001 - never log the payload; never show it
                    log.exception("masking a dashboard call reply failed")
                    reply = journal_reply = {
                        "ok": False, "error": "the reply was withheld: masking vault values "
                                              "in it failed (the call did run)"}
        if journal is not None:
            try:
                reply_id = journal.record(cap=cap, worker=worker_name, identity=identity,
                                          args=args, reply=journal_reply,
                                          authz=decision.journal() if decision is not None else None)
                if isinstance(reply, dict) and reply_id:
                    reply = {**reply, "journal_id": reply_id}
            except Exception:  # noqa: BLE001
                log.exception("journaling a dashboard call failed")
        return _reply(reply, status)

    return Route(PATH, band_call, methods=["POST"])
