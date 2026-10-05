"""The home agent hub plugin (rook/hub/plugins/home): settings, the
OpenAI-compatible client against a fake endpoint, chat replies, home.ask,
read-only tools and the Manage > Home agent page actions."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode
from rook.hub.plugins.home import HomeAgent, HomeError
from rook.hub.plugins.home.llm import ChatClient, LLMError, base_url, clean_reply
from rook.hub.settings_service import SettingsError
from rook.hub.settings_store import SettingsStore

KEY = "sk-test-not-a-real-key-123"
OP = "user:operator"


class FakeVault:
    def __init__(self, data=None):
        self.data, self.log = dict(data or {}), []

    def set(self, name, value, description, actor):
        self.data[name] = value
        return {"name": name}

    def get(self, name, actor, via="get", task=None):
        self.log.append((name, actor, via))
        return self.data[name]

    def delete(self, name, actor):
        return self.data.pop(name, None) is not None

    def list(self):
        return [{"name": n} for n in sorted(self.data)]


class FakeLLM:
    """An OpenAI-compatible endpoint as an injectable ``http`` coroutine.
    ``script`` is a list of replies for chat calls: a string (content), a
    dict (a raw message, e.g. with tool_calls) or an exception to raise."""

    def __init__(self, script=None, models=("small-model", "big-model"), status=200):
        self.script = list(script or [])
        self.models = list(models)
        self.status = status
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    async def __call__(self, method, url, body, headers, timeout):
        self.requests.append((method, url, body, headers))
        if self.status != 200:
            return self.status, {"error": {"message": f"bad key {KEY}"}}
        if url.endswith("/models"):
            return 200, {"object": "list", "data": [{"id": m} for m in self.models]}
        if url.endswith("/chat/completions"):
            item = self.script.pop(0) if self.script else "ok"
            if isinstance(item, BaseException):
                raise item
            msg = item if isinstance(item, dict) else {"role": "assistant", "content": item}
            return 200, {"model": body["model"], "choices": [
                {"index": 0, "message": msg, "finish_reason": "stop"}]}
        return 404, {"error": "not found"}

    def chats(self):
        return [r for r in self.requests if r[1].endswith("/chat/completions")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import rook.hub.plugins.settings as settings_plugin
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)
    vault = FakeVault({"llm-key": KEY})
    chat = ChatStore(str(tmp_path / "chat.db"))
    node = HubNode(str(tmp_path), entry_points=False, vault=vault, chat=chat,
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    node.journal = SimpleNamespace(rows=[])
    node.journal.record = lambda **kw: node.journal.rows.append(kw)
    home: HomeAgent = node.plugin("home")
    llm = FakeLLM()
    home._http = llm
    return SimpleNamespace(node=node, home=home, llm=llm, chat=chat, vault=vault,
                           svc=node.settings)


def configure(env, **extra):
    values = {"enabled": True, "base_url": "http://llm.example:1234/v1",
              "model": "small-model", "api_key": "{{secret:llm-key}}", **extra}
    for k, v in values.items():
        env.svc.set(f"home.{k}", v, actor="human:op")


# -- settings ----------------------------------------------------------------------

def test_plugin_loads_with_caps_and_settings(env):
    assert {"home.ask", "home.status"} <= set(env.node.caps())
    tiers = env.node.announce_msg()["tiers"]
    assert tiers["home.ask"] == "w" and tiers["home.status"] == "r"
    keys = {e.key for e in env.svc.schema if e.key.startswith("home.")}
    assert {"home.enabled", "home.base_url", "home.model", "home.api_key", "home.persona",
            "home.system_prompt", "home.tools"} <= keys
    st = env.home.status()
    assert st["enabled"] is False and st["identity"] == "agent:home"
    assert st["missing"] == ["base_url", "model"]


def test_api_key_is_only_ever_a_vault_reference(env):
    with pytest.raises(SettingsError):
        env.svc.set("home.api_key", KEY, actor="human:op")
    env.svc.set("home.api_key", "{{secret:llm-key}}", actor="human:op")
    assert env.home.settings["api_key"] == "{{secret:llm-key}}"
    assert KEY not in json.dumps(env.svc.store.history(key="home."))
    with pytest.raises(SettingsError):
        env.svc.set("home.name", "Not A Slug", actor="human:op")


def test_status_never_shows_the_key(env):
    configure(env)
    st = env.home.status()
    assert st["enabled"] and st["key_set"] and st["missing"] == []
    assert st["endpoint_host"] == "llm.example:1234" and KEY not in json.dumps(st)


# -- the client against a real HTTP server ---------------------------------------------

@pytest.mark.asyncio
async def test_client_over_real_http():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    seen = []

    async def models(request):
        seen.append(request.headers.get("Authorization"))
        return web.json_response({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]})

    async def chat(request):
        body = await request.json()
        seen.append(body)
        if body["messages"][-1]["content"] == "slow":
            await asyncio.sleep(5)
        if body["messages"][-1]["content"] == "fail":
            return web.json_response({"error": {"message": "overloaded " + KEY}}, status=503)
        return web.json_response({"model": body["model"], "choices": [{"message": {
            "role": "assistant", "content": "<think>hmm</think>Hello there."},
            "finish_reason": "stop"}]})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    async with TestServer(app) as server:
        url = str(server.make_url("/v1/chat/completions"))   # a pasted full URL is cut back
        cli = ChatClient(url, "a", KEY, timeout=5)
        assert cli.url.endswith("/v1")
        assert await cli.models() == ["a", "b"]
        assert seen[0] == f"Bearer {KEY}"
        res = await cli.chat([{"role": "user", "content": "hi"}], max_tokens=50)
        assert res["content"] == "Hello there." and res["model"] == "a"
        assert seen[-1]["max_tokens"] == 50 and seen[-1]["stream"] is False
        with pytest.raises(LLMError) as err:
            await cli.chat([{"role": "user", "content": "fail"}])
        assert "HTTP 503" in str(err.value) and KEY not in str(err.value)
        with pytest.raises(LLMError) as err:
            await cli.chat([{"role": "user", "content": "slow"}], timeout=0.3)
        assert "did not answer" in str(err.value)
    with pytest.raises(LLMError) as err:
        await ChatClient(url, "a", KEY, timeout=2).models()
    assert "cannot reach" in str(err.value) and KEY not in str(err.value)


def test_url_and_reply_helpers():
    assert base_url("http://x:1/v1/") == "http://x:1/v1"
    assert base_url("http://x:1/v1/models") == "http://x:1/v1"
    assert clean_reply("<think>\nplan\n</think>\n\nAnswer") == "Answer"
    with pytest.raises(LLMError):
        ChatClient("llm.example:1234")


# -- chat ----------------------------------------------------------------------------

def _room(env, title="lounge", invite=()):
    return env.chat.start(title, OP, list(invite))["room"]


async def _drain(env):
    tasks = await env.home.tick()
    if tasks:
        await asyncio.gather(*tasks)
    return tasks


@pytest.mark.asyncio
async def test_mention_in_a_room_gets_a_reply_with_room_context(env):
    configure(env, system_prompt="Keep it short.")
    room = _room(env, invite=["agent:claude"])
    env.chat.send(room, OP, "old news before the agent came online", [], False)
    assert await _drain(env) == []                       # baseline: history is not answered
    assert env.chat.is_online("agent:home")              # present in chat while enabled
    env.llm.script = ["Hi! The lights are on."]
    env.chat.send(room, "agent:claude", "context from claude", [], False)
    sent = env.chat.send(room, OP, "@home are the lights on?", ["agent:home"], True)
    assert "agent:home" in sent["participants"] and sent["offline"] == []
    assert len(await _drain(env)) == 1
    msgs = env.chat.read(room, None)["messages"]
    assert msgs[-1]["sender"] == "agent:home" and msgs[-1]["text"] == "Hi! The lights are on."
    assert msgs[-1]["mentions"] == [OP]                  # 3+ room: it addresses the asker
    method, url, body, headers = env.llm.chats()[-1]
    assert url == "http://llm.example:1234/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}" and body["model"] == "small-model"
    system = body["messages"][0]
    assert system["role"] == "system" and "agent:home" in system["content"]
    assert "Keep it short." in system["content"] and "lounge" in system["content"]
    convo = [m["content"] for m in body["messages"][1:]]
    assert "[agent:claude] context from claude" in convo
    assert convo[-1] == f"[{OP}] @home are the lights on?"
    # The key came from the vault, read as the home agent.
    assert ("llm-key", "agent:home", "home agent") in env.vault.log
    # Journaled as the home agent, not as the person who asked.
    row = env.node.journal.rows[-1]
    assert row["cap"] == "home.reply" and row["identity"] == "agent:home"
    assert row["audit"]["kind"] == "home" and row["thread_id"] == room
    assert row["reply"]["ok"] and KEY not in json.dumps(env.node.journal.rows)
    # Its own message does not trigger it again.
    assert await _drain(env) == []
    assert env.home.activity[0]["via"] == "chat" and env.home.activity[0]["ok"]


@pytest.mark.asyncio
async def test_who_it_answers(env):
    configure(env)
    await _drain(env)
    group = _room(env, "group", ["agent:home", "agent:claude"])
    env.chat.send(group, OP, "talking to claude, not the home agent", ["agent:claude"], True)
    assert await _drain(env) == []
    env.chat.send(group, OP, "and now @Home, what do you think?", [], False)  # text mention
    assert len(await _drain(env)) == 1
    dm = _room(env, "dm", ["agent:home"])
    env.chat.send(dm, OP, "no mention needed in a 1:1", [], False)
    assert len(await _drain(env)) == 1
    other = _room(env, "elsewhere", ["agent:claude"])
    env.chat.send(other, OP, "@home is not in this room", [], False)
    assert await _drain(env) == []                       # not a participant, no metadata mention


@pytest.mark.asyncio
async def test_disabled_or_unconfigured_does_nothing(env):
    env.svc.set("home.enabled", True, actor="human:op")   # no URL or model yet
    room = _room(env, invite=["agent:home"])
    env.chat.send(room, OP, "hello?", [], False)
    assert await _drain(env) == [] and env.llm.requests == []
    assert not env.chat.is_online("agent:home")


@pytest.mark.asyncio
async def test_a_failed_model_call_is_said_in_the_room(env):
    configure(env)
    await _drain(env)
    env.home._http = FakeLLM(status=401)
    room = _room(env, invite=["agent:home"])
    env.chat.send(room, OP, "hi", [], False)
    await _drain(env)
    last = env.chat.read(room, None)["messages"][-1]
    assert last["sender"] == "agent:home" and last["text"].startswith("(I could not answer:")
    assert "HTTP 401" in last["text"] and KEY not in last["text"]
    assert env.node.journal.rows[-1]["reply"]["ok"] is False
    assert "HTTP 401" in env.home.status()["last_error"]


@pytest.mark.asyncio
async def test_renamed_agent(env):
    configure(env, name="ada")
    await _drain(env)
    room = _room(env, invite=["agent:claude"])
    env.chat.send(room, OP, "@ada hello", ["agent:ada"], True)
    assert len(await _drain(env)) == 1
    assert env.chat.read(room, None)["messages"][-1]["sender"] == "agent:ada"


@pytest.mark.asyncio
async def test_persona_is_in_the_system_prompt(env):
    persona = env.node.plugin("persona")
    persona.store.save({"id": "calm", "name": "Ada", "voice": "Calm and dry."}, "human:op", "",
                       None, False)
    configure(env, persona="calm")
    assert "Calm and dry." in env.home.system_prompt(env.home.config(), "")
    env.svc.set("home.persona", "", actor="human:op")       # blank: family "home" resolution
    assert "Calm and dry." not in env.home.system_prompt(env.home.config(), "")
    persona.store.assign("family", "home", "calm", "human:op", "")
    assert "Calm and dry." in env.home.system_prompt(env.home.config(), "")


# -- home.ask and tools ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_ask(env):
    off = await env.node.dispatch("home.ask", {"question": "hi"}, "agent:claude")
    assert not off["ok"] and "off" in off["error"]
    configure(env)
    env.llm.script = ["Forty-two."]
    res = await env.node.dispatch("home.ask", {"question": "meaning?", "context": "a joke"},
                                  "agent:claude")
    assert res["ok"], res
    assert res["result"]["answer"] == "Forty-two." and res["result"]["agent"] == "agent:home"
    user = env.llm.chats()[-1][2]["messages"][-1]["content"]
    assert user.startswith("[agent:claude] meaning?") and "a joke" in user
    # Over the band it needs the operator to raise the ceiling (risk write).
    band = await env.node.dispatch("home.ask", {"question": "x"}, "anyone", source="band")
    assert not band["ok"] and "not callable over the band" in band["error"]


@pytest.mark.asyncio
async def test_rook_home_ask_tool_only_when_enabled(env):
    assert env.home.mcp_tools(lambda cap, args: None) == []
    configure(env)
    calls = []

    async def invoke(cap, args):
        calls.append((cap, args))
        return {"answer": "yes"}
    (tool,) = env.home.mcp_tools(invoke)
    assert tool.__name__ == "rook_home_ask"
    assert json.loads(await tool("q?")) == {"answer": "yes"}
    assert calls == [("home.ask", {"question": "q?", "context": ""})]


@pytest.mark.asyncio
async def test_knowledge_tool_runs_as_the_home_agent(env, monkeypatch):
    from rook.band_mcp import attribution
    from rook.core.context import caller_identity
    configure(env, tools=True)
    seen = []

    async def invoke(cap, args, identity=None):
        att = attribution.current.get()
        seen.append((cap, args, identity, caller_identity.get(), att.kind if att else None))
        return {"results": [{"slug": "lights", "excerpt": "The lights are on a timer."}]}
    monkeypatch.setattr(env.node, "invoke", invoke)
    monkeypatch.setattr(env.home, "_has_knowledge", lambda: True)
    env.llm.script = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "knowledge_search", "arguments": "{\"query\": \"lights\"}"}}]},
        "They are on a timer.",
    ]
    res = await env.node.dispatch("home.ask", {"question": "lights?"}, "agent:claude")
    assert res["ok"] and res["result"]["answer"] == "They are on a timer."
    assert res["result"]["tools_used"] == ["knowledge_search"]
    assert seen == [("knowledge.read", {"action": "search", "query": "lights"},
                     "agent:home", "agent:home", "home")]
    first, second = env.llm.chats()
    assert first[2]["tools"][0]["function"]["name"] == "knowledge_search"
    tool_msg = second[2]["messages"][-1]
    assert tool_msg["role"] == "tool" and "timer" in tool_msg["content"]
    row = env.node.journal.rows[-1]
    assert row["cap"] == "knowledge.read" and row["identity"] == "agent:home"
    assert attribution.current.get() is None              # restored afterwards


@pytest.mark.asyncio
async def test_tools_off_sends_no_tools(env):
    configure(env)
    await env.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert "tools" not in env.llm.chats()[-1][2]


# -- the Manage > Home agent page ------------------------------------------------------------

@pytest.mark.asyncio
async def test_page_actions(env):
    page = env.home.page(env.svc)
    assert page["secrets"] == ["llm-key"] and page["settings"]["model"]["value"] == ""
    assert "persona" in page["settings"] and page["providers"] == ["openai"]
    form = {"enabled": True, "base_url": "http://llm.example:1234/v1", "model": "",
            "api_key": "{{secret:llm-key}}"}
    got = await env.home.page_action(env.svc, {"action": "home_models", "values": form}, "human:op")
    assert got == {"ok": True, "models": ["big-model", "small-model"]}
    assert env.svc.store.get("home.base_url", "hub") is None   # listing saves nothing
    form["model"] = "big-model"
    env.llm.script = ["Hello from the test."]
    t = await env.home.page_action(env.svc, {"action": "home_test", "values": form}, "human:op")
    assert t["ok"] and t["reply"] == "Hello from the test." and t["model"] == "big-model"
    # Saving validates everything before writing anything.
    with pytest.raises(SettingsError):
        await env.home.page_action(env.svc, {"action": "home_save", "values": {
            **form, "api_key": KEY}}, "human:op")
    assert env.svc.store.get("home.enabled", "hub") is None
    saved = await env.home.page_action(env.svc, {"action": "home_save",
                                                 "values": {**form, "temperature": None}},
                                       "human:op")
    assert set(saved["saved"]) == {"home.enabled", "home.base_url", "home.model", "home.api_key"}
    assert saved["status"]["enabled"] and saved["status"]["model"] == "big-model"
    again = await env.home.page_action(env.svc, {"action": "home_save", "values": form},
                                       "human:op")
    assert again["saved"] == []
    assert env.svc.store.history(key="home.")[0]["actor"] == "human:op"
    bad = await env.home.page_action(env.svc, {"action": "home_test", "values": {
        **form, "api_key": "{{secret:missing}}"}}, "human:op")
    assert not bad["ok"] and "missing" in bad["error"]
    with pytest.raises(HomeError):
        await env.home.page_action(env.svc, {"action": "nope"}, "human:op")


@pytest.mark.asyncio
async def test_settings_web_routes_home_view(env):
    """The dashboard reaches the page through /settings/account-api."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from rook.hub.settings_web import routes

    class Accounts:
        def session(self, token):
            return {"id": "1", "username": "op", "admin": token == "admin", "csrf": "c"}
    app = Starlette(routes=routes(lambda: env.svc, Accounts()))
    with TestClient(app) as http:
        http.cookies.set("rook_account", "admin")
        page = http.get("/settings/account-api", params={"view": "home"}).json()
        assert page["view"] == "home" and page["status"]["identity"] == "agent:home"
        res = http.post("/settings/account-api", json={
            "csrf": "c", "action": "home_save", "values": {"model": "m1"}}).json()
        assert res["saved"] == ["home.model"]
        http.cookies.set("rook_account", "member")
        assert http.get("/settings/account-api", params={"view": "home"}).status_code == 403
        assert http.post("/settings/account-api", json={
            "csrf": "c", "action": "home_save", "values": {"model": "x"}}).status_code == 403
