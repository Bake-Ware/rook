"""Token cost of the MCP bridge: compact replies, per-session notices, roster
filters, lean knowledge search, slim tool listing, and compatibility for the
clients that parse replies (voice agent) and for workers that predate the
caps.describe prefix arg."""
import importlib
import json
import sys
import time
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from rook.band_mcp import envelope, roster
from rook.band_mcp.server import build_server

STATIC = "static-token-0123456789abcdef"
HEX = "0123456789abcdef0123456789abcdef"


class FakeBand:
    def __init__(self):
        now = time.time()
        self.sent = []
        self.workers = {
            "a" * 32: {"worker_id": "a" * 32, "name": "worker-a", "band": "x", "description": "build box",
                       "caps": ["shell.exec", "info.host", "caps.describe"], "plugins": ["shell", "info"],
                       "build": "167.brisk.otter", "hb": {"battery": {"percent": 70}}, "last_seen": now},
            "b" * 32: {"worker_id": "b" * 32, "name": "worker-b", "band": "x",
                       "caps": ["shell.exec", "info.host", "camera.snap"], "last_seen": now - 80},
            "c" * 32: {"worker_id": "c" * 32, "name": "worker-c", "band": "x",
                       "caps": ["shell.exec", "info.host"], "last_seen": now},
            "d" * 32: {"worker_id": "d" * 32, "name": "worker-c", "band": "x",
                       "caps": ["shell.exec"], "last_seen": now},
        }
        self.stdout = ""

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.sent.append((cap, dict(args or {})))
        if cap == "caps.describe":
            if args:  # a build-167 worker: unknown kwargs are "bad args"
                return {"id": HEX, "from": target, "ok": False, "error": f"bad args: {sorted(args)}"}
            return {"id": HEX, "from": target, "ok": True, "result": {
                "shell.exec": {"doc": "Run a command.", "params": [
                    {"name": "cmd", "required": False, "default": None, "type": "str | None"},
                    {"name": "timeout", "required": False, "default": 30.0, "type": "float"}]},
                "info.host": {"doc": "Host facts.", "params": []}}}
        if cap == "shell.exec":
            return {"id": HEX, "from": target, "ok": True,
                    "result": {"ok": True, "code": 0, "stdout": self.stdout, "stderr": ""}}
        return {"id": HEX, "from": target, "ok": True, "result": {"cap": cap}}


def _body(r):
    if r.headers["content-type"].startswith("application/json"):
        return r.json()
    return json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))


@asynccontextmanager
async def hub(tmp_path, monkeypatch, knowledge=False):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1" if knowledge else "0")
    monkeypatch.setenv("ROOK_KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    band = FakeBand()
    mcp, store = build_server(band, public_url="https://mcp.example.com",
                              persist_path=str(tmp_path / "tokens.json"), static_token=STATIC,
                              journal_path=str(tmp_path / "journal.db"))
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream"}) as http:
        async def connect(token=STATIC):
            h = {"Authorization": "Bearer " + token}
            r = await http.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
            h["mcp-session-id"] = r.headers["mcp-session-id"]
            await http.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

            async def rpc(method, params):
                return _body(await http.post("/mcp", headers=h, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params}))["result"]

            async def tool(name, **arguments):
                res = await rpc("tools/call", {"name": name, "arguments": arguments})
                return res["content"][0]["text"], res
            return rpc, tool
        yield SimpleNamespace(band=band, mcp=mcp, store=store, app=app, connect=connect)


# -- reply envelope ------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_shell_exec_reply_is_compact_and_single_copy(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        await tool("rook_call", cap="shell.exec", worker="worker-a")  # first call carries the cap tip
        text, res = await tool("rook_call", cap="shell.exec", worker="worker-a")
        assert "structuredContent" not in res  # no second, escaped copy of the payload
        assert len(text) <= 120, text
        assert json.loads(text) == {"ok": True, "id": HEX, "from": "worker-a", "result": {"code": 0}}
        assert "\n" not in text


@pytest.mark.asyncio
async def test_nonempty_and_failing_shell_results_keep_what_matters(tmp_path, monkeypatch):
    assert envelope.compact_result("shell.exec", {"ok": False, "code": 2, "stdout": "", "stderr": "boom"}) \
        == {"code": 2, "stderr": "boom"}
    assert envelope.compact_result("shell.exec", {"ok": True, "code": 0, "stdout": "hi\n", "stderr": ""}) \
        == {"code": 0, "stdout": "hi\n"}
    timeout = {"ok": False, "error": "timeout", "timeout": 30.0, "note": "killed"}
    assert envelope.compact_result("shell.exec", timeout) == timeout  # not shell-shaped: untouched
    assert envelope.compact_result("x.y", {"code": "abc"}) == {"code": "abc"}


@pytest.mark.asyncio
async def test_text_mode_returns_stdout_only(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        env.band.stdout = "hello\n"
        await tool("rook_call", cap="shell.exec", worker="worker-a")
        text, _ = await tool("rook_call", cap="shell.exec", worker="worker-a", text=True)
        assert text == "hello"
        env.band.stdout = ""
        text, _ = await tool("rook_call", cap="shell.exec", worker="worker-a", text=True)
        assert text == "[exit 0, no output]"
    fail = {"ok": True, "result": {"ok": False, "code": 1, "stdout": "partial\n", "stderr": "nope\n"}}
    assert envelope.call_text(fail, "shell.exec", {}) == "partial\n[stderr]\nnope\n[exit 1]"
    assert envelope.call_text({"ok": True, "result": "plain"}, "x.y", {"_task": "t1"}) == 'plain\n[rook] {"_task":"t1"}'
    assert envelope.call_text({"ok": False, "error": "x"}, "shell.exec", {}) is None  # errors stay JSON


@pytest.mark.asyncio
async def test_notices_only_when_new_for_the_session(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch, knowledge=True) as env:
        rpc, tool = await env.connect()
        k = env.mcp._rook_knowledge
        me = {"id": "static", "kind": "shared", "label": "static"}
        c = await k.dispatch("create", kind="concept", data={"title": "C"}, request_id="c", actor=me)
        p = await k.dispatch("create", kind="project", data={"title": "P", "parent": c["id"]}, request_id="p", actor=me)
        task = await k.dispatch("create", kind="task", data={"title": "T", "parent": p["id"]}, request_id="t", actor=me)
        text, _ = await tool("rook_task", action="claim", id=task["id"], request_id="c1")
        assert json.loads(text)["ok"]
        first = json.loads((await tool("rook_call", cap="info.host", worker="worker-a"))[0])
        assert first["_task"] == task["id"]
        again = json.loads((await tool("rook_call", cap="info.host", worker="worker-a"))[0])
        assert "_task" not in again

        # Unread chat: shown when it appears, not repeated, shown again when it changes.
        chat = env.mcp._rook_chat
        identity = json.loads((await tool("rook_whoami"))[0])["identity"]
        room = chat.start("ops", "agent:other", [identity])["room"]
        chat.send(room, "agent:other", "hi", [], False)
        r1 = json.loads((await tool("rook_call", cap="info.host", worker="worker-a"))[0])
        assert r1["_unread_chat"][0]["unread"] == 1
        r2 = json.loads((await tool("rook_call", cap="info.host", worker="worker-a"))[0])
        assert "_unread_chat" not in r2
        chat.send(room, "agent:other", "again", [], False)
        r3 = json.loads((await tool("rook_call", cap="info.host", worker="worker-a"))[0])
        assert r3["_unread_chat"][0]["unread"] == 2
        # A new MCP session is told again.
        _, tool2 = await env.connect()
        fresh = json.loads((await tool2("rook_call", cap="info.host", worker="worker-a"))[0])
        assert fresh["_unread_chat"] and fresh["_task"] == task["id"]


@pytest.mark.asyncio
async def test_legacy_envelope_restores_the_old_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_MCP_ENVELOPE", "legacy")
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        text, _ = await tool("rook_call", cap="shell.exec", worker="worker-a")
        reply = json.loads(text)
        assert text.startswith("{\n  ")
        assert reply["_journal_id"] == reply["id"] == HEX and reply["from"] == "a" * 32
        assert reply["result"] == {"ok": True, "code": 0, "stdout": "", "stderr": ""}
        rows = json.loads((await tool("rook_workers"))[0])
        assert set(rows[0]) >= {"worker_id", "caps", "plugins", "app_release"}
        caps = json.loads((await tool("rook_caps"))[0])
        assert {"cap": "camera.snap", "workers": ["worker-b"]} in caps


# -- caps.describe --------------------------------------------------------------

@pytest.mark.asyncio
async def test_caps_describe_prefix_filters_on_the_hub_for_old_workers(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        text, _ = await tool("rook_call", cap="caps.describe", worker="worker-a", args={"prefix": "shell."})
        reply = json.loads(text)
        assert reply["ok"], reply
        assert reply["result"] == {"shell.exec": "(cmd: str = None, timeout: float = 30.0) — Run a command."}
        assert all(args == {} for cap, args in env.band.sent if cap == "caps.describe")  # never sent


def test_worker_side_describe_prefix():
    from rook.worker.registry import CapabilityRegistry
    reg = CapabilityRegistry()
    reg.register("shell.exec", lambda cmd=None: None)
    reg.register("info.host", lambda: None)
    assert set(reg.describe()) == {"shell.exec", "info.host"}
    assert set(reg.describe("shell.")) == {"shell.exec"}


# -- rosters -------------------------------------------------------------------

def test_workers_view_defaults_filters_and_fields():
    band = FakeBand()
    rows = roster.workers_view(band.workers)
    assert [r["name"] for r in rows] == ["worker-a", "worker-b", "worker-c", "worker-c"]
    assert set(rows[0]) == {"name", "description", "build", "hb", "last_seen_age_secs"}
    assert "hb" not in rows[1] and "description" not in rows[1]  # empty values omitted
    assert all("worker_id" in r for r in rows[2:])  # shared name: id added so it can be targeted
    assert [r["name"] for r in roster.workers_view(band.workers, name="WORKER-B,worker-a")] == ["worker-a", "worker-b"]
    cam = roster.workers_view(band.workers, cap_prefix="camera.")
    assert cam == [{"name": "worker-b", "last_seen_age_secs": cam[0]["last_seen_age_secs"], "caps": ["camera.snap"]}]
    assert "worker-b" not in [r["name"] for r in roster.workers_view(band.workers, online=True)]
    assert roster.workers_view(band.workers, name="worker-a", fields="name,online") == [{"name": "worker-a", "online": True}]
    full = roster.workers_view(band.workers, name="worker-a", fields=["all"])[0]
    assert set(full) == {"worker_id", "name", "description", "serves", "band", "caps", "plugins", "version",
                         "build", "app_release", "hb", "last_seen_age_secs"}
    with pytest.raises(ValueError, match="unknown fields"):
        roster.workers_view(band.workers, fields="nope")


def test_caps_view_compacts_holders():
    band = FakeBand()
    view = roster.caps_view(band.workers)
    assert view["workers"] == 3  # distinct names
    assert view["caps"]["shell.exec"] == "*"
    assert view["caps"]["info.host"] == "*"  # every name holds it (one worker-c does)
    assert view["caps"]["camera.snap"] == ["worker-b"]
    assert roster.caps_view(band.workers, prefix="camera.") == {"workers": 3, "caps": {"camera.snap": ["worker-b"]}}
    one = roster.caps_view(band.workers, worker="worker-a")
    assert one == [{"worker": "worker-a", "worker_id": "a" * 32, "caps": ["caps.describe", "info.host", "shell.exec"]}]
    with pytest.raises(ValueError):
        roster.caps_view(band.workers, worker="nope")
    many = {str(i): {"worker_id": str(i), "name": f"w{i}", "caps": ["x.y"] + (["z.z"] if i else [])} for i in range(5)}
    assert roster.caps_view(many)["caps"]["z.z"] == {"all_but": ["w0"]}


@pytest.mark.asyncio
async def test_roster_tools_take_filters(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        rows = json.loads((await tool("rook_workers", cap_prefix="camera."))[0])
        assert [r["name"] for r in rows] == ["worker-b"]
        caps = json.loads((await tool("rook_caps", prefix="info."))[0])
        assert caps == {"workers": 3, "caps": {"info.host": "*"}}
        err = json.loads((await tool("rook_workers", fields="bogus"))[0])
        assert err["ok"] is False and "unknown fields" in err["error"]


# -- tool listing --------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_list_fits_the_budget(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch, knowledge=True) as env:
        rpc, _ = await env.connect()
        tools = (await rpc("tools/list", {}))["tools"]
        assert len(tools) >= 29
        size = len(json.dumps({"tools": tools}, separators=(",", ":"), ensure_ascii=False))
        assert size <= 12400, size  # +300 task note/batch/deck options and handoff close; +100 console_write literal/secrets
        by = {t["name"]: t for t in tools}
        assert all("outputSchema" not in t for t in tools)
        # Schema slimming keeps a property literally named "title" and required args.
        start = by["rook_chat_start"]["inputSchema"]
        assert start["required"] == ["title"] and start["properties"]["title"] == {"type": "string"}
        assert by["rook_call"]["inputSchema"]["properties"]["worker"] == {"type": "string"}
        assert by["rook_journal"]["inputSchema"]["properties"]["limit"] == {"default": 30, "type": "integer"}


@pytest.mark.asyncio
async def test_slimmed_schema_still_validates_like_before(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        # null for an optional arg and JSON-in-a-string still go through the real arg model
        text, _ = await tool("rook_call", cap="info.host", worker="worker-a", args=None, timeout=None)
        assert json.loads(text)["ok"]
        text, _ = await tool("rook_chat_start", title="t", invite='["agent:x"]')
        assert json.loads(text)["ok"]


def test_slim_schema_unit():
    schema = {"title": "fArguments", "type": "object", "required": ["title"], "properties": {
        "title": {"title": "Title", "type": "string"},
        "opt": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Opt"},
        "multi": {"anyOf": [{"items": {}, "type": "array"}, {"type": "string"}, {"type": "null"}], "default": None},
        "data": {"anyOf": [{"additionalProperties": True, "type": "object"}, {"type": "null"}], "default": None},
        "flag": {"default": False, "type": "boolean", "title": "Flag"},
        "limit": {"default": 30, "type": "integer"},
        "ratio": {"default": 0.0, "type": "number"}}}
    assert envelope._slim_schema(schema) == {"type": "object", "required": ["title"], "properties": {
        "title": {"type": "string"},
        "opt": {"type": "string"},
        "multi": {"anyOf": [{"type": "array"}, {"type": "string"}]},
        "data": {"type": "object"},
        "flag": {"type": "boolean"},
        "limit": {"default": 30, "type": "integer"},
        "ratio": {"default": 0.0, "type": "number"}}}


@pytest.mark.asyncio
async def test_indented_json_from_any_tool_is_compacted(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        text, _ = await tool("rook_whoami")
        assert "\n" not in text and json.loads(text)


# -- knowledge -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_knowledge_search_is_lean_over_mcp_but_not_for_the_web(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch, knowledge=True) as env:
        k = env.mcp._rook_knowledge
        human = {"id": "human:u1", "kind": "human", "label": "op"}
        for i in range(8):
            await k.dispatch("create", kind="knowledge", data={"title": f"Deploy note {i}", "body": "deploy " * 200},
                             request_id=f"k{i}", actor=human)
        _, tool = await env.connect()
        res = json.loads((await tool("rook_knowledge", action="search", query="deploy"))[0])["result"]
        assert len(res["results"]) == 5
        assert set(res["results"][0]) == {"id", "slug", "kind", "title", "state", "score", "excerpt"}
        assert len(res["results"][0]["excerpt"]) <= 240
        picked = json.loads((await tool("rook_knowledge", action="search", query="deploy",
                                        data={"limit": 2, "fields": "slug,title"}))[0])["result"]
        assert [set(r) for r in picked["results"]] == [{"slug", "title"}] * 2
        listed = json.loads((await tool("rook_knowledge", action="list"))[0])["result"]["records"]
        assert set(listed[0]) == {"id", "slug", "kind", "title", "state", "parent", "excerpt"}
        # The operator's web page keeps its shape: 20 rows with bodies.
        web = await k.dispatch("search", query="deploy", actor=human)
        assert len(web["results"]) == 8 and "body" in web["results"][0]


# -- existing clients ------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_agent_parses_compact_replies(tmp_path, monkeypatch):
    for mod in ("numpy", "faster_whisper", "kokoro_onnx"):
        if mod not in sys.modules:
            try:
                importlib.import_module(mod)
            except ImportError:
                stub = types.ModuleType(mod)
                stub.WhisperModel = stub.Kokoro = object
                monkeypatch.setitem(sys.modules, mod, stub)
    from services.voice import providers, rookmcp
    async with hub(tmp_path, monkeypatch) as env:
        Real = httpx.AsyncClient

        class Client(Real):
            def __init__(self, **kw):
                super().__init__(transport=httpx.ASGITransport(app=env.app), base_url="http://localhost", **kw)
        monkeypatch.setattr(rookmcp.httpx, "AsyncClient", Client)
        monkeypatch.setattr(rookmcp.RookMCP, "_sid", None)
        monkeypatch.setattr(rookmcp, "ROOK_MCP_TOKEN", STATIC)
        monkeypatch.setattr(rookmcp.RookMCP.__init__, "__defaults__",
                            ("http://localhost/mcp", STATIC, 30.0))
        said = await providers.tool_rook_devices({})
        assert said.startswith("4 devices on the band: worker-a (battery 70%)")
        assert "worker-b [stale]" not in said  # 80s old but still under the voice agent's 90s stale mark
        cap = sorted(providers.READ_CAPS)[0]
        env.band.workers["a" * 32]["caps"].append(cap)
        out = await providers.tool_rook_read({"cap": cap, "worker": "worker-a"})
        assert json.loads(out) == {"cap": cap}
