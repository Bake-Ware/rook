"""Every account JSON API behind the dashboard shares one auth gate
(rook.remote.account_api): signed out 401, non-operator 403 (where operator
only), bad or missing CSRF 403, and a valid request reaches the handler."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from rook.remote.account_api import account_api
from rook.remote.accounts import csrf_ok

USERS = {
    "op": {"id": "u1", "username": "operator", "name": "Operator", "csrf": "tok-op", "admin": True},
    "member": {"id": "u2", "username": "guest", "name": "Guest", "csrf": "tok-m", "admin": False},
}
ACCOUNTS = SimpleNamespace(session=lambda c: USERS.get(c))
OP = {"Cookie": "rook_account=op"}
MEMBER = {"Cookie": "rook_account=member"}


def _guidance():
    from rook.band_mcp.guidance_web import routes
    g = Mock(editable=True, slots=Mock(return_value=[]), history=Mock(return_value=[]))
    return routes(g, Mock(), lambda: ["rook_call"], ACCOUNTS), {"action": "set", "key": "server", "text": "x"}


def _vault():
    from rook.band_mcp.vault_web import routes
    v = Mock(list=Mock(return_value=[]), access_log=Mock(return_value=[]),
             set=Mock(return_value={"name": "n"}))
    return routes(v, lambda n, val: 0, ACCOUNTS), {"action": "set", "name": "n", "value": "v"}


def _tokens():
    from rook.band_mcp.account_tokens import build_account_token_routes
    p = Mock(list_api_tokens=Mock(return_value=[]), revoke_api_token=Mock(return_value=True))
    return build_account_token_routes(p, accounts=ACCOUNTS), {"op": "revoke", "id": "x", "confirm": True}


def _knowledge():
    from rook.hub.plugins.knowledge.web import routes
    s = Mock(bands=Mock(return_value=[]), dispatch=AsyncMock(return_value={}))
    return routes(s, ACCOUNTS), {"action": "list"}


def _settings():
    from rook.hub.settings_web import routes
    svc = Mock(overview=Mock(return_value={}), user_page=Mock(return_value={}),
               set=Mock(return_value={"ok": True}))
    return routes(lambda: svc, ACCOUNTS), {"action": "set", "scope": "user", "key": "k", "value": 1}


def _jobs():
    from rook.hub.plugins.jobs import web
    plugin = SimpleNamespace(service=SimpleNamespace(tz=lambda: "UTC"))
    node = SimpleNamespace(plugin=lambda ns: plugin, invoke=AsyncMock(return_value=[]))
    return web.routes(lambda: node, ACCOUNTS, None), {"action": "list"}


# (factory, operator only)
APIS = {"guidance": (_guidance, True), "vault": (_vault, True), "tokens": (_tokens, True),
        "knowledge": (_knowledge, True), "settings": (_settings, False), "jobs": (_jobs, False)}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(APIS))
async def test_account_api_gate(name):
    factory, operator_only = APIS[name]
    routes, body = factory()
    path = routes[0].path
    app = Starlette(routes=routes)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        # Signed out: JSON 401 on both methods, never HTML.
        for r in (await c.get(path), await c.post(path, json={**body, "csrf": "tok-op"})):
            assert r.status_code == 401 and "error" in r.json()
            assert r.headers["cache-control"] == "no-store"
        # Wrong account.
        r = await c.get(path, headers=MEMBER)
        assert (r.status_code == 403) is operator_only
        # Bad CSRF: missing, wrong, another session's, non-string, non-ASCII.
        for bad in ({}, {"csrf": "nope"}, {"csrf": "tok-m"}, {"csrf": ["tok-op"]}, {"csrf": "tök"}):
            r = await c.post(path, headers=OP, json={**body, **bad})
            assert r.status_code == 403 and r.json()["error"] == "Form expired; reload the page.", bad
        # Not a JSON object.
        assert (await c.post(path, headers=OP, json=["x"])).status_code == 400
        assert (await c.post(path, headers=OP, content=b"{")).status_code == 400
        # OK.
        r = await c.get(path, headers=OP)
        assert r.status_code == 200, r.text
        r = await c.post(path, headers=OP, json={**body, "csrf": "tok-op"})
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_decorator_hands_user_and_body():
    seen = []

    @account_api(ACCOUNTS, admin=False, csrf=False)
    async def handler(request, user, data):
        from starlette.responses import JSONResponse
        seen.append((user["id"], data))
        return JSONResponse({})
    app = Starlette(routes=[Route("/x", handler, methods=["GET", "POST"])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/x", headers=MEMBER)).status_code == 200
        assert (await c.post("/x", headers=MEMBER, json={"a": 1})).status_code == 200
    assert seen == [("u2", None), ("u2", {"a": 1})]


def test_csrf_ok_is_strict():
    assert csrf_ok({"csrf": "a"}, "a")
    assert not csrf_ok({"csrf": "a"}, "")          # no expected token never passes
    assert not csrf_ok({}, "a") and not csrf_ok(None, "a") and not csrf_ok(["a"], "a")
    assert not csrf_ok({"csrf": "é"}, "a")         # non-ASCII fails, does not raise
    assert csrf_ok({"form_token": "f"}, "f", "form_token")
