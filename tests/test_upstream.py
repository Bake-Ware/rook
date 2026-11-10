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
