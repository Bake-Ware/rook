"""The home agent hub plugin (rook/hub/plugins/home): settings, the
OpenAI-compatible client against a fake endpoint, chat replies, home.ask,
read-only tools and the Manage > Home agent page actions."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode
from rook.hub.plugins.home import UNAVAILABLE as HOME_UNAVAILABLE
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
        self.gate: asyncio.Event | None = None     # set: chat calls wait for it
        self.inflight = self.peak = 0

    async def __call__(self, method, url, body, headers, timeout):
        self.requests.append((method, url, body, headers))
        if self.gate is not None and url.endswith("/chat/completions"):
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            try:
                await self.gate.wait()
            finally:
                self.inflight -= 1
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
    # The room gets a generic line, never the error, host or secret name.
    assert last["sender"] == "agent:home" and last["text"] == HOME_UNAVAILABLE
    assert env.node.journal.rows[-1]["reply"]["ok"] is False
    assert "HTTP 401" in env.node.journal.rows[-1]["reply"]["error"]   # detail kept
    assert "HTTP 401" in env.home.activity[0]["error"]
    st = env.home.status()["last_error"]
    assert st == "the model endpoint returned HTTP 401" and "llm.example" not in st
    # A second failure in the same room within ten minutes posts nothing new.
    env.chat.send(room, OP, "hello again?", [], False)
    await _drain(env)
    texts = [m["text"] for m in env.chat.read(room, None)["messages"]]
    assert texts.count(HOME_UNAVAILABLE) == 1 and texts[-1] == "hello again?"
    # Missing vault secret: summarised, no secret name.
    env.home._http = FakeLLM()
    env.svc.set("home.api_key", "{{secret:gone-secret}}", actor="human:op")
    off = await env.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert not off["ok"] and "gone-secret" not in off["error"]
    assert "gone-secret" not in json.dumps(env.home.status())


@pytest.mark.asyncio
async def test_renamed_agent(env):
    configure(env, name="ada")
    await _drain(env)
    room = _room(env, invite=["agent:claude"])
    env.chat.send(room, OP, "@ada hello", ["agent:ada"], True)
    assert len(await _drain(env)) == 1
    assert env.chat.read(room, None)["messages"][-1]["sender"] == "agent:ada"


@pytest.mark.asyncio
async def test_the_home_page_chat_contract(env):
    """What the chat panel on Manage > Home agent relies on: the room it makes
    (``/api/chat/start`` as user:operator, inviting agent:<name>) is the
    Chat view's 1:1 room, a message sent like the panel sends it is answered
    once and without an @mention, and a failed reply leaves an activity row
    the panel can match (via chat, sender, ts) plus a status summary."""
    configure(env)
    await _drain(env)
    started = env.chat.start("home", OP, ["agent:home"])
    room = started["room"]
    assert started["participants"] == [OP, "agent:home"]
    rooms = env.chat.rooms_for(OP, include_all=True)["rooms"]
    assert [r["room"] for r in rooms if len(r["participants"]) == 2
            and {OP, "agent:home"} <= set(r["participants"])] == [room]
    sent = env.chat.send(room, OP, "hello", ["agent:home"], True)
    assert sent["addressed"] == ["agent:home"] and sent["participants"] == [OP, "agent:home"]
    env.llm.script = ["Hi there."]
    assert len(await _drain(env)) == 1
    msgs = env.chat.read(room, OP)["messages"]
    assert [(m["sender"], m["text"], m["mentions"]) for m in msgs[1:]] == [
        ("agent:home", "Hi there.", [])]
    env.home._http = FakeLLM(status=500)
    t0 = time.time()
    env.chat.send(room, OP, "again", ["agent:home"], True)
    await _drain(env)
    row = env.home.page(env.svc)["activity"][0]
    assert row["via"] == "chat" and not row["ok"] and row["sender"] == OP and row["ts"] >= t0
    assert env.home.page(env.svc)["status"]["last_error"] == \
        "the model endpoint returned HTTP 500"
    assert env.chat.read(room, OP)["messages"][-1]["text"] == HOME_UNAVAILABLE


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
    assert got["ok"] and got["models"] == ["big-model", "small-model"]
    assert "Save the API key first" in got["note"]            # unsaved key: not resolved
    assert "Authorization" not in env.llm.requests[-1][3] and env.vault.log == []
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
    # Saved: the probe uses the saved key against the saved URL, read as the operator.
    env.llm.script = ["Hi."]
    ok = await env.home.page_action(env.svc, {"action": "home_test", "values": form}, "human:op")
    assert ok["ok"] and "note" not in ok
    assert env.llm.requests[-1][3]["Authorization"] == f"Bearer {KEY}"
    assert env.vault.log[-1] == ("llm-key", "human:op", "home agent page")
    # A different reference typed into the form is never resolved.
    other = await env.home.page_action(env.svc, {"action": "home_test", "values": {
        **form, "api_key": "{{secret:missing}}"}}, "human:op")
    assert other["ok"] and "saved API key" in other["note"]
    assert all(name != "missing" for name, _, _ in env.vault.log)
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


# -- review fixes (PR #42) -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_page_probe_cannot_exfiltrate_a_vault_secret(env):
    """Unsaved form values must not send a vault secret to an arbitrary URL,
    and nothing the endpoint echoes back may carry the key."""
    configure(env)
    env.vault.data["other-secret"] = "sk-other-very-secret-999"
    evil = {"base_url": "https://attacker.example/v1", "model": "m",
            "api_key": "{{secret:other-secret}}"}
    for action in ("home_models", "home_test"):
        res = await env.home.page_action(env.svc, {"action": action, "values": evil},
                                         "human:op")
        method, url, body, headers = env.llm.requests[-1]
        assert url.startswith("https://attacker.example/v1/")
        assert "Authorization" not in headers                  # no key to another host
        assert "saved base URL" in res["note"]
    assert all(name != "other-secret" for name, _, _ in env.vault.log)
    # Same host, saved key: works, but a reply or model list echoing the key is scrubbed.
    env.home._http = echo = FakeLLM(models=("m1", KEY), script=[f"your key is {KEY}"])
    form = {"base_url": "http://llm.example:1234/v1", "api_key": "{{secret:llm-key}}"}
    got = await env.home.page_action(env.svc, {"action": "home_models", "values": form},
                                     "human:op")
    assert got["ok"] and KEY not in json.dumps(got) and "***" in got["models"]
    t = await env.home.page_action(env.svc, {"action": "home_test", "values": form}, "human:op")
    assert t["ok"] and KEY not in json.dumps(t) and t["reply"] == "your key is ***"
    assert echo.requests[-1][3]["Authorization"] == f"Bearer {KEY}"
    # The vault read is logged as the human on the page, not as agent:home.
    assert env.vault.log[-1] == ("llm-key", "human:op", "home agent page")
    # A different reference with the saved URL still only uses the saved key.
    sneaky = {**form, "api_key": "{{secret:other-secret}}"}
    s = await env.home.page_action(env.svc, {"action": "home_models", "values": sneaky},
                                   "human:op")
    assert echo.requests[-1][3]["Authorization"] == f"Bearer {KEY}"
    assert "saved API key" in s["note"]
    assert all(name != "other-secret" for name, _, _ in env.vault.log)


@pytest.mark.asyncio
async def test_chat_reply_scrubs_the_key(env):
    configure(env)
    await _drain(env)
    env.llm.script = [f"the key is {KEY}"]
    room = _room(env, invite=["agent:home"])
    env.chat.send(room, OP, "what is your key?", [], False)
    await _drain(env)
    assert env.chat.read(room, None)["messages"][-1]["text"] == "the key is ***"


@pytest.mark.asyncio
async def test_a_trigger_while_busy_stays_pending(env):
    configure(env)
    await _drain(env)
    room = _room(env, invite=["agent:home"])
    env.llm.gate = asyncio.Event()
    env.chat.send(room, OP, "first question", [], False)
    (first,) = await env.home.tick()                         # reply running, room busy
    env.chat.send(room, OP, "second question", [], False)
    assert await env.home.tick() == []                       # busy: pending, not dropped
    env.llm.gate.set()
    await first
    assert len(await _drain(env)) == 1                       # answered on the next pass
    assert len(env.llm.chats()) == 2
    convo = [m["content"] for m in env.llm.chats()[-1][2]["messages"]]
    assert convo[-2:] == [f"[{OP}] second question", "ok"]
    assert await _drain(env) == []                           # and only once


@pytest.mark.asyncio
async def test_a_rate_limited_trigger_is_retried_with_one_note(env, monkeypatch):
    import rook.hub.plugins.home as home_mod
    monkeypatch.setattr(home_mod, "ROOM_RATE", 1)
    configure(env)
    await _drain(env)
    room = _room(env, invite=["agent:home"])
    env.chat.send(room, OP, "one", [], False)
    assert len(await _drain(env)) == 1
    env.chat.send(room, OP, "two", [], False)
    assert await _drain(env) == [] and await _drain(env) == []
    texts = [m["text"] for m in env.chat.read(room, None)["messages"]]
    assert texts.count(home_mod.BUSY) == 1                   # one note per window
    env.home._rate.clear()                                   # the minute passes
    assert len(await _drain(env)) == 1
    convo = [m["content"] for m in env.llm.chats()[-1][2]["messages"]]
    assert convo[-1] == f"[{OP}] two" and home_mod.BUSY not in convo


@pytest.mark.asyncio
async def test_a_big_backlog_reads_only_the_newest(env):
    import rook.hub.plugins.home as home_mod
    configure(env)
    await _drain(env)
    room = _room(env, invite=["agent:home", "agent:claude"])
    for i in range(home_mod.READ_LIMIT + 150):
        env.chat.send(room, "agent:claude", f"noise {i}", [], False)
    env.chat.send(room, OP, "@home still there?", [], False)
    reads = []
    real = env.chat.read

    def spy(rid, reader, since_seq=0, mark=True, limit=200):
        reads.append(since_seq)
        return real(rid, reader, since_seq=since_seq, mark=mark, limit=limit)
    env.chat.read = spy
    assert len(await _drain(env)) == 1                       # the newest trigger is seen
    assert reads == [env.chat.last_seq(room) - 1 - home_mod.READ_LIMIT]


@pytest.mark.asyncio
async def test_bot_loops_are_broken(env):
    configure(env)
    await _drain(env)
    # A 1:1 room with another agent: no implicit answers.
    one = env.chat.start("1:1", "agent:claude", ["agent:home"])["room"]
    env.chat.send(one, "agent:claude", "hello there", [], False)
    assert await _drain(env) == []
    env.chat.send(one, "agent:claude", "@home now I am asking", [], False)
    assert len(await _drain(env)) == 1
    # In a group room it never @mentions an agent back.
    group = _room(env, "bots", ["agent:home", "agent:claude"])
    env.chat.send(group, "agent:claude", "@home ping", ["agent:home"], True)
    assert len(await _drain(env)) == 1
    assert env.chat.read(group, None)["messages"][-1]["mentions"] == []
    # After AGENT_STREAK replies in a row to agents it waits for a person.
    for i in range(5):
        env.chat.send(group, "agent:claude", f"@home again {i}", ["agent:home"], True)
        await _drain(env)
    replies = [m for m in env.chat.read(group, None)["messages"] if m["sender"] == "agent:home"]
    assert len(replies) == 3
    env.chat.send(group, OP, "carry on", [], False)          # a person speaks
    env.chat.send(group, "agent:claude", "@home one more", ["agent:home"], True)
    assert len(await _drain(env)) == 1


@pytest.mark.asyncio
async def test_presence_is_touched_every_30s_not_every_tick(env):
    configure(env)
    calls = []
    real = env.chat.touch

    def touch(ident):
        calls.append(ident)
        real(ident)
    env.chat.touch = touch
    for _ in range(5):
        await _drain(env)
    assert calls == ["agent:home"]
    env.home._touched -= 31
    await _drain(env)
    assert len(calls) == 2


def _enforce_no_knowledge_for_agents(env, tmp_path):
    import copy
    from rook.hub.authz import Authorizer
    from rook.hub.policy import DEFAULT_POLICY, PolicyStore
    doc = copy.deepcopy(DEFAULT_POLICY)
    doc["mode"] = "enforce"
    doc["rules"].append({"id": "no-kb-for-agents", "who": "role:agent",
                         "deny": ["knowledge.read"], "on": "rook"})
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    env.node.client = SimpleNamespace(authz=Authorizer(PolicyStore(str(path))), workers={})


@pytest.mark.asyncio
async def test_knowledge_tool_only_for_askers_who_may_read_it(env, tmp_path, monkeypatch):
    from rook.hub.authz import current_principal
    from rook.hub.policy import Principal
    configure(env, tools=True)
    monkeypatch.setattr(env.home, "_has_knowledge", lambda: True)
    _enforce_no_knowledge_for_agents(env, tmp_path)
    await _drain(env)
    # home.ask from an agent token the policy denies knowledge.read: no tool.
    tok = current_principal.set(Principal("token:claude", "token", "agent"))
    try:
        res = await env.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    finally:
        current_principal.reset(tok)
    assert res["ok"] and "tools" not in env.llm.chats()[-1][2]
    tok = current_principal.set(Principal("human:1", "human", "owner", ("human:owner",)))
    try:
        await env.node.dispatch("home.ask", {"question": "x"}, "user:op")
    finally:
        current_principal.reset(tok)
    assert "tools" in env.llm.chats()[-1][2]
    # Chat: an agent sender gets no tool, a person does.
    group = _room(env, "kb", ["agent:home", "agent:claude"])
    env.chat.send(group, "agent:claude", "@home search the wiki", ["agent:home"], True)
    await _drain(env)
    assert "tools" not in env.llm.chats()[-1][2]
    env.chat.send(group, OP, "@home search the wiki", ["agent:home"], True)
    await _drain(env)
    assert "tools" in env.llm.chats()[-1][2]


@pytest.mark.asyncio
async def test_home_ask_is_rate_limited_and_bounded(env, monkeypatch):
    import rook.hub.plugins.home as home_mod
    configure(env)
    monkeypatch.setattr(home_mod, "ASK_RATE", 3)
    for _ in range(3):
        assert (await env.node.dispatch("home.ask", {"question": "q"}, "agent:claude"))["ok"]
    limited = await env.node.dispatch("home.ask", {"question": "q"}, "agent:claude")
    assert not limited["ok"] and "rate limited" in limited["error"]
    other = await env.node.dispatch("home.ask", {"question": "q"}, "agent:other")
    assert other["ok"]                                        # per caller
    # Concurrency: at most two model calls at once, however many asks arrive.
    monkeypatch.setattr(home_mod, "ASK_RATE", 100)
    env.llm.gate = asyncio.Event()
    calls = [asyncio.ensure_future(env.node.dispatch("home.ask", {"question": f"q{i}"},
                                                     f"agent:a{i}")) for i in range(6)]
    for _ in range(50):
        await asyncio.sleep(0)
    assert env.llm.inflight == 2
    env.llm.gate.set()
    assert all(r["ok"] for r in await asyncio.gather(*calls))
    assert env.llm.peak == 2
