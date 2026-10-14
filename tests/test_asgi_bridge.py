"""rook.hub.asgi_bridge: an ASGI app served from aiohttp in one process
(HTTP, streaming, SSE disconnects, WebSockets, lifespan, in-memory calls)."""
import asyncio
import contextvars
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from contextlib import asynccontextmanager
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from rook.hub.asgi_bridge import AsgiBridge, Lifespan, LifespanError, call, cookie_header

LEAK = contextvars.ContextVar("leak", default="clean")


def make_app(events=None):
    events = events if events is not None else []

    @asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield {"greeting": "hello"}
        events.append("shutdown")

    async def info(request):
        return JSONResponse({
            "method": request.method, "path": request.url.path,
            "query": request.url.query, "scheme": request.url.scheme,
            "client": request.client.host if request.client else None,
            "cookie": request.cookies.get("rook_account"),
            "state": getattr(request.state, "greeting", None),
            "leak": LEAK.get(),
            "host": request.headers.get("host"),
        }, headers={"X-Multi": "a"})

    async def echo(request):
        body = await request.body()
        return Response(body, media_type="application/octet-stream",
                        headers={"X-Length": str(len(body))})

    async def cookies(request):
        r = PlainTextResponse("ok")
        r.set_cookie("one", "1")
        r.set_cookie("two", "2")
        return r

    gate = asyncio.Event()

    async def stream(request):
        async def gen():
            yield b"first\n"
            await gate.wait()           # released by the test after it read "first"
            yield b"second\n"
        return StreamingResponse(gen(), media_type="text/plain")

    async def release(request):
        gate.set()
        return PlainTextResponse("released")

    async def sse(request):
        async def gen():
            try:
                n = 0
                while True:
                    n += 1
                    yield {"data": f"tick {n}"}
                    await asyncio.sleep(0.05)
            finally:
                events.append("sse-closed")
        return EventSourceResponse(gen(), ping=60)

    async def boom(request):
        raise ValueError("boom")

    async def ws_echo(websocket):
        await websocket.accept(subprotocol="rook" if "rook" in websocket.scope["subprotocols"] else None)
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    events.append(("ws-disconnect", msg.get("code")))
                    return
                if msg.get("bytes") is not None:
                    await websocket.send_bytes(msg["bytes"][::-1])
                elif msg.get("text") == "close":
                    await websocket.close(code=4001, reason="asked")
                    return
                else:
                    await websocket.send_text((msg.get("text") or "").upper())
        except WebSocketDisconnect:
            events.append("ws-disconnect")

    async def ws_refuse(websocket):
        await websocket.close(code=1008)

    return Starlette(routes=[
        Route("/info", info, methods=["GET", "POST", "HEAD"]),
        Route("/echo", echo, methods=["POST"]),
        Route("/cookies", cookies),
        Route("/stream", stream),
        Route("/release", release),
        Route("/sse", sse),
        Route("/boom", boom),
        WebSocketRoute("/ws", ws_echo),
        WebSocketRoute("/ws-refuse", ws_refuse),
    ], lifespan=lifespan)


@asynccontextmanager
async def served(events=None):
    events = events if events is not None else []
    asgi = make_app(events)
    life = Lifespan(asgi)
    await life.start()
    bridge = AsgiBridge(asgi, lifespan=life, disconnect_poll=0.05)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", bridge.handle)
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, events
    finally:
        await life.stop()


@pytest.mark.asyncio
async def test_http_scope_headers_and_lifespan_state():
    async with served() as (client, events):
        r = await client.get("/info?a=1&b=%20x", cookies={"rook_account": "tok"})
        assert r.status == 200 and r.headers["X-Multi"] == "a"
        body = await r.json()
        assert body["path"] == "/info" and body["query"] == "a=1&b=%20x"
        assert body["cookie"] == "tok" and body["state"] == "hello"
        assert body["scheme"] == "http" and body["client"] == "127.0.0.1"
        assert events == ["startup"]
        r = await client.head("/info")
        assert r.status == 200 and await r.read() == b""
        r = await client.get("/cookies")
        assert sorted(c.key for c in r.cookies.values()) == ["one", "two"]
    assert events == ["startup", "shutdown"]


@pytest.mark.asyncio
async def test_request_body_streams_in():
    async with served() as (client, _):
        async def chunks():
            for i in range(50):
                yield (b"%03d" % i) * 1000
        r = await client.post("/echo", data=chunks())
        data = await r.read()
        assert r.status == 200 and len(data) == 150_000 and r.headers["X-Length"] == "150000"
        assert data[:3] == b"000" and data[-3:] == b"049"


@pytest.mark.asyncio
async def test_forwarded_headers_only_from_a_trusted_proxy():
    async with served() as (client, _):
        r = await client.get("/info", headers={"X-Forwarded-For": "198.51.100.7, 127.0.0.1",
                                               "X-Forwarded-Proto": "https"})
        body = await r.json()
        # The test client connects from loopback: a trusted proxy.
        assert body["client"] == "198.51.100.7" and body["scheme"] == "https"
    asgi = make_app()
    bridge = AsgiBridge(asgi, trusted_proxies=())
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", bridge.handle)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/info", headers={"X-Forwarded-For": "198.51.100.7",
                                               "X-Forwarded-Proto": "https"})
        body = await r.json()
        assert body["client"] == "127.0.0.1" and body["scheme"] == "http"


@pytest.mark.asyncio
async def test_streamed_response_is_flushed_chunk_by_chunk():
    async with served() as (client, _):
        r = await client.get("/stream")
        assert r.status == 200
        # "first" arrives while the app is still blocked before "second".
        line = await asyncio.wait_for(r.content.readline(), 5)
        assert line == b"first\n"
        assert (await client.get("/release")).status == 200
        assert await asyncio.wait_for(r.content.readline(), 5) == b"second\n"
        r.release()


@pytest.mark.asyncio
async def test_sse_stream_sees_the_client_disconnect():
    async with served() as (client, events):
        r = await client.get("/sse")
        assert r.headers["Content-Type"].startswith("text/event-stream")
        seen = b""
        while b"tick 2" not in seen:
            seen += await asyncio.wait_for(r.content.readany(), 5)
        r.close()   # drop the connection mid-stream
        for _ in range(100):
            if "sse-closed" in events:
                break
            await asyncio.sleep(0.05)
        assert "sse-closed" in events


@pytest.mark.asyncio
async def test_app_error_before_response_is_a_500():
    async with served() as (client, _):
        r = await client.get("/boom")
        assert r.status == 500


@pytest.mark.asyncio
async def test_websocket_text_bytes_subprotocol_and_close():
    async with served() as (client, events):
        async with client.ws_connect("/ws", protocols=("rook",)) as ws:
            assert ws.protocol == "rook"
            await ws.send_str("hi")
            assert (await ws.receive_str(timeout=5)) == "HI"
            await ws.send_bytes(b"abc")
            assert (await ws.receive_bytes(timeout=5)) == b"cba"
            await ws.send_str("close")
            msg = await ws.receive(timeout=5)
            assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4001
        async with client.ws_connect("/ws") as ws:
            await ws.send_bytes(b"x")
            assert (await ws.receive_bytes(timeout=5)) == b"x"
        for _ in range(100):
            if any(e == "ws-disconnect" or (isinstance(e, tuple) and e[0] == "ws-disconnect")
                   for e in events):
                break
            await asyncio.sleep(0.05)
        assert any(e == "ws-disconnect" or (isinstance(e, tuple) and e[0] == "ws-disconnect")
                   for e in events)


@pytest.mark.asyncio
async def test_websocket_closed_before_accept_is_403():
    async with served() as (client, _):
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await client.ws_connect("/ws-refuse")
        assert err.value.status == 403


@pytest.mark.asyncio
async def test_lifespan_failure_and_apps_without_lifespan():
    @asynccontextmanager
    async def broken(app):
        raise RuntimeError("no database")
        yield
    life = Lifespan(Starlette(lifespan=broken))
    with pytest.raises(LifespanError):
        await life.start()

    async def bare(scope, receive, send):   # rejects every scope type but http
        if scope["type"] != "http":
            raise ValueError("http only")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    life = Lifespan(bare)
    await life.start()
    assert life.supported is False
    reply = await call(bare, "GET", "/")
    assert reply.status == 204
    await life.stop()


@pytest.mark.asyncio
async def test_in_memory_call_matches_a_loopback_request():
    events = []
    asgi = make_app(events)
    life = Lifespan(asgi)
    await life.start()
    token = LEAK.set("dashboard principal")
    try:
        reply = await call(asgi, "POST", "/info?x=1", host="127.0.0.1", port=8765,
                           headers=[("Cookie", cookie_header({"rook_account": "abc"}))],
                           body=b"{}", state=life.state)
    finally:
        LEAK.reset(token)
    body = json.loads(reply.body)
    assert reply.status == 200 and reply.content_type == "application/json"
    assert body["method"] == "POST" and body["query"] == "x=1" and body["cookie"] == "abc"
    assert body["host"] == "127.0.0.1:8765" and body["client"] == "127.0.0.1"
    assert body["state"] == "hello"
    assert body["leak"] == "clean"   # no context variable of the caller reaches the app
    reply = await call(asgi, "POST", "/echo", body=b"payload")
    assert reply.body == b"payload"
    with pytest.raises(asyncio.TimeoutError):
        await call(asgi, "GET", "/stream", timeout=0.2)   # blocks on the gate
    await life.stop()
    assert events == ["startup", "shutdown"]
