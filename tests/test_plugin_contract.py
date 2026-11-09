"""The plugin contract across every built-in plugin: lazy optional imports,
declared risk tiers, docstrings, timeouts (pluginhost audit)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A sitecustomize that makes aiohttp and PyNaCl unimportable, as on a light
# worker or a hub installed without the optional extras.
_BLOCK = textwrap.dedent("""
    import sys, importlib.abc
    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name.split(".")[0] in ("aiohttp", "nacl"):
                raise ImportError("blocked for the test: " + name)
    sys.meta_path.insert(0, _Block())
""")


def _run_blocked(tmp_path, code: str) -> subprocess.CompletedProcess:
    (tmp_path / "sitecustomize.py").write_text(_BLOCK, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(ROOT)]))
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)


# -- every built-in cap: declared risk, a docstring ---------------------------

ANDROID_PY = ROOT / "android" / "app" / "src" / "main" / "python"


def _android_stubs(monkeypatch):
    """Import rook_android.plugins off-device: Chaquopy's ``java`` absent, the
    app-context helper inert. Mirrors tools/gen_skill_reference.py."""
    import types
    monkeypatch.syspath_prepend(str(ANDROID_PY))
    monkeypatch.setitem(sys.modules, "java", None)
    monkeypatch.setitem(sys.modules, "rook_android.androidctx", types.SimpleNamespace(
        app_context=lambda: None, jclass=None, cast=None, has_permission=lambda p: False))
    for k in [k for k in sys.modules if k == "rook_android" or k.startswith("rook_android.")
              and k != "rook_android.androidctx"]:
        monkeypatch.delitem(sys.modules, k)


def _plugin_caps(packages):
    """[(cap, fn, module)] for every Plugin and Device class under ``packages``."""
    import importlib
    import inspect
    import pkgutil
    from rook.core.devices import Device
    from rook.core.plugin import Plugin
    out = []
    for package in packages:
        pkg = importlib.import_module(package)
        for info in pkgutil.walk_packages(pkg.__path__, package + "."):
            mod = importlib.import_module(info.name)
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if (issubclass(cls, (Plugin, Device)) and cls not in (Plugin, Device)
                        and cls.NAMESPACE and cls.__module__ == mod.__name__):
                    out += [(cap, fn, info.name) for cap, fn in cls.declared_caps().items()]
    return out


def _core_caps():
    """Caps the worker and hub cores register themselves (not plugins)."""
    import tempfile
    from types import SimpleNamespace
    from rook.hub.node import HubNode
    from rook.worker import core
    with tempfile.TemporaryDirectory() as tmp:
        node = HubNode(tmp, entry_points=False, package=None)
        w = core.Worker(SimpleNamespace(send=None), enabled=[], name="w")
        return {c: node.host.registry.handler(c) for c in node.caps()} | \
            {c: w.registry.handler(c) for c in w.registry.list()}


# -- runtime enable / disable / reload ---------------------------------------

import asyncio  # noqa: E402

import pytest  # noqa: E402

from rook.core.facts import NodeFacts  # noqa: E402
from rook.core.host import PluginHost  # noqa: E402
from rook.core.plugin import Candidate, Plugin, capability, place, setting  # noqa: E402

EVENTS: list = []


class Bound(Plugin):
    """Needs its side hook before start(), like work/sessions/config."""
    NAMESPACE = "bound"
    SETTINGS = (setting("greeting", str, default="hi", apply="reload"),
                setting("enabled", bool, default=True))

    def __init__(self):
        super().__init__()
        self.runtime = None
        self.greeting = None

    def available(self):
        return bool(self.settings["enabled"])

    def bind_node(self, node):
        self.runtime = node

    bind_worker = bind_node   # what a worker's binder calls

    async def start(self):
        assert self.runtime is not None, "started before it was bound"
        self.greeting = self.settings["greeting"]
        EVENTS.append(("start", id(self), self.greeting))

    async def stop(self):
        EVENTS.append(("stop", id(self)))

    @capability("hello", risk="read", scope="self")
    def hello(self):
        """Say the configured greeting."""
        return self.greeting


class Needs(Plugin):
    NAMESPACE = "needs"
    DEPENDS = ("bound",)

    @capability("x", risk="read", scope="self")
    def x(self):
        """Uses bound."""
        return self.dependency("bound").greeting


class NoScope(Plugin):
    """A plugin with a cap that lacks scope declaration: used to test rejection."""
    NAMESPACE = "noscope"

    @capability("bad")
    def bad(self):
        """This cap is missing scope."""
        return None


def _host(stored):
    host = PluginHost(facts=NodeFacts(node_id="w"), stored_settings=lambda ns: stored.get(ns, {}))
    cands = [Candidate("bound", "test", lambda: Bound), Candidate("needs", "test", lambda: Needs)]
    host.set_binder(lambda p: getattr(p, "bind_node", lambda n: None)("NODE"))
    return host, cands


@pytest.mark.asyncio
async def test_enable_uses_the_boot_path_and_binds_before_start():
    EVENTS.clear()
    stored = {"bound": {"greeting": "hello"}}
    host, cands = _host(stored)
    host.load(cands, enabled=[])            # discovered, filtered out at boot
    await host.start()
    assert host.plugins == [] and set(host.candidates) == {"bound", "needs"}
    res = await host.enable("bound")
    assert res["ok"] and res["caps"] == ["bound.hello"]
    assert await host.registry.call("bound.hello") == "hello"   # stored settings wired
    assert host.status["bound"]["state"] == "loaded"
    assert (await host.enable("bound"))["already_loaded"]
    assert (await host.enable("nope"))["error"] == "no such plugin module: nope"
    assert (await host.enable("needs"))["ok"]
    assert await host.registry.call("needs.x") == "hello"

    # A plugin others depend on cannot be pulled out from under them.
    refused = await host.disable("bound")
    assert not refused["ok"] and "needed by needs" in refused["error"]
    assert (await host.disable("needs"))["ok"]
    res = await host.disable("bound")
    assert res["ok"] and res["caps_removed"] == ["bound.hello"]
    assert not host.registry.has("bound.hello") and host.status["bound"]["state"] == "disabled"
    assert ("stop", res and EVENTS[-1][1]) == EVENTS[-1]


@pytest.mark.asyncio
async def test_reload_restarts_with_fresh_settings_and_reports_unavailable():
    EVENTS.clear()
    stored = {"bound": {"greeting": "one"}}
    host, cands = _host(stored)
    host.load(cands)
    await host.start()
    first = host.plugin("bound")
    stored["bound"] = {"greeting": "two"}
    res = await host.reload("bound")
    assert res["ok"]
    second = host.plugin("bound")
    assert second is not first and await host.registry.call("bound.hello") == "two"
    assert ("stop", id(first)) in EVENTS and ("start", id(second), "two") in EVENTS
    assert host.plugin("needs").dependency("bound") is second   # dependents re-pointed

    stored["bound"] = {"greeting": "two", "enabled": False}
    res = await host.reload("bound")
    assert not res["ok"] and res["unloaded"] and "available() returned False" in res["error"]
    assert not host.registry.has("bound.hello")


@pytest.mark.asyncio
async def test_settings_changed_reloads_only_reload_plugins():
    stored = {"bound": {"greeting": "a"}}
    host, cands = _host(stored)
    host.load(cands)
    await host.start()
    stored["bound"] = {"greeting": "b"}
    out = host.settings_changed(["bound"])
    assert out["reloading"] == ["bound"]
    for _ in range(50):
        await asyncio.sleep(0.01)
        if host.plugin("bound") is not None and host.plugin("bound").greeting == "b":
            break
    assert await host.registry.call("bound.hello") == "b"
    assert host.settings_changed(["needs"])["reloading"] == []   # no reload settings


@pytest.mark.asyncio
async def test_settings_change_from_a_worker_thread_reloads_on_the_loop():
    stored = {"bound": {"greeting": "a"}}
    host, cands = _host(stored)
    host.load(cands)
    await host.start()
    stored["bound"] = {"greeting": "c"}
    await asyncio.to_thread(host.settings_changed, ["bound"])   # a sync cap's thread
    for _ in range(50):
        await asyncio.sleep(0.01)
        if host.plugin("bound") is not None and host.plugin("bound").greeting == "c":
            break
    assert await host.registry.call("bound.hello") == "c"


@pytest.mark.asyncio
async def test_worker_runtime_enable_runs_bind_worker(tmp_path, monkeypatch):
    """Re-enabling work/sessions/config/... crashed: the throwaway loader never
    called bind_worker, so start() awaited a runtime that was never set."""
    from types import SimpleNamespace
    from rook.worker import admin, core
    from rook.worker.plugins import work as work_mod
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom.json")
    monkeypatch.setenv("ROOK_WORK_DB", str(tmp_path / "work.sqlite3"))
    monkeypatch.setattr(work_mod.WorkPlugin, "available", lambda self: True)
    w = core.Worker(SimpleNamespace(send=None), enabled=["info", "work", "shell"], name="w")
    await w.host.start()
    try:
        assert (await w.admin.plugin_disable("work"))["ok"]
        assert not w.registry.has("work.status")
        res = await w.admin.plugin_enable("work")
        assert res["ok"], res
        p = w.host.loaded("work")
        assert p.runtime is not None and "start_error" not in w.host.status["work"]
        assert (await w.registry.call("work.status", sessions=[]))["ok"]
        res = await w.admin.plugin_reload("shell")
        assert res["ok"] and w.host.loaded("shell")._worker is w
        assert (await w.admin.plugin_disable("selfupdate"))["ok"] is False
    finally:
        await w.host.stop()


# -- cap timeouts -------------------------------------------------------------

class Slow(Plugin):
    NAMESPACE = "slow"

    @capability("hang", risk="read", timeout=0.05, scope="self")
    async def hang(self):
        """Never returns."""
        await asyncio.sleep(30)

    @capability("own", risk="read", timeout=0.05, scope="self")
    async def own(self, timeout: float = 0.0):
        """Bounded by its caller's timeout."""
        await asyncio.sleep(0.2)
        return "done"

    @capability("forever", risk="read", timeout=None, scope="self")
    async def forever(self):
        """Opted out."""
        await asyncio.sleep(0.1)
        return "late"

    @capability("raises", risk="read", scope="self")
    async def raises(self):
        """Its own TimeoutError is not core's."""
        raise TimeoutError("backend")

    @capability("blocking", risk="read", timeout=0.05, scope="self")
    def blocking(self):
        """A sync handler past its timeout."""
        import time
        time.sleep(0.3)


@pytest.mark.asyncio
async def test_cap_timeouts(monkeypatch):
    from rook.core import plugin as plugin_mod
    from rook.core.registry import CapTimeout
    monkeypatch.setattr(plugin_mod, "TIMEOUT_MARGIN", 0.0)
    host = PluginHost(facts=NodeFacts(node_id="w"))
    host.load([Candidate("slow", "test", lambda: Slow)])
    reg = host.registry
    res = await host.dispatch("slow.hang", {})
    assert res == {"ok": False, "error": "timeout: slow.hang did not finish within 0.05s"}
    assert await reg.call("slow.own", timeout=0.5) == "done"     # caller's timeout + margin
    with pytest.raises(CapTimeout):
        await reg.call("slow.own")
    assert await reg.call("slow.forever") == "late"
    with pytest.raises(TimeoutError) as e:
        await reg.call("slow.raises")
    assert not isinstance(e.value, CapTimeout)
    with pytest.raises(CapTimeout):
        await reg.call("slow.blocking")
    assert reg.describe("slow.hang")["slow.hang"]["cap_timeout"] == 0.05
    assert reg.describe("slow.forever")["slow.forever"]["cap_timeout"] is None
    assert "cap_timeout" not in reg.describe("slow.raises")["slow.raises"]


def test_default_timeout_is_env_overridable_and_shell_exec_honours_args(monkeypatch):
    from rook.core.plugin import default_cap_timeout
    from rook.core.registry import CapabilityRegistry
    from rook.worker.plugins.shell import ShellPlugin
    monkeypatch.delenv("ROOK_CAP_TIMEOUT", raising=False)
    assert default_cap_timeout() == 300.0
    monkeypatch.setenv("ROOK_CAP_TIMEOUT", "0")
    assert default_cap_timeout() is None
    monkeypatch.setenv("ROOK_CAP_TIMEOUT", "40")
    reg = CapabilityRegistry()
    for c, fn in ShellPlugin().caps().items():
        reg.register(c, fn)
    assert reg.timeout_for("shell.exec", {}) == 45.0             # 30 s default + 15 s margin
    assert reg.timeout_for("shell.exec", {"timeout": 600}) == 615.0
    assert reg.timeout_for("shell.which", {"timeout": 600}) == 40.0   # takes no timeout arg


# -- worker settings without a restart ----------------------------------------

@pytest.mark.asyncio
async def test_worker_config_apply_without_restart_reloads_reload_plugins(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from rook.worker import admin, core, wconfig
    from rook.worker.plugins.config import ConfigPlugin
    for name, value in (("_DIR", tmp_path), ("_ACTIVE", tmp_path / "config.json"),
                        ("_PREV", tmp_path / "config.json.prev"),
                        ("_PENDING", tmp_path / "config.json.pending"),
                        ("_PUSHED", set()), ("_SECRET_REFS", {}), ("_RESOLVED", set())):
        monkeypatch.setattr(wconfig, name, value)
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.delenv("ROOK_BOUND_GREETING", raising=False)
    w = core.Worker(SimpleNamespace(send=None), enabled=["info"], name="w")
    w.host.load([Candidate("bound", "test", lambda: Bound)])
    w.host.set_binder(w._bind_plugin)   # binds the late-loaded plugin too
    await w.host.start()
    try:
        cfg = ConfigPlugin()
        cfg.bind_worker(w)
        res = await cfg._apply({"env": {"ROOK_BOUND_GREETING": "pushed"}}, epoch=7,
                               restart=False)
        assert res["ok"] and res["applied"]["reloading"] == ["bound"]
        for _ in range(50):
            await asyncio.sleep(0.01)
            if await w.registry.call("bound.hello") == "pushed":
                break
        assert await w.registry.call("bound.hello") == "pushed"
    finally:
        await w.host.stop()
        # Not monkeypatch.delenv: it would record "pushed" and put it back at
        # teardown, leaking into later tests that load Bound.
        os.environ.pop("ROOK_BOUND_GREETING", None)


@pytest.mark.asyncio
async def test_reload_keeps_caps_registered_until_the_new_instance_is_up():
    """An announce built mid-reload (the old instance stopping, the new one
    starting) still lists the plugin's caps; nothing collides on the way."""
    EVENTS.clear()
    stored = {"bound": {"greeting": "one"}}
    host, cands = _host(stored)
    host.load(cands)
    await host.start()
    seen_during = []
    real_stop = Bound.stop

    async def stop(self):
        seen_during.append(sorted(c for c in host.registry.list() if c.startswith("bound.")))
        await real_stop(self)
    Bound.stop = stop
    try:
        stored["bound"] = {"greeting": "two"}
        assert (await host.reload("bound"))["ok"]
    finally:
        Bound.stop = real_stop
    assert seen_during == [["bound.hello"]]
    assert await host.registry.call("bound.hello") == "two"


@pytest.mark.asyncio
async def test_failed_reload_drops_caps_and_unbinds_dependents():
    """A dependent must not keep calling a stopped instance: its dependency()
    goes to None (status says so) until the plugin is back."""
    stored = {"bound": {"greeting": "one"}}
    host, cands = _host(stored)
    host.load(cands)
    await host.start()
    old = host.plugin("bound")
    assert host.plugin("needs").dependency("bound") is old
    stored["bound"] = {"greeting": "one", "enabled": False}
    res = await host.reload("bound")
    assert not res["ok"] and res["unloaded"] and res["dependents_unbound"] == ["needs"]
    assert not host.registry.has("bound.hello")
    assert host.plugin("needs").dependency("bound") is None
    assert host.status["needs"]["dependency_unloaded"] == "bound"
    with pytest.raises(AttributeError):
        await host.registry.call("needs.x")   # refuses rather than reading the stopped one

    stored["bound"] = {"greeting": "back"}
    assert (await host.enable("bound"))["ok"]
    assert await host.registry.call("needs.x") == "back"
    assert "dependency_unloaded" not in host.status["needs"]


@pytest.mark.asyncio
async def test_bind_hooks_follow_reload_and_disable():
    stored = {"bound": {"greeting": "one"}}
    host, cands = _host(stored)
    host.load(cands)
    await host.start()
    log = []
    host.add_bind_hook(lambda p: log.append(("bind", p.NAMESPACE, id(p))),
                       lambda p: log.append(("unbind", p.NAMESPACE, id(p))))
    first = host.plugin("bound")
    assert ("bind", "bound", id(first)) in log
    log.clear()
    assert (await host.reload("bound"))["ok"]
    second = host.plugin("bound")
    assert log == [("unbind", "bound", id(first)), ("bind", "bound", id(second))]
    log.clear()
    needs = host.plugin("needs")
    assert (await host.disable("needs"))["ok"]
    assert log == [("unbind", "needs", id(needs))]


@pytest.mark.asyncio
async def test_plugin_with_cap_missing_scope_is_rejected():
    """A capability without a scope declaration fails to load during _activate,
    before any caps are registered."""
    host = PluginHost(facts=NodeFacts(node_id="w"))
    cands = [Candidate("noscope", "test", lambda: NoScope)]
    host.load(cands)
    status = host.status.get("noscope", {})
    assert status.get("state") == "failed"
    assert "missing" in status.get("reason", "").lower() and "scope" in status.get("reason", "").lower()
    assert not host.registry.has("noscope.bad")


def test_custom_command_caps_declare_local_scope():
    """Agent-built cmd.* caps are parameterised shell.exec on this machine."""
    from rook.core.registry import CapabilityRegistry
    from rook.worker.admin import WorkerAdmin
    admin = WorkerAdmin.__new__(WorkerAdmin)   # skips reading the real custom_caps.json
    admin.registry = CapabilityRegistry()
    admin._register_custom("hello", {"command": "echo hi", "args": []})
    meta = admin.registry.meta("cmd.hello")
    assert (meta.risk, meta.scope) == ("exec", "local")

def test_plugin_reload_and_disable_are_destructive():
    from rook.core import authz
    for cap in ("worker.plugin.reload", "worker.plugin.disable",
                "hub.plugin.reload", "hub.plugin.disable"):
        assert authz.effective_tier(cap) == "admin", cap
        assert "destructive" in authz.builtin_tags(cap), cap


@pytest.mark.asyncio
async def test_selfupdate_reload_does_not_stack_binary_handlers():
    from types import SimpleNamespace
    from rook.worker.plugins.selfupdate import SelfUpdatePlugin
    handlers = []
    worker = SimpleNamespace(
        register_binary_handler=lambda h: handlers.append(h) if h not in handlers else None,
        unregister_binary_handler=lambda h: handlers.remove(h) if h in handlers else None)
    for _ in range(3):   # boot, then two reloads: bind new, stop old
        p = SelfUpdatePlugin()
        p.bind_worker(worker)
        await p.stop()
    p = SelfUpdatePlugin()
    p.bind_worker(worker)
    assert len(handlers) == 1 and handlers[0] == p._ota_binary
