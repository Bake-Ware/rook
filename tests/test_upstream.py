"""rook.remote.upstream: the dashboard's calls to the MCP side's APIs go
in memory when the MCP app is in this process, over HTTP otherwise."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from rook.remote import upstream


async def api(request):
    body = await request.body()
    return JSONResponse({"method": request.method, "query": dict(request.query_params),
                         "cookie": request.cookies.get("rook_account"),
                         "auth": request.headers.get("authorization"),
                         "body": body.decode() or None,
                         "type": request.headers.get("content-type")}, status_code=201)


async def text(request):
    return PlainTextResponse("not json")


async def boom(request):
    raise RuntimeError("handler bug")

APP = Starlette(routes=[Route("/x/account-api", api, methods=["GET", "POST"]),
                        Route("/text", text), Route("/boom", boom)])


@pytest.mark.asyncio
async def test_registered_origin_is_served_in_memory():
    undo = upstream.register_local(["http://127.0.0.1:8765", "http://localhost:8765"], APP)
    try:
        assert upstream.local_app("http://127.0.0.1:8765/x/account-api") is APP
        assert upstream.local_app("http://LOCALHOST:8765/x") is APP
        assert upstream.local_app("http://127.0.0.1:9999/x") is None
        status, data = await upstream.request_json(
            "POST", "http://127.0.0.1:8765/x/account-api?a=1", params={"b": "2"},
            json={"csrf": "c"}, cookies={"rook_account": "tok"},
            headers={"Authorization": "Bearer t"})
        assert status == 201
        assert data == {"method": "POST", "query": {"a": "1", "b": "2"}, "cookie": "tok",
                        "auth": "Bearer t", "body": '{"csrf": "c"}', "type": "application/json"}
        status, data = await upstream.request_json("GET", "http://localhost:8765/x/account-api")
        assert data["body"] is None and data["type"] is None   # json=None sends no body
        with pytest.raises(upstream.Unavailable):
            await upstream.request_json("GET", "http://127.0.0.1:8765/text")
        with pytest.raises(upstream.Unavailable):
            await upstream.request_json("GET", "http://127.0.0.1:8765/boom")
    finally:
        undo()
    assert upstream.local_app("http://127.0.0.1:8765/x") is None


@pytest.mark.asyncio
async def test_other_urls_go_over_http():
    async def handler(request):
        return web.json_response({"cookie": request.cookies.get("rook_account"),
                                  "body": await request.json()}, status=202)
    app = web.Application()
    app.router.add_post("/x/account-api", handler)
    async with TestServer(app) as ts:
        status, data = await upstream.request_json(
            "POST", str(ts.make_url("/x/account-api")), json={"k": 1},
            cookies={"rook_account": "tok"})
        assert status == 202 and data == {"cookie": "tok", "body": {"k": 1}}
    with pytest.raises(upstream.Unavailable):
        await upstream.request_json("GET", "http://127.0.0.1:9/nothing", timeout=2)
