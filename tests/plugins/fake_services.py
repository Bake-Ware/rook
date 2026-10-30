"""Fake HTTP services for the hub service plugins, on 127.0.0.1 in the test's
event loop (aiohttp): the Telegram Bot API, Discord's REST API and Gateway,
an OpenAI-compatible endpoint and the Claude API. Each records what it got
in ``.requests`` ({method, path, query, headers (lower case), body}); tests feed inbound traffic
with ``push``."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from aiohttp import web


@asynccontextmanager
async def serve(app: web.Application):
    """Run ``app`` on a free port; yields its base URL."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await asyncio.wait_for(runner.cleanup(), 10)


class Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.app = web.Application()

    async def record(self, request: web.Request) -> dict:
        raw = await request.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        rec = {"method": request.method, "path": request.path, "query": dict(request.query),
               "headers": {k.lower(): v for k, v in request.headers.items()}, "body": body}
        self.requests.append(rec)
        return rec

    def sent(self, path_end: str) -> list[dict]:
        return [r for r in self.requests if r["path"].endswith(path_end)]

    async def wait(self, cond, timeout: float = 5.0):
        for _ in range(int(timeout / 0.02)):
            got = cond()
            if got:
                return got
            await asyncio.sleep(0.02)
        raise AssertionError("the fake didn't see what the test waited for")


class FakeTelegram(Fake):
    """getMe, getUpdates (long-polled: waits for ``push``), sendMessage."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token
        self.updates: list[dict] = []
        self._new = asyncio.Event()
        self._next = 1
        self.app.router.add_post("/bot{token}/{method}", self.handle)

    def push(self, message: dict) -> None:
        self.updates.append({"update_id": self._next, "message": message})
        self._next += 1
        self._new.set()

    async def handle(self, request: web.Request) -> web.Response:
        rec = await self.record(request)
        if request.match_info["token"] != self.token:
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        method, body = request.match_info["method"], rec["body"] or {}
        if method == "getMe":
            return web.json_response({"ok": True, "result": {"id": 999, "is_bot": True, "username": "rook_bot"}})
        if method == "getUpdates":
            offset = int(body.get("offset") or 0)
            pending = [u for u in self.updates if u["update_id"] >= offset]
            if not pending:
                self._new.clear()
                try:
                    await asyncio.wait_for(self._new.wait(), min(float(body.get("timeout") or 0), 1.0))
                except asyncio.TimeoutError:
                    pass
                pending = [u for u in self.updates if u["update_id"] >= offset]
            return web.json_response({"ok": True, "result": pending})
        if method == "sendMessage":
            if str(body.get("chat_id")) == "404":
                return web.json_response({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
                                         status=400)
            return web.json_response({"ok": True, "result": {"message_id": 77, "chat": {"id": body.get("chat_id")}}})
        return web.json_response({"ok": False, "error_code": 404, "description": "Not Found"}, status=404)


class FakeDiscord(Fake):
    """REST under /api (gateway/bot, users/@me/channels, channels/{id}/messages)
    and a Gateway websocket at /gw that sends hello, READY after identify, and
    whatever the test ``push``es as MESSAGE_CREATE."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token
        self.base = ""
        self.identified: list[dict] = []
        self.heartbeats = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        r = self.app.router
        r.add_get("/api/gateway/bot", self.gateway_bot)
        r.add_post("/api/users/@me/channels", self.dm_channel)
        r.add_post("/api/channels/{channel}/messages", self.message)
        r.add_get("/gw/", self.gateway)

    def push(self, message: dict) -> None:
        self._queue.put_nowait(message)

    def _authorized(self, request) -> bool:
        return request.headers.get("Authorization") == f"Bot {self.token}"

    async def gateway_bot(self, request):
        await self.record(request)
        if not self._authorized(request):
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        return web.json_response({"url": self.base.replace("http://", "ws://") + "/gw"})

    async def dm_channel(self, request):
        rec = await self.record(request)
        return web.json_response({"id": "5" + str(rec["body"]["recipient_id"]), "type": 1})

    async def message(self, request):
        rec = await self.record(request)
        if not self._authorized(request):
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        return web.json_response({"id": "8001", "channel_id": request.match_info["channel"],
                                  "content": rec["body"]["content"]})

    async def gateway(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 100}})
        seq = 0

        async def reader():
            nonlocal seq
            async for m in ws:
                ev = json.loads(m.data)
                if ev["op"] == 2:
                    self.identified.append(ev["d"])
                    seq += 1
                    await ws.send_json({"op": 0, "t": "READY", "s": seq,
                                        "d": {"user": {"id": "999", "username": "rook-bot"}}})
                elif ev["op"] == 1:
                    self.heartbeats += 1
                    await ws.send_json({"op": 11})

        task = asyncio.ensure_future(reader())
        try:
            while not ws.closed:
                getter = asyncio.ensure_future(self._queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    break
                seq += 1
                await ws.send_json({"op": 0, "t": "MESSAGE_CREATE", "s": seq, "d": getter.result()})
        finally:
            task.cancel()
        return ws


class FakeOpenAI(Fake):
    """POST /v1/chat/completions (echoes the last message), GET /v1/models."""

    def __init__(self, key: str | None = None):
        super().__init__()
        self.key = key
        self.app.router.add_post("/v1/chat/completions", self.complete)
        self.app.router.add_get("/v1/models", self.models)

    def _denied(self, request):
        if self.key and request.headers.get("Authorization") != f"Bearer {self.key}":
            return web.json_response({"error": {"message": "bad key"}}, status=401)
        return None

    async def complete(self, request):
        rec = await self.record(request)
        denied = self._denied(request)
        if denied:
            return denied
        body = rec["body"]
        if body.get("model") == "busy":
            return web.json_response({"error": {"message": "overloaded"}}, status=503)
        last = body["messages"][-1]["content"]
        return web.json_response({"model": body.get("model", "default-model"),
                                  "choices": [{"message": {"role": "assistant", "content": "echo: " + last},
                                               "finish_reason": "length" if body.get("max_tokens") == 1 else "stop"}],
                                  "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    async def models(self, request):
        await self.record(request)
        return self._denied(request) or web.json_response({"data": [{"id": "qwen"}, {"id": "llama"}]})


class FakeClaude(Fake):
    """POST /v1/messages, GET /v1/models, keyed by x-api-key."""

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.app.router.add_post("/v1/messages", self.messages)
        self.app.router.add_get("/v1/models", self.models)

    def _denied(self, request):
        if request.headers.get("x-api-key") != self.key:
            return web.json_response({"type": "error", "error": {"type": "authentication_error",
                                                                 "message": "invalid x-api-key"}}, status=401)
        return None

    async def messages(self, request):
        rec = await self.record(request)
        denied = self._denied(request)
        if denied:
            return denied
        body = rec["body"]
        if "temperature" in body:
            return web.json_response({"type": "error", "error": {"type": "invalid_request_error",
                                                                 "message": "temperature: not supported"}}, status=400)
        if body["model"] == "overloaded":
            return web.json_response({"type": "error", "error": {"type": "overloaded_error",
                                                                 "message": "Overloaded"}}, status=529)
        last = body["messages"][-1]["content"]
        return web.json_response({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                  "content": [{"type": "thinking", "thinking": ""},
                                              {"type": "text", "text": "claude: " + last}],
                                  "stop_reason": "end_turn", "usage": {"input_tokens": 11, "output_tokens": 4}})

    async def models(self, request):
        await self.record(request)
        return self._denied(request) or web.json_response(
            {"data": [{"id": "claude-opus-5-5"}, {"id": "claude-haiku-5-5"}], "has_more": False})
