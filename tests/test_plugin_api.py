"""The unified plugin API: core registry contract, placement, facts, the plugin
host (hub + worker), the hub node served as worker "rook", generated MCP tools."""
from __future__ import annotations

import json
import sys
import textwrap
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rook.core import facts as facts_mod
from rook.core.facts import (NodeFacts, clean_facts, compile_placement,
                             evaluate_placement, roles_from_grants, wire_facts)
from rook.core.host import PluginHost
from rook.core.plugin import (DEFAULT_PLACEMENT, Plugin, capability, core_api_compatible,
                              parse_resource, place, resource, setting, valid_version)
from rook.core.registry import CapabilityRegistry

HUB = NodeFacts(node_id="h", name="rook", roles=frozenset({"is_hub"}), hw={"os": "linux"})
WORKER = NodeFacts(node_id="w", name="worker-a",
                   hw={"os": "linux", "pty": True, "camera": True,
                       "gpu": [{"vendor": "nvidia", "vram_gb": 12.0}]})
PHONE = NodeFacts(node_id="p", name="worker-b", hw={"os": "android", "embedded": True})


# -- capability metadata + registry contract ---------------------------------

class Rows(Plugin):
    NAMESPACE = "rows"

    @capability("legacy")
    def legacy(self, fields=None):
        return {"fields": fields}

    @capability("list", risk="read", limit=3, fields=["id", "name"])
    def list_(self) -> list:
        return [{"id": i, "name": f"n{i}", "secret": "x"} for i in range(10)]

    @capability("paged", risk="read", limit=5)
    def paged(self, limit: int = 100) -> dict:
        return {"got": limit}

    @capability("one", tier="write", tags=("destructive",), fields="*")
    def one(self) -> dict:
        return {"a": 1, "b": 2, "items": [{"a": 1, "b": 2}]}


def _registry(plugin: Plugin) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for k, fn in plugin.caps().items():
        reg.register(k, fn)
    return reg


def test_bare_capability_is_unchanged():
    reg = _registry(Rows())
    assert reg.meta("rows.legacy") is None
    assert "risk" not in reg.describe()["rows.legacy"]


@pytest.mark.asyncio
async def test_core_enforces_limit_and_fields():
    reg = _registry(Rows())
    # Legacy caps keep receiving `fields` as an ordinary argument.
    assert await reg.call("rows.legacy", fields=["x"]) == {"fields": ["x"]}
    # Default limit trims, default projection applies.
    assert await reg.call("rows.list") == [{"id": 0, "name": "n0"}, {"id": 1, "name": "n1"},
                                           {"id": 2, "name": "n2"}]
    got = await reg.call("rows.list", limit=2, fields="id,secret")
    assert got == [{"id": 0, "secret": "x"}, {"id": 1, "secret": "x"}]
    assert len((await reg.call("rows.list", fields="*"))[0]) == 3
    # A handler that takes `limit` gets the default injected, or the caller's.
    assert await reg.call("rows.paged") == {"got": 5}
    assert await reg.call("rows.paged", limit=7) == {"got": 7}
    # fields="*" default returns everything; projection reaches into items.
    assert await reg.call("rows.one") == {"a": 1, "b": 2, "items": [{"a": 1, "b": 2}]}
    # A dict with an items list is an envelope: projection applies to the items.
    assert await reg.call("rows.one", fields=["a"]) == {"a": 1, "b": 2, "items": [{"a": 1}]}
    d = reg.describe()
    assert d["rows.list"]["risk"] == "read" and d["rows.list"]["limit"] == 3
    assert d["rows.one"]["risk"] == "write" and d["rows.one"]["tags"] == ["destructive"]


def test_capability_validates_metadata():
    with pytest.raises(ValueError):
        capability("x", risk="root")
    with pytest.raises(ValueError):
        capability("x", limit=0)
    with pytest.raises(ValueError):
        capability("x", risk="read", tier="exec")


# -- placement + facts ---------------------------------------------------------

@pytest.mark.parametrize("expr,hub,worker,phone", [
    ("is_hub", True, False, False),
    ("not is_hub", False, True, True),
    ("has('camera')", False, True, False),
    ("has('gpu', vram_gb >= 8)", False, True, False),
    ("has('gpu', vram_gb >= 16)", False, False, False),
    ("has('gpu', vendor='nvidia')", False, True, False),
    ("is_embedded or os == 'android'", False, False, True),
    ("has('pty') and os in ('linux', 'darwin')", False, True, False),
    ("any", True, True, True),
])
def test_placement_expressions(expr, hub, worker, phone):
    assert [evaluate_placement(expr, f) for f in (HUB, WORKER, PHONE)] == [hub, worker, phone]


@pytest.mark.parametrize("bad", ["__import__('os')", "os.system", "open('x')",
                                 "has(camera)", "[x for x in y]", "lambda: 1", ""])
def test_placement_rejects_anything_but_the_grammar(bad):
    with pytest.raises(ValueError):
        compile_placement(bad)
    assert evaluate_placement(bad, HUB) is False


def test_place_validates_at_declaration():
    with pytest.raises(ValueError):
        place("is_hub and (", run="all")
    with pytest.raises(ValueError):
        place("is_hub", run="some")
    assert evaluate_placement(lambda n: n.name == "worker-a", WORKER)


def test_default_placement_is_workers_only():
    assert evaluate_placement(DEFAULT_PLACEMENT.where, WORKER)
    assert not evaluate_placement(DEFAULT_PLACEMENT.where, HUB)


def test_self_reported_facts_can_never_be_roles(monkeypatch):
    announce = {"worker_id": "w1", "name": "x",
                "facts": {"is_hub": True, "roles": ["is_hub"], "grants": [1], "camera": True},
                "grants": [{"role": "is_hub", "sig": "forged"}]}
    nf = NodeFacts.from_announce(announce)
    assert not nf.is_hub and nf.roles == frozenset()
    assert nf.hw == {"camera": True}
    assert not evaluate_placement("is_hub", nf)
    monkeypatch.setenv("ROOK_NODE_FACTS", json.dumps({"is_hub": True, "camera": True}))
    detected = facts_mod.detect_facts()
    assert "is_hub" not in detected and detected["camera"] is True


def test_build_167_announce_has_empty_facts():
    nf = NodeFacts.from_announce({"worker_id": "w1", "name": "old", "caps": ["shell.exec"]})
    assert nf.hw == {} and nf.roles == frozenset()


def test_role_verifier_hook():
    grant = {"role": "is_hub", "sub": {"worker_id": "w1"}}
    assert roles_from_grants([grant], "w1") == frozenset()      # stub rejects everything
    try:
        facts_mod.set_role_verifier(
            lambda g, node: g["role"] if g["sub"]["worker_id"] == node else None)
        assert roles_from_grants([grant], "w1") == frozenset({"is_hub"})
        assert roles_from_grants([grant], "w2") == frozenset()
        facts_mod.set_role_verifier(lambda g, node: 1 / 0)       # a crash rejects
        assert roles_from_grants([grant], "w1") == frozenset()
    finally:
        facts_mod.set_role_verifier(None)


def test_wire_facts_stay_small():
    big = {"os": "linux", "camera": False, "pty": True,
           "gpu": [{"vendor": "nvidia", "name": "x" * 40, "vram_gb": 24}] * 8, "note": "y" * 900}
    out = wire_facts(big)
    assert "camera" not in out and out["pty"] is True
    assert len(json.dumps(out)) <= 1024


def test_clean_facts_tolerates_garbage():
    assert clean_facts(None) == {} and clean_facts([1]) == {}


# -- versions, settings, resources -------------------------------------------

def test_core_api_ranges():
    assert core_api_compatible(">=1.0,<2", "1.0")
    assert core_api_compatible("1", "1.4")
    assert not core_api_compatible("1", "2.0")
    assert not core_api_compatible(">=1.1", "1.0")
    assert not core_api_compatible("garbage", "1.0")
    assert valid_version("167.snappy.quail") and not valid_version("1.0")


class Configured(Plugin):
    NAMESPACE = "cfg"
    SETTINGS = (
        setting("interval", int, default=30, env="ROOK_TEST_INTERVAL", scope="worker"),
        setting("token", str, secret=True, label="API token"),
        setting("mode", str, default="a", choices=("a", "b")),
        resource("embedder", default="cap://any/embed.text"),
    )


def test_settings_resolution(monkeypatch):
    p = Configured()
    from rook.core.plugin import SettingsView
    view = SettingsView(p, stored={"interval": 45, "mode": "b"},
                        secrets=lambda key: "s3cret" if key == "plugin.cfg.token" else None)
    assert view["interval"] == 45 and view.source("interval") == "stored"
    monkeypatch.setenv("ROOK_TEST_INTERVAL", "60")
    assert view["interval"] == 60 and view.source("interval") == "env"
    monkeypatch.setenv("ROOK_TEST_INTERVAL", "not-a-number")
    assert view["interval"] == 45            # invalid env falls through
    assert view["token"] == "s3cret" and view.as_dict()["token"] == "***"
    assert view["mode"] == "b"
    schema = {s["name"]: s for s in view.schema()}
    assert schema["token"]["secret"] and "default" not in schema["token"]
    assert schema["interval"]["env"] == "ROOK_TEST_INTERVAL"
    with pytest.raises(ValueError):
        setting("bad", int, default="x")
    with pytest.raises(ValueError):
        setting("bad", str, scope="galaxy")


@pytest.mark.asyncio
async def test_resources():
    r = parse_resource("cap://any/embed.text")
    assert (r.scheme, r.target, r.path) == ("cap", "any", "embed.text")
    assert parse_resource("sqlite:///var/lib/x.db").path == "/var/lib/x.db"
    with pytest.raises(ValueError):
        parse_resource("ftp://example.com/x")
    with pytest.raises(ValueError):
        parse_resource("cap://any/")
    calls = []

    async def caller(cap, args, target, timeout):
        calls.append((cap, args, target))
        return {"vec": [1]}

    host = PluginHost(facts=WORKER, cap_caller=caller, check_placement=False)
    host.load([_cand("cfg", Configured)])
    res = host.plugins[0].resource("embedder")
    assert await res.call({"text": "hi"}) == {"vec": [1]}
    assert calls == [("embed.text", {"text": "hi"}, "any")]


# -- the plugin host -------------------------------------------------------------

def _cand(name, obj):
    from rook.core.plugin import Candidate
    return Candidate(name, f"test:{name}", lambda: obj)


class HubOnly(Plugin):
    NAMESPACE = "hubonly"
    PLACEMENT = place("is_hub")

    @capability("x", risk="read")
    def x(self):
        return 1


class OnePerBand(Plugin):
    NAMESPACE = "single"
    PLACEMENT = place("any", run="one")


class FutureApi(Plugin):
    NAMESPACE = "future"
    CORE_API = ">=2.0"


class Unavailable(Plugin):
    NAMESPACE = "gone"

    def available(self):
        return False


class Exploding(Plugin):
    NAMESPACE = "boom"

    def available(self):
        raise RuntimeError("no")


class Clash(Plugin):
    NAMESPACE = "rows"   # collides with Rows' caps

    @capability("list")
    def other(self):
        return None

    @capability("unique")
    def unique(self):
        return None


def _broken():
    raise ImportError("missing dependency")


def test_host_isolates_failures_and_applies_placement():
    from rook.core.plugin import Candidate
    host = PluginHost(facts=WORKER, build_version="167.snappy.quail")
    loaded = host.load([
        _cand("rows", Rows), _cand("hubonly", HubOnly), _cand("single", OnePerBand),
        _cand("future", FutureApi), _cand("gone", Unavailable), _cand("boom", Exploding),
        _cand("clash", Clash), Candidate("broken", "test", _broken),
        _cand("none", None), _cand("off", Rows),
    ], disabled={"off"})
    assert [p.NAMESPACE for p in loaded] == ["rows"]
    st = {k: v["state"] for k, v in host.status.items()}
    assert st == {"rows": "loaded", "hubonly": "not_placed", "single": "not_placed",
                  "future": "failed", "gone": "unavailable", "boom": "failed",
                  "clash": "failed", "broken": "failed", "none": "skipped",
                  "off": "disabled"}
    # A clashing plugin leaves no half-registered caps behind.
    assert not host.registry.has("rows.unique")
    assert host.plugins[0].VERSION == "167.snappy.quail"
    assert host.tiers() == {"rows.list": "r", "rows.one": "w", "rows.paged": "r"}


def test_hub_host_loads_hub_placed_and_elected():
    host = PluginHost(facts=HUB, elect_one=lambda p: True)
    host.load([_cand("rows", Rows), _cand("hubonly", HubOnly), _cand("single", OnePerBand)])
    assert sorted(p.NAMESPACE for p in host.plugins) == ["hubonly", "single"]


@pytest.mark.asyncio
async def test_host_dispatch_shapes_and_identity():
    from rook.core import context

    class Who(Plugin):
        NAMESPACE = "who"

        @capability("", risk="read")
        async def who(self):
            return context.current_identity()

        @capability("fail")
        def fail(self):
            raise RuntimeError("bad")

    host = PluginHost(facts=WORKER)
    host.load([_cand("who", Who)])
    assert await host.dispatch("who", {}, "agent:x") == {"ok": True, "result": "agent:x"}
    assert (await host.dispatch("who.fail", {}))["error"] == "RuntimeError: bad"
    assert (await host.dispatch("who", {"nope": 1}))["error"].startswith("bad args")
    assert (await host.dispatch("who", [1]))["error"] == "args must be an object"
    assert (await host.dispatch("missing", {}))["error"] == "unknown capability: missing"


def test_package_and_entry_point_discovery(tmp_path, monkeypatch):
    pkg = f"rook_test_plugins_{uuid.uuid4().hex[:8]}"
    d = tmp_path / pkg
    d.mkdir()
    (d / "__init__.py").write_text("")
    (d / "_private.py").write_text("raise SystemExit('never imported')\n")
    (d / "clock.py").write_text(textwrap.dedent("""
        from rook.worker.plugin import Plugin, capability
        class Clock(Plugin):
            NAMESPACE = "clock"
            @capability("now")
            def now(self):
                return 1
        PLUGIN = Clock
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    ep = SimpleNamespace(name="ext", load=lambda: HubOnly)

    class EPs(list):
        def select(self, group):
            return self if group == "rook.plugins" else EPs()

    monkeypatch.setattr("importlib.metadata.entry_points", lambda: EPs([ep]))
    cands = PluginHost.discover(pkg)
    assert [c.module for c in cands] == ["clock", "ext"]
    host = PluginHost(facts=HUB, elect_one=lambda p: True)
    host.load(cands)
    assert host.status["clock"]["state"] == "not_placed"     # worker default placement
    assert host.status["ext"]["state"] == "loaded"
    assert host.manifests()[0]["source"] == "entry_point:ext"
    sys.modules.pop(f"{pkg}.clock", None)


# -- existing worker plugins load unchanged ----------------------------------

def test_every_builtin_worker_plugin_loads_like_before():
    """The worker host loads exactly what the legacy package-scan loader did
    (no placement), and no built-in worker plugin would land on the hub."""
    from rook.core.plugin import load_plugins
    legacy = CapabilityRegistry()
    legacy_plugins = load_plugins("rook.worker.plugins", legacy)
    host = PluginHost(facts=NodeFacts(node_id="w", hw=facts_mod.local_facts()))
    host.load(PluginHost.discover("rook.worker.plugins", entry_points=False))
    assert sorted(p._module for p in host.plugins) == sorted(p._module for p in legacy_plugins)
    assert host.registry.list() == legacy.list()
    hub = PluginHost(facts=HUB, elect_one=lambda p: True)
    hub.load(PluginHost.discover("rook.worker.plugins", entry_points=False))
    assert hub.plugins == []


def test_worker_announce_carries_facts_and_old_keys(tmp_path, monkeypatch):
    from rook.worker import core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    w = core.Worker(SimpleNamespace(send=AsyncMock()), enabled=["info"], name="worker-a")
    assert w.plugins is w.host.plugins
    import asyncio
    asyncio.run(w.announce())
    msg = json.loads(w.transport.send.call_args.args[0])
    for key in ("kind", "worker_id", "name", "caps", "plugins", "version", "build"):
        assert key in msg
    assert isinstance(msg["facts"], dict) and "os" in msg["facts"]
    assert "caps.describe" in msg["caps"]


# -- the hub node + band client ------------------------------------------------

def _node(tmp_path, **kw):
    from rook.hub.node import HubNode
    return HubNode(str(tmp_path), entry_points=False, **kw)


def test_hub_node_hosts_hub_plugins_only(tmp_path):
    node = _node(tmp_path)
    assert node.name == "rook" and node.facts.is_hub
    assert {"caps.describe", "hub.info", "hub.plugins"} <= set(node.caps())
    assert not any(c.startswith("shell.") for c in node.caps())
    msg = node.announce_msg()
    assert msg["name"] == "rook" and msg["tiers"]["hub.info"] == "r"
    # The node id survives restarts.
    assert _node(tmp_path).worker_id == node.worker_id


@pytest.mark.asyncio
async def test_hub_info_and_plugins_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_HUB_MOTD", "maintenance at noon")
    node = _node(tmp_path)
    info = (await node.dispatch("hub.info", {}))["result"]
    assert info["roles"] == ["is_hub"] and info["motd"] == "maintenance at noon"
    hub_info = [p for p in info["plugins"] if p["namespace"] == "hub"][0]
    assert {"name": "hub-info", "namespace": "hub"}.items() <= hub_info.items()
    compact = (await node.dispatch("hub.plugins", {}))["result"]
    assert set(compact[0]) == {"name", "namespace", "version", "state", "caps"}
    full = (await node.dispatch("hub.plugins", {"fields": "*"}))["result"]
    full_hub = [p for p in full if p["namespace"] == "hub"][0]
    assert full_hub["placement"] == {"where": "is_hub", "run": "one"}
    assert full_hub["settings"][0]["env"] == "ROOK_HUB_MOTD"


@pytest.mark.asyncio
async def test_band_calls_to_the_hub_are_read_only_and_journaled(tmp_path):
    pkg = f"rook_test_hubplugins_{uuid.uuid4().hex[:8]}"
    d = tmp_path / pkg
    d.mkdir()
    (d / "__init__.py").write_text("")
    (d / "danger.py").write_text(textwrap.dedent("""
        from rook.core.plugin import Plugin, capability, place
        class Danger(Plugin):
            NAMESPACE = "danger"
            PLACEMENT = place("is_hub")
            @capability("run", risk="exec")
            def run(self):
                return "ran"
            @capability("peek", risk="read")
            def peek(self):
                return "peeked"
        PLUGIN = Danger
    """))
    sys.path.insert(0, str(tmp_path))
    try:
        seen = []
        node = _node(tmp_path, package=pkg, on_band_call=lambda *a: seen.append(a))
        assert (await node.dispatch("danger.peek", {}, "band:x", source="band"))["ok"]
        denied = await node.dispatch("danger.run", {}, "band:x", source="band")
        assert not denied["ok"] and "not callable over the band" in denied["error"]
        assert (await node.dispatch("danger.run", {}, "agent:y", source="local"))["result"] == "ran"
        assert [s[0] for s in seen] == ["danger.peek", "danger.run"]
        raised = _node(tmp_path, package=pkg, band_max_risk="exec")
        assert (await raised.dispatch("danger.run", {}, None, source="band"))["ok"]
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_band_client_serves_the_hub_node(tmp_path):
    from rook.band_mcp.client import BandClient
    client = BandClient("test-band")
    client.transport.send = AsyncMock()
    node = _node(tmp_path, client=client)
    client.attach_local(node)
    assert client.workers[node.worker_id]["name"] == "rook"

    # MCP-side call short-circuits in process (never hits the band).
    reply = await client.call("hub.info", target=node.worker_id, identity="agent:a")
    assert reply["ok"] and reply["from"] == node.worker_id
    client.transport.send.assert_not_called()

    # A band request addressed to the hub node is answered on the band.
    await client._on_message(json.dumps({"id": "m1", "cap": "hub.info", "args": {},
                                         "target": node.worker_id}).encode(), ("peer",))
    import asyncio
    await asyncio.gather(*client._local_calls)
    sent = json.loads(client.transport.send.call_args.args[0])
    assert sent["id"] == "m1" and sent["ok"] and sent["from"] == node.worker_id

    # Requests for other workers, and requests for caps it lacks, are ignored.
    client.transport.send.reset_mock()
    await client._on_message(json.dumps({"id": "m2", "cap": "hub.info",
                                         "target": "someone-else"}).encode(), ("peer",))
    await client._on_message(json.dumps({"id": "m3", "cap": "shell.exec"}).encode(), ("peer",))
    await asyncio.gather(*client._local_calls)
    client.transport.send.assert_not_called()

    # Own announce echoed back is ignored; an impostor "rook" is renamed.
    client._handle_announce(node.announce_msg())
    client._handle_announce({"kind": "announce", "worker_id": "abcdef1234567890",
                             "name": "Rook", "caps": ["shell.exec"],
                             "facts": {"camera": True, "is_hub": True}})
    assert client.workers[node.worker_id]["local"] is True
    impostor = client.workers["abcdef1234567890"]
    assert impostor["name"] == "Rook~abcdef12" and impostor["facts"] == {"camera": True}

    # Announce loop sends the node's announce; gc never evicts it.
    await client._announce_local()
    assert json.loads(client.transport.send.call_args.args[0])["name"] == "rook"
    await client.stop()


def test_band_client_without_hub_node_quarantines_ungranted_rook():
    # Without a valid is_hub grant (and proof of key possession) nobody gets
    # the reserved name, even on a client with no hub node of its own.
    from rook.band_mcp.client import BandClient
    client = BandClient("test-band")
    client._handle_announce({"kind": "announce", "worker_id": "w1", "name": "rook",
                             "caps": []})
    w = client.workers["w1"]
    assert w["name"] == "rook~w1" and w["quarantined"] and w["facts"] == {}


@pytest.mark.asyncio
async def test_multiband_client_routes_to_hub_node(tmp_path):
    from rook.band_mcp.client import MultiBandClient
    client = MultiBandClient([])
    node = _node(tmp_path, client=client)
    client.attach_local(node)
    assert client.workers[node.worker_id]["band"] == "*"
    reply = await client.call("hub.info", target=node.worker_id)
    assert reply["ok"] and reply["result"]["name"] == "rook"


# -- MCP bridge end to end -------------------------------------------------------

def _text(result):
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


class Notes(Plugin):
    """A hub plugin with a dedicated tool, injected as an entry point."""
    NAMESPACE = "notes"
    PLACEMENT = place("is_hub")

    @capability("search", risk="read", limit=2, fields="*", tool=True,
                description="Search notes.")
    def search(self, q: str, exact: bool = False) -> list:
        """Long developer docstring that must not reach tools/list."""
        return [{"q": q, "exact": exact, "n": i} for i in range(5)]


def _entry_points(monkeypatch, *plugins):
    class EPs(list):
        def select(self, group):
            return self if group == "rook.plugins" else EPs()
    eps = EPs(SimpleNamespace(name=p.NAMESPACE, load=lambda p=p: p) for p in plugins)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda: eps)


async def _tools(mcp):
    return {t.name: t for t in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_mcp_reaches_hub_caps(tmp_path, monkeypatch):
    from rook.band_mcp.client import BandClient
    from rook.band_mcp.server import build_server
    _entry_points(monkeypatch)
    mcp, _ = build_server(BandClient("test-band"), public_url="https://mcp.example.com",
                          persist_path=str(tmp_path / "tokens.json"))
    # The built-in hub plugin adds no tool: tools/list is unchanged.
    assert not [n for n in await _tools(mcp) if n.startswith("rook_hub")]

    via_call = _text(await mcp.call_tool("rook_call", {"cap": "hub.info", "worker": "rook"}))
    assert via_call["ok"] and via_call["result"]["name"] == "rook"
    assert via_call["result"]["roles"] == ["is_hub"]

    plugins = _text(await mcp.call_tool("rook_call", {"cap": "hub.plugins", "worker": "rook",
                                                      "args": {"fields": ["name"]}}))
    assert plugins["result"] == [{"name": "decide"}, {"name": "hub-info"}, {"name": "notify"},
                                 {"name": "persona"},
                                 {"name": "hub-policy"}, {"name": "chat-rooms"},
                                 {"name": "settings"}]

    workers = _text(await mcp.call_tool("rook_workers", {}))
    assert [w["name"] for w in workers] == ["rook"]
    caps = _text(await mcp.call_tool("rook_caps", {"prefix": "hub."}))
    assert set(caps["caps"]) == {"hub.info", "hub.plugins"}


@pytest.mark.asyncio
async def test_mcp_tool_generated_from_a_hub_cap(tmp_path, monkeypatch):
    from rook.band_mcp.client import BandClient
    from rook.band_mcp.server import build_server
    _entry_points(monkeypatch, Notes)
    mcp, _ = build_server(BandClient("test-band"), public_url="https://mcp.example.com",
                          persist_path=str(tmp_path / "tokens.json"))
    tool = (await _tools(mcp))["rook_notes_search"]
    assert tool.description == "Search notes."
    schema = tool.inputSchema
    assert schema["required"] == ["q"]
    assert set(schema["properties"]) == {"q", "exact", "limit", "fields"}
    got = _text(await mcp.call_tool("rook_notes_search", {"q": "x"}))
    assert got["ok"] and got["result"] == [{"q": "x", "exact": False, "n": 0},
                                           {"q": "x", "exact": False, "n": 1}]
    got = _text(await mcp.call_tool("rook_notes_search", {"q": "y", "limit": 1,
                                                           "fields": ["n"]}))
    assert got["result"] == [{"n": 0}]
    # Same reply as rook_call on worker "rook".
    same = _text(await mcp.call_tool("rook_call", {"cap": "notes.search", "worker": "rook",
                                                   "args": {"q": "y", "limit": 1, "fields": ["n"]}}))
    assert same["result"] == got["result"]


@pytest.mark.asyncio
async def test_generated_tools_never_shadow_existing_ones(tmp_path, monkeypatch):
    from rook.band_mcp.client import BandClient
    from rook.band_mcp.server import build_server

    class Shadow(Plugin):
        NAMESPACE = "rook"   # would generate rook_call from cap "rook.call"
        PLACEMENT = place("is_hub")

        @capability("call", risk="read", tool=True)
        def call(self):
            """Not the real rook_call."""
            return 1

    _entry_points(monkeypatch, Shadow)
    mcp, _ = build_server(BandClient("test-band"), public_url="https://mcp.example.com",
                          persist_path=str(tmp_path / "tokens.json"))
    assert (await _tools(mcp))["rook_call"].description.startswith("Run a cap on one worker")


@pytest.mark.asyncio
async def test_hub_plugins_can_be_disabled(tmp_path, monkeypatch):
    from rook.band_mcp.client import BandClient
    from rook.band_mcp.server import build_server
    monkeypatch.setenv("ROOK_HUB_PLUGINS", "0")
    mcp, _ = build_server(BandClient("test-band"), public_url="https://mcp.example.com",
                          persist_path=str(tmp_path / "tokens.json"))
    assert mcp._rook_hub is None
    roster = _text(await mcp.call_tool("rook_workers", {}))
    assert roster == []


def test_generated_tool_signature_mirrors_the_cap():
    import inspect
    from rook.hub.mcp_tools import _signature, tool_name

    class Sig(Plugin):
        NAMESPACE = "sig"

        @capability("q", risk="read", limit=10, fields="*", tool=True)
        def q(self, query: str, depth: int = 2) -> list:
            """Search things."""
            return []

    fn = Sig().caps()["sig.q"]
    sig = _signature(fn, fn._rook_cap_meta)
    assert list(sig.parameters) == ["query", "depth", "limit", "fields"]
    assert sig.parameters["query"].default is inspect.Parameter.empty
    assert sig.parameters["depth"].annotation is int
    assert tool_name("sig.q") == "rook_sig_q"


# -- migrations + packaging ----------------------------------------------------

def test_plugin_migrations_apply_once_in_order(tmp_path):
    import sqlite3
    from rook.core import migrations
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_init.sql").write_text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\n")
    (mig / "002_seed.sql").write_text("INSERT INTO notes(body) VALUES ('a;b');\n"
                                      "INSERT INTO notes(body) VALUES ('c');\n")
    (mig / "README.txt").write_text("ignored")
    conn = sqlite3.connect(tmp_path / "p.db")
    assert migrations.apply(conn, mig, "notes") == [1, 2]
    assert migrations.apply(conn, mig, "notes") == []
    (mig / "003_bad.sql").write_text("INSERT INTO notes(body) VALUES ('d');\nNOT SQL;\n")
    with pytest.raises(sqlite3.Error):
        migrations.apply(conn, mig, "notes")
    # The failed file rolled back entirely and is not recorded.
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    assert [v for v, _ in migrations.pending(conn, mig, "notes")] == [3]


def test_core_is_stdlib_only_and_bundled():
    """rook.core ships inside the worker bundle and the Android app, so it may
    import only the standard library and itself."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f in (root / "rook" / "core").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            else:
                continue
            for m in mods:
                assert m.split(".")[0] in sys.stdlib_module_names, f"{f.name} imports {m}"
    for script in ("rook/remote/build_band_worker.py", "android/stage_worker.py"):
        assert '"core"' in (root / script).read_text(), script


def test_plugin_data_dir_and_migrate(tmp_path):
    import sqlite3

    class Notes(Plugin):
        NAMESPACE = "notes"
        MIGRATIONS = "migrations"

    host = PluginHost(facts=WORKER, data_root=str(tmp_path / "plugins"))
    host.load([_cand("notes", Notes)])
    p = host.plugins[0]
    assert p.data_dir == tmp_path / "plugins" / "notes" and p.data_dir.is_dir()
    # No migrations dir next to this test module: nothing to apply.
    assert p.migrate(sqlite3.connect(p.data_dir / "n.db")) == []
    assert host.manifests()[0]["migrations"] == "migrations"


def test_caps_roster_does_not_count_the_hub_node():
    from rook.band_mcp import roster
    workers = {str(i): {"worker_id": str(i), "name": f"worker-{i}",
                        "caps": ["caps.describe", "shell.exec"]} for i in range(4)}
    workers["h"] = {"worker_id": "h", "name": "rook", "local": True,
                    "caps": ["caps.describe", "hub.info"]}
    view = roster.caps_view(workers)
    assert view["workers"] == 4
    assert view["caps"] == {"caps.describe": "*", "shell.exec": "*", "hub.info": ["rook"]}
