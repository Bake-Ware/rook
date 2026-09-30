"""Telegram / Discord integration plugins against fake platform servers.

No real network: each test runs an aiohttp server that speaks just enough of
the Telegram Bot API or the Discord REST + Gateway protocol. Covers the room
bridge both ways (attribution, mention mapping, loop prevention), commands
under the integration principal (policy denial of exec, the documented
exec-on-named-workers rule), notifications, rate limiting and secret masking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rook.band_mcp.chat_rooms import ChatStore
from rook.core.plugin import SettingsView
from rook.hub import integrations
from rook.hub.authz import Authorizer
from rook.hub.integrations import EXAMPLE_EXEC_RULE, Inbound, RateLimiter, split_message
from rook.hub.plugins.discord import Discord
from rook.hub.plugins.notify import Notify
from rook.hub.plugins.policy import PolicyPlugin
from rook.hub.plugins.telegram import Telegram
from rook.hub.policy import DEFAULT_POLICY, PolicyStore

TG_TOKEN = "123456789:AAFakeTelegramTokenForTestsOnly_abcdefghij"
DC_TOKEN = "MTAxFakeDiscordTokenPartOne.GhIjKl.FakeDiscordTokenSecretPartForTests123"
CHAT = "-100777"
CHANNEL = "555000"


async def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# -- fakes ---------------------------------------------------------------------

class FakeTelegram:
    def __init__(self, token=TG_TOKEN):
        self.token = token
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.fail_next: list[tuple[int, dict]] = []
        self.next_id = 100
        self.update_id = 0
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        self.server = TestServer(app)

    async def __aenter__(self):
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    @property
    def base(self):
        return str(self.server.make_url("")).rstrip("/")

    def push(self, text, user_id=42, username="alice", chat=CHAT, is_bot=False, reply_to=None):
        self.update_id += 1
        msg = {"message_id": 1000 + self.update_id, "chat": {"id": int(chat)},
               "from": {"id": user_id, "is_bot": is_bot, "username": username}, "text": text}
        if reply_to:
            msg["reply_to_message"] = {"message_id": int(reply_to)}
        self.updates.append({"update_id": self.update_id, "message": msg})

    async def handle(self, request):
        if request.match_info["token"] != self.token:
            return web.json_response({"ok": False, "error_code": 401,
                                      "description": "Unauthorized"}, status=401)
        method = request.match_info["method"]
        body = await request.json() if request.can_read_body else {}
        if method == "getMe":
            return web.json_response({"ok": True, "result": {"id": 999, "is_bot": True,
                                                             "username": "rook_test_bot"}})
        if method == "getUpdates":
            for _ in range(10):
                if self.updates:
                    break
                await asyncio.sleep(0.02)
            out = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            self.updates = []
            return web.json_response({"ok": True, "result": out})
        if method == "sendMessage":
            if self.fail_next:
                status, payload = self.fail_next.pop(0)
                return web.json_response(payload, status=status)
            self.next_id += 1
            self.sent.append(body)
            return web.json_response({"ok": True, "result": {"message_id": self.next_id}})
        return web.json_response({"ok": False, "error_code": 404}, status=404)


class FakeDiscord:
    def __init__(self, token=DC_TOKEN):
        self.token = token
        self.sent: list[dict] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.identified = asyncio.Event()
        self.seq = 1
        app = web.Application()
        app.router.add_get("/users/@me", self.me)
        app.router.add_get("/gateway/bot", self.gateway)
        app.router.add_post("/channels/{cid}/messages", self.post)
        app.router.add_get("/gw", self.ws)
        self.server = TestServer(app)

    async def __aenter__(self):
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    @property
    def base(self):
        return str(self.server.make_url("")).rstrip("/")

    def _auth(self, request):
        return request.headers.get("Authorization") == f"Bot {self.token}"

    async def me(self, request):
        if not self._auth(request):
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        return web.json_response({"id": "999", "username": "rookbot"})

    async def gateway(self, request):
        url = str(self.server.make_url("/gw")).replace("http://", "ws://")
        return web.json_response({"url": url})

    async def post(self, request):
        body = await request.json()
        body["_channel"] = request.match_info["cid"]
        self.sent.append(body)
        return web.json_response({"id": str(7000 + len(self.sent))})

    def push(self, content, author_id="42", username="bob", bot=False, channel=CHANNEL,
             mentions=(), reply_to=None):
        self.seq += 1
        d = {"id": str(9000 + self.seq), "channel_id": channel, "content": content,
             "author": {"id": author_id, "username": username, "bot": bot},
             "mentions": [{"id": i, "username": n} for i, n in mentions]}
        if reply_to:
            d["message_reference"] = {"message_id": reply_to}
        self.events.put_nowait({"op": 0, "t": "MESSAGE_CREATE", "s": self.seq, "d": d})

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 45000}})
        ident = await ws.receive_json()
        assert ident["op"] == 2
        if ident["d"]["token"] != self.token:
            await ws.close(code=4004)
            return ws
        self.identified.set()
        await ws.send_json({"op": 0, "t": "READY", "s": 1,
                            "d": {"user": {"id": "999", "username": "rookbot"}, "session_id": "s"}})
        while not ws.closed:
            try:
                ev = await asyncio.wait_for(self.events.get(), 0.1)
            except asyncio.TimeoutError:
                continue
            await ws.send_json(ev)
        return ws


class FakeClient:
    def __init__(self, authz, workers):
        self.authz = authz
        self.workers = workers
        self.calls: list[dict] = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None,
                   principal=None, _decision=None):
        self.calls.append({"cap": cap, "args": args, "target": target, "identity": identity,
                           "principal": getattr(principal, "id", None)})
        return {"ok": True, "result": {"ran": cap, "echo": args}}


WORKERS = {"wid-a": {"name": "worker-a", "caps": ["shell.exec"]},
           "wid-b": {"name": "worker-b", "caps": ["shell.exec"]},
           "wid-c": {"name": "worker-c", "caps": ["shell.exec", "hub.echo"],
                     "tiers": {"hub.echo": "w"}}}


def make_node(tmp_path, rules=()):
    path = tmp_path / "policy.json"
    doc = {**DEFAULT_POLICY, "rules": list(DEFAULT_POLICY["rules"]) + list(rules)}
    path.write_text(json.dumps(doc))
    records = []
    authz = Authorizer(PolicyStore(str(path), check_every=0), None,
                       record=lambda **kw: records.append(kw))
    node = SimpleNamespace(worker_id="hub-id", _state_dir=str(tmp_path),
                           entry=lambda: {"name": "rook", "roles": ["is_hub"]},
                           client=FakeClient(authz, dict(WORKERS)), records=records)
    node.plugins = {}
    node.plugin = lambda ns: node.plugins.get(ns)
    return node


def wire(plugin, tmp_path, token, **stored):
    stored = {"enabled": True, **stored}
    vault_key = f"plugin.{plugin.NAMESPACE}.token"
    plugin.__dict__["_settings"] = SettingsView(
        plugin, stored, secrets=lambda k: token if k == vault_key else None)
    plugin.__dict__["_data_root"] = str(tmp_path / "plugins")
    plugin.POLL_SECS = 0.05
    return plugin


@pytest.fixture
def chat(tmp_path):
    store = ChatStore(str(tmp_path / "chat.db"))
    yield store
    store.close()


# -- Telegram ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_bridge_attribution_and_loop_prevention(tmp_path, chat):
    room = chat.start("ops", "agent:claude", [])["room"]
    chat.send(room, "agent:claude", "old history, not relayed", [], False)
    node = make_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rooms=[room], api_base=tg.base)
        p.LONG_POLL_SECS = 1
        p.bind_host(node)
        p.use_chat_store(chat)
        await p.start()
        try:
            await wait_for(lambda: p.state["connected"])
            await asyncio.sleep(0.15)
            assert tg.sent == []                          # history is not replayed

            chat.send(room, "agent:claude", "deploy finished", [], False)
            await wait_for(lambda: len(tg.sent) == 1)
            assert tg.sent[0]["text"] == "agent:claude: deploy finished"
            assert tg.sent[0]["chat_id"] == CHAT
            assert "parse_mode" not in tg.sent[0]         # plain text: no markup injection

            tg.push("thanks!", username="alice")
            await wait_for(lambda: any(m["sender"] == "telegram:alice"
                                       for m in chat.read(room, None)["messages"]))
            msgs = chat.read(room, None)["messages"]
            assert msgs[-1]["text"] == "thanks!"
            await asyncio.sleep(0.2)
            assert len(tg.sent) == 1                      # never echoed back to Telegram

            tg.push("bot chatter", username="otherbot", is_bot=True)
            tg.push("from elsewhere", chat="-100999")
            await asyncio.sleep(0.3)
            assert [m["text"] for m in chat.read(room, None)["messages"]][-1] == "thanks!"
            assert p.status()["relayed_in"] == 1 and p.status()["relayed_out"] == 1
        finally:
            await p.stop()


@pytest.mark.asyncio
async def test_telegram_mentions_map_both_ways(tmp_path, chat):
    room = chat.start("ops", "agent:claude", ["agent:bob"])["room"]
    node = make_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rooms=[room], api_base=tg.base,
                 mentions={"alice": "user:operator", "bobby": "agent:bob"})
        p.bind_host(node)
        p.use_chat_store(chat)
        p._watermarks = {room: 0}
        chat.send(room, "agent:claude", "@user:operator please review", ["user:operator"], True)
        await p.connect()
        try:
            assert await p.bridge_once() == 1
            assert tg.sent[0]["text"] == "agent:claude: @alice please review"
        finally:
            await p.disconnect()
        res = await p.handle_inbound(Inbound(chat_id=CHAT, user_id="42", username="alice",
                                             text="ping @bobby and @agent:claude"))
        assert res == "bridged"
        last = chat.read(room, None)["messages"][-1]
        assert last["sender"] == "telegram:alice"
        assert last["mentions"] == ["agent:bob", "agent:claude"] and last["expects_reply"]


@pytest.mark.asyncio
async def test_replies_route_to_the_room_they_answer(tmp_path, chat):
    r1 = chat.start("one", "agent:a", [])["room"]
    r2 = chat.start("two", "agent:b", [])["room"]
    node = make_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rooms=[r1, r2], api_base=tg.base)
        p.bind_host(node)
        p.use_chat_store(chat)
        p._watermarks = {r1: 0, r2: 0}
        chat.send(r2, "agent:b", "question in two", [], False)
        await p.connect()
        try:
            await p.bridge_once()
        finally:
            await p.disconnect()
        assert tg.sent[0]["text"] == "[two] agent:b: question in two"
        mid = str(tg.next_id)
    await p.handle_inbound(Inbound(chat_id=CHAT, user_id="42", username="alice",
                                   text="answer", reply_to=mid))
    await p.handle_inbound(Inbound(chat_id=CHAT, user_id="42", username="alice",
                                   text="not a reply"))
    assert chat.read(r2, None)["messages"][-1]["text"] == "answer"
    assert chat.read(r1, None)["messages"][-1]["text"] == "not a reply"   # first room


@pytest.mark.asyncio
async def test_commands_run_as_integration_and_exec_is_denied(tmp_path):
    node = make_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base,
                 commands=["help", "workers", "call"], allowed_caps=["shell.exec", "hub.echo"])
        p.bind_host(node)
        p.bot_name = "rook_test_bot"
        await p.connect()
        try:
            async def say(text, user="42"):
                before = len(tg.sent)
                await p.handle_inbound(Inbound(chat_id=CHAT, user_id=user, username="alice",
                                               text=text, message_id="5"))
                return tg.sent[before]["text"] if len(tg.sent) > before else None

            assert "/workers" in await say("/help")
            assert await say("/workers@rook_test_bot") == "3 workers: worker-a, worker-b, worker-c"
            assert await say("/workers@some_other_bot") is None
            assert "not enabled" in await say("/rooms")
            assert await say("/unknown thing") is None

            # exec: denied by the integration:* defaults, even in audit mode.
            reply = await say('/call worker-c shell.exec {"cmd": "id"}')
            assert reply.startswith("denied: integration:telegram may not call shell.exec (exec)")
            assert node.client.calls == []
            assert any(r["identity"] == "integration:telegram" and r["cap"] == "shell.exec"
                       for r in node.records)                # the would-deny is journaled
            assert "not in this integration's allowed caps" in await say("/call worker-c hub.other")

            # a write-tier cap passes and runs under the integration principal
            reply = await say('/call worker-c hub.echo {"x": 1}')
            assert json.loads(reply)["ran"] == "hub.echo"
            call = node.client.calls[-1]
            assert call["principal"] == "integration:telegram" and call["target"] == "wid-c"
            assert call["identity"] == "integration:telegram/user:42"
            assert p.status()["denied"] == 1
        finally:
            await p.disconnect()


def test_policy_explain_integration_defaults_and_example_rule(tmp_path):
    for rules, want_a in (((), {"would_deny"}), ((EXAMPLE_EXEC_RULE,), {"allow"})):
        node = make_node(tmp_path, rules)
        pol = PolicyPlugin()
        pol.bind_host(node)
        a = pol.explain("integration:telegram", "shell.exec", "worker-a")
        c = pol.explain("integration:telegram", "shell.exec", "worker-c")
        assert a["decision"] in want_a and a["tier"] == "exec"
        assert c["decision"] == "would_deny" and c["rule"] == "default:role:integration"
        assert pol.explain("integration:telegram", "hub.info", "rook")["decision"] == "allow"
        assert pol.explain("integration:discord", "worker.restart", "worker-a")["decision"] \
            == "would_deny"                                   # admin: never
        assert pol.explain("integration:discord", "task.write", "rook")["decision"] == "allow"
    assert a["rule"] == "telegram-exec-lab"


@pytest.mark.asyncio
async def test_example_rule_grants_exec_on_named_workers_only(tmp_path):
    node = make_node(tmp_path, (EXAMPLE_EXEC_RULE,))
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base,
                 commands=["call"], allowed_caps=["shell.exec"])
        p.bind_host(node)
        await p.connect()
        try:
            await p.handle_inbound(Inbound(chat_id=CHAT, user_id="42", username="a",
                                           text='/call worker-a shell.exec {"cmd": "id"}'))
            await p.handle_inbound(Inbound(chat_id=CHAT, user_id="42", username="a",
                                           text='/call worker-c shell.exec {"cmd": "id"}'))
        finally:
            await p.disconnect()
    assert [c["target"] for c in node.client.calls] == ["wid-a"]
    assert tg.sent[1]["text"].startswith("denied:")


@pytest.mark.asyncio
async def test_command_users_and_no_policy_means_no_commands(tmp_path):
    node = make_node(tmp_path)
    p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, command_users=["7"])
    p.bind_host(node)
    m = Inbound(chat_id=CHAT, user_id="42", username="x", text="")
    assert "not allowed" in await p.run_command("workers", m)
    m.user_id = "7"
    assert "workers" in await p.run_command("workers", m)
    node.client.authz = None
    assert "permissions are not configured" in await p.run_command("workers", m)


@pytest.mark.asyncio
async def test_secrets_never_reach_logs_or_replies(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    node = make_node(tmp_path)

    async def leaky_call(**kw):
        return {"ok": True, "result": f"env says TOKEN={TG_TOKEN}"}
    node.client.call = leaky_call
    async with FakeTelegram() as tg:
        tg.fail_next = [(400, {"ok": False, "error_code": 400,
                               "description": f"bad request for bot{TG_TOKEN}"})]
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base,
                 commands=["call"], allowed_caps=["hub.*"])
        p.bind_host(node)
        await p.connect()
        try:
            res = await p.send("hello")
            assert res["ok"] is False and TG_TOKEN not in json.dumps(res)
            await p.handle_inbound(Inbound(chat_id=CHAT, user_id="42", username="a",
                                           text="/call worker-c hub.echo"))
            assert TG_TOKEN not in tg.sent[-1]["text"] and "***" in tg.sent[-1]["text"]
            # a token-shaped string that is not ours is masked too
            assert p.mask("x 987654321:ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ y") == "x *** y"
        finally:
            await p.disconnect()
    status = json.dumps(p.status())
    assert TG_TOKEN not in status and '"token_set": true' in status
    assert p.settings.as_dict()["token"] == "***"
    # (the fake server's own aiohttp access log sees the URL; our loggers must not)
    assert all(TG_TOKEN not in r.getMessage() for r in caplog.records
               if not r.name.startswith("aiohttp"))
    assert "***" in (p.state["last_error"] or "")


@pytest.mark.asyncio
async def test_rate_limits_and_429_retry(tmp_path):
    node = make_node(tmp_path)
    p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rate_in=2)
    p.bind_host(node)
    m = lambda: Inbound(chat_id=CHAT, user_id="42", username="a", text="hi")  # noqa: E731
    assert await p.handle_inbound(m()) == "ignored:no-bridge"
    assert await p.handle_inbound(m()) == "ignored:no-bridge"
    assert await p.handle_inbound(m()) == "dropped:rate"
    other = Inbound(chat_id=CHAT, user_id="43", username="b", text="hi")
    assert await p.handle_inbound(other) == "ignored:no-bridge"      # per user

    bucket = RateLimiter(3, per=60)
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]
    assert 0 < bucket.wait_time() <= 20

    async with FakeTelegram() as tg:
        tg.fail_next = [(429, {"ok": False, "error_code": 429,
                               "parameters": {"retry_after": 0}})]
        p2 = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base)
        await p2.start()
        try:
            assert (await p2.send("after a 429"))["ok"]
            assert tg.sent[-1]["text"] == "after a 429"
        finally:
            await p2.stop()


@pytest.mark.asyncio
async def test_burst_is_summarized_not_flooded(tmp_path, chat):
    room = chat.start("busy", "agent:a", [])["room"]
    node = make_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rooms=[room], api_base=tg.base,
                 rate_out=1000)
        p.MAX_BURST = 3
        p.bind_host(node)
        p.use_chat_store(chat)
        p._watermarks = {room: 0}
        for i in range(8):
            chat.send(room, "agent:a", f"m{i}", [], False)
        await p.connect()
        try:
            await p.bridge_once()
        finally:
            await p.disconnect()
    texts = [s["text"] for s in tg.sent]
    assert texts[0].startswith("(5 earlier messages") and texts[1:] == [
        "agent:a: m5", "agent:a: m6", "agent:a: m7"]


def test_long_messages_split():
    parts = split_message("a" * 50 + "\n\n" + "b" * 50, 60)
    assert parts == ["a" * 50, "b" * 50]
    assert all(len(x) <= 10 for x in split_message("x" * 95, 10))


def test_available_needs_enabled_and_aiohttp(tmp_path, monkeypatch):
    p = Telegram()
    p.__dict__["_settings"] = SettingsView(p, {})
    assert p.available() is False
    wire(p, tmp_path, TG_TOKEN)
    assert p.available() is True
    monkeypatch.setattr(integrations, "aiohttp_available", lambda: False)
    assert p.available() is False


def test_hub_node_loads_integrations_only_when_enabled(tmp_path, monkeypatch):
    from rook.hub.node import HubNode
    monkeypatch.setenv("ROOK_SETTINGS_DB", str(tmp_path / "settings.db"))
    node = HubNode(str(tmp_path), entry_points=False)
    assert not node.has("telegram.send") and node.has("notify.send")
    monkeypatch.setenv("ROOK_TELEGRAM", "1")
    node = HubNode(str(tmp_path / "b"), entry_points=False)
    assert node.has("telegram.send") and node.host.tiers()["telegram.send"] == "w"
    assert node.host.status["telegram"]["state"] == "loaded"


# -- notify ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_send_fans_out(tmp_path):
    node = make_node(tmp_path)
    n = Notify()
    n.bind_host(node)
    assert (await n.send("x"))["ok"] is False
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base)
        await p.connect()
        node.plugins["telegram"] = p
        try:
            res = await n.send("disk almost full")
            assert res == {"ok": True, "sent": ["telegram"]}
            assert tg.sent[-1] == {"chat_id": CHAT, "text": "disk almost full",
                                   "disable_web_page_preview": True}
            assert (await n.send("x", channel="discord"))["ok"] is False
            assert (await n.send("x", channel="sms"))["ok"] is False
            assert (await p.send("x", chat="-1001"))["ok"] is False   # only the configured chat
            assert n.channels() == {"channels": ["telegram"]}
        finally:
            await p.disconnect()


# -- Discord ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discord_gateway_bridge_and_mentions(tmp_path, chat):
    room = chat.start("ops", "agent:claude", [])["room"]
    node = make_node(tmp_path)
    async with FakeDiscord() as dc:
        p = wire(Discord(), tmp_path, DC_TOKEN, chat_id=CHANNEL, rooms=[room], api_base=dc.base,
                 mentions={"4242": "user:operator"}, commands=["help", "workers"])
        p.bind_host(node)
        p.use_chat_store(chat)
        await p.start()
        try:
            await asyncio.wait_for(dc.identified.wait(), 5)
            await wait_for(lambda: p.state["connected"] and p.bot_id == "999")

            dc.push("hi <@999> and <@4242>", mentions=[("999", "rookbot"), ("4242", "opname")])
            await wait_for(lambda: any(m["sender"] == "discord:bob"
                                       for m in chat.read(room, None)["messages"]))
            last = chat.read(room, None)["messages"][-1]
            assert last["text"] == "hi and @opname"            # own mention dropped
            assert last["mentions"] == ["user:operator"]

            dc.push("I am a bot", bot=True)
            dc.push("self", author_id="999", username="rookbot")
            chat.send(room, "agent:claude", "@user:operator shipped @everyone", ["user:operator"],
                      False)
            await wait_for(lambda: len(dc.sent) == 1)
            out = dc.sent[0]
            assert out["_channel"] == CHANNEL
            assert out["content"] == "agent:claude: <@4242> shipped @everyone"
            assert out["allowed_mentions"] == {"parse": [], "users": ["4242"]}

            dc.push("!workers")
            await wait_for(lambda: len(dc.sent) == 2)
            assert dc.sent[1]["content"] == "3 workers: worker-a, worker-b, worker-c"
            assert dc.sent[1]["message_reference"]["message_id"]
            await asyncio.sleep(0.2)
            senders = [m["sender"] for m in chat.read(room, None)["messages"]]
            assert senders.count("discord:bob") == 1          # bots and commands not bridged
            assert len(dc.sent) == 2                          # discord:bob never echoed back
        finally:
            await p.stop()


@pytest.mark.asyncio
async def test_discord_bad_token_backs_off_and_is_masked(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    node = make_node(tmp_path)
    async with FakeDiscord(token="the-real-one") as dc:
        p = wire(Discord(), tmp_path, DC_TOKEN, chat_id=CHANNEL, api_base=dc.base)
        p.bind_host(node)
        await p.start()
        try:
            await wait_for(lambda: p.state["last_error"])
            assert "bad token" in p.state["last_error"]
            assert p.state["connected"] is False
        finally:
            await p.stop()
    assert all(DC_TOKEN not in r.getMessage() for r in caplog.records
               if not r.name.startswith("aiohttp"))
    assert DC_TOKEN not in json.dumps(p.status())


# -- watchdog --------------------------------------------------------------------------

def test_watchdog_alerts_through_the_hub_then_falls_back(monkeypatch):
    from rook.band_mcp import watchdog
    monkeypatch.setenv("ROOK_MCP_STATIC_TOKEN", "t")
    monkeypatch.setenv("ROOK_WATCHDOG_VIA_HUB", "1")
    direct = []
    monkeypatch.setattr(watchdog, "telegram", lambda text: direct.append(text) or True)
    calls = []
    reply = {"ok": True, "result": {"ok": True, "sent": ["telegram"]}}

    def http(url, method="GET", body=None, headers=None, timeout=15):
        calls.append((method, body))
        if body and body.get("method") == "initialize":
            return 200, {"mcp-session-id": "sid"}, "{}"
        if body and body.get("method") == "tools/call":
            rpc = {"jsonrpc": "2.0", "id": 2,
                   "result": {"content": [{"type": "text", "text": json.dumps(reply)}]}}
            return 200, {}, "event: message\ndata: " + json.dumps(rpc) + "\n\n"
        return 200, {}, ""
    monkeypatch.setattr(watchdog, "http", http)

    assert watchdog.alert("hub down?") is True and direct == []
    tool = next(b for m, b in calls if b and b.get("method") == "tools/call")
    assert tool["params"]["arguments"] == {"cap": "notify.send", "worker": "rook",
                                           "args": {"text": "hub down?", "channel": "all"}}
    reply = {"ok": True, "result": {"ok": False, "error": "no chat integration is running"}}
    assert watchdog.alert("again") is True and direct == ["again"]
    monkeypatch.setenv("ROOK_WATCHDOG_VIA_HUB", "0")
    calls.clear()
    watchdog.alert("direct only")
    assert calls == [] and direct[-1] == "direct only"
