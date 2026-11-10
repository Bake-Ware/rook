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

    @capability("legacy", scope="self")
    def legacy(self, fields=None):
        return {"fields": fields}

    @capability("list", risk="read", limit=3, fields=["id", "name"], scope="self")
    def list_(self) -> list:
        return [{"id": i, "name": f"n{i}", "secret": "x"} for i in range(10)]

    @capability("paged", risk="read", limit=5, scope="self")
    def paged(self, limit: int = 100) -> dict:
        return {"got": limit}

    @capability("one", risk="write", tags=("destructive",), fields="*", scope="self")
    def one(self) -> dict:
        return {"a": 1, "b": 2, "items": [{"a": 1, "b": 2}]}


def _registry(plugin: Plugin) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for k, fn in plugin.caps().items():
        reg.register(k, fn)
    return reg


def test_a_cap_declaring_only_scope_keeps_legacy_behaviour():
    reg = _registry(Rows())
    meta = reg.meta("rows.legacy")
    assert meta is not None and meta.risk is None and meta.scope == "self"
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


def test_host_id_is_stable_hashed_and_wire_safe():
    a = facts_mod._host_id()
    assert a is None or (isinstance(a, str) and len(a) == 16 and a == facts_mod._host_id())
    facts = facts_mod.detect_facts()
    if a is not None:
        # The raw machine id never rides the band; a 16-hex digest does.
        assert facts["host_id"] == a and len(wire_facts(facts)["host_id"]) == 16


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

    @capability("x", risk="read", scope="self")
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

    @capability("list", scope="self")
    def other(self):
        return None

    @capability("unique", scope="self")
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

        @capability("", risk="read", scope="self")
        async def who(self):
            return context.current_identity()

        @capability("fail", scope="self")
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
            @capability("now", scope="self")
            def now(self):
                return 1
        PLUGIN = Clock
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    ep = SimpleNamespace(name="ext", group="rook.plugins", load=lambda: HubOnly)
    other = SimpleNamespace(name="x", group="console_scripts", load=lambda: None)
    dist = SimpleNamespace(metadata={"Name": "ext-dist"}, entry_points=[ep, other])
    monkeypatch.setattr("importlib.metadata.distributions", lambda path=None: [dist])
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
    legacy_host = PluginHost(check_placement=False)
    legacy = legacy_host.registry
    legacy_plugins = legacy_host.load(PluginHost.discover("rook.worker.plugins",
                                                          entry_points=False))
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


# -- MCP bridge end to end -------------------------------------------------------

def _text(result):
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


class Notes(Plugin):
    """A hub plugin with a dedicated tool, injected as an entry point."""
    NAMESPACE = "notes"
    PLACEMENT = place("is_hub")

    @capability("search", risk="read", limit=2, fields="*", tool=True,
                description="Search notes.", scope="self")
    def search(self, q: str, exact: bool = False) -> list:
        """Long developer docstring that must not reach tools/list."""
        return [{"q": q, "exact": exact, "n": i} for i in range(5)]


def _entry_points(monkeypatch, *plugins):
    dists = [SimpleNamespace(metadata={"Name": "dist-" + p.NAMESPACE}, entry_points=[
        SimpleNamespace(name=p.NAMESPACE, group="rook.plugins", load=lambda p=p: p)]) for p in plugins]
    monkeypatch.setattr("importlib.metadata.distributions", lambda path=None: dists)


async def _tools(mcp):
    return {t.name: t for t in await mcp.list_tools()}


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


def test_listing_caps_has_no_side_effects(tmp_path, monkeypatch):
    """caps() must not touch properties: data_dir creates a directory, and a
    plugin attached outside a host (Android native plugins) has no data root,
    so it would mkdir relative to a possibly read-only cwd."""
    from rook.core.plugin import Plugin, capability

    class P(Plugin):
        NAMESPACE = "probe"

        @capability("ping", scope="self")
        async def ping(self):
            return "pong"

    monkeypatch.chdir(tmp_path)
    assert set(P().caps()) == {"probe.ping"}
    assert not (tmp_path / ".rook-plugin-data").exists()
