"""rook.core2.worker host + loader: plugin folders become callable caps.

Fixtures: tests/fixtures/castling/echo (YAML only) and counter (run: + hooks).
Every test runs under a hard asyncio timeout.
"""

import asyncio
import hashlib
import shutil
from pathlib import Path

import pytest

from rook.core2 import wire
from rook.core2.worker.host import CallError, Host, LoadError
from rook.core2.worker.loader import load_folder, merge_settings, resolve_run
from rook.kit.ctx import Caller

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "castling"
ME = Caller(person="bake", agent="claude", band="b1", why="testing the host")
needs_echo = pytest.mark.skipif(shutil.which("echo") is None, reason="no echo binary")


def go(body, timeout=15):
    async def main():
        return await asyncio.wait_for(body(), timeout)
    return asyncio.run(main())


def counter(tmp_path, folder="v1", version=1, script_extra=""):
    """A copy of the counter fixture whose code says VERSION = version."""
    dst = tmp_path / folder
    shutil.copytree(FIX / "counter", dst)
    y = dst / "plugin.yaml"
    y.write_text(y.read_text().replace("version: 1.0.0", f"version: {version}.0.0"))
    s = dst / "scripts" / "counter.py"
    s.write_text(s.read_text().replace("VERSION = 1", f"VERSION = {version}") + script_extra)
    return dst


def state(host):
    """The counter script module's STATE for the loaded version."""
    (mod,) = host.plugins["counter"].modules.values()
    return mod.STATE


async def until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


# -- YAML-only plugin -----------------------------------------------------------

@needs_echo
def test_yaml_plugin_calls_and_aliases():
    async def body():
        host = Host(os_name="linux")
        p = await host.load(FIX / "echo")
        assert p.folder == (FIX / "echo").resolve()
        assert (await host.call("echo.say", {"text": "hi there"}, ME)).strip() == "hi there"
        assert (await host.call("echo.speak", {"text": "alias"}, ME)).strip() == "alias"
        assert (await host.call("echo.greet", {}, ME)).strip() == "hello world"
        await host.close()
    go(body)


@needs_echo
def test_bad_args_and_unknown_cap():
    async def body():
        host = Host(os_name="linux")
        await host.load(FIX / "echo")
        with pytest.raises(CallError, match=r"^bad args: 'text' is a required property"):
            await host.call("echo.say", {}, ME)
        with pytest.raises(CallError, match=r"^bad args: text: 5 is not of type 'string'"):
            await host.call("echo.say", {"text": 5}, ME)
        with pytest.raises(CallError, match=r"^bad args: .*'extra' was unexpected"):
            await host.call("echo.say", {"text": "x", "extra": 1}, ME)
        with pytest.raises(CallError, match=r"^unknown capability: echo.nope$"):
            await host.call("echo.nope", {}, ME)
        await host.close()
    go(body)


def test_unavailable_caps_have_reasons():
    async def body():
        host = Host(os_name="linux", hardware=["gpu"])
        p = await host.load(FIX / "echo")
        assert p.unavailable == {
            "echo.gone": "missing bin: rook-no-such-binary",
            "echo.windows": "not on linux (runs on windows)",
            "echo.lamp": "missing hardware: lamp",
            "echo.motto": "setting not set: motto",
        }
        with pytest.raises(CallError, match=r"^unavailable: echo.lamp: missing hardware: lamp$"):
            await host.call("echo.lamp", {}, ME)
        assert "echo.lamp" not in host.caps()
        ann = await host.announce()
        assert ann["unavailable"]["echo.gone"] == "missing bin: rook-no-such-binary"
        await host.close()

        lamp = Host(os_name="linux", hardware=["lamp"])
        p = await lamp.load(FIX / "echo")
        assert "echo.lamp" in p.caps and "echo.lamp" not in p.unavailable
        await lamp.close()
    go(body)


def test_plugin_level_requirements_disable_every_cap_and_hooks(tmp_path):
    async def body():
        src = counter(tmp_path)
        y = src / "plugin.yaml"
        y.write_text(y.read_text().replace("doc: Counts.", "doc: Counts.\nplatforms: [macos]"))
        host = Host(os_name="linux")
        p = await host.load(src)
        assert p.caps == {}
        assert set(p.unavailable.values()) == {"not on linux (runs on macos)"}
        assert "counter" not in host.scheduler.plugins   # hooks never started
        await host.close()
    go(body)


@needs_echo
def test_secret_settings_resolve_per_call():
    async def body():
        vault = {"echo-token": "s3cret-1"}
        asked = []

        def resolve(name):
            asked.append(name)
            return vault.get(name)

        host = Host(os_name="linux", resolve_secret=resolve)
        await host.set_settings("echo", hub={"token": "echo-token"})
        await host.load(FIX / "echo")
        assert (await host.call("echo.secret", {}, ME)).strip() == "s3cret-1"
        vault["echo-token"] = "s3cret-2"   # rotated in the vault: the next call sees it
        assert (await host.call("echo.secret", {}, ME)).strip() == "s3cret-2"
        assert asked == ["echo-token", "echo-token"]
        await host.call("echo.greet", {}, ME)   # doesn't name the secret: not fetched
        assert len(asked) == 2
        del vault["echo-token"]
        with pytest.raises(CallError, match="not in the vault"):
            await host.call("echo.secret", {}, ME)
        await host.close()
    go(body)


@needs_echo
def test_settings_merge_and_requires_settings():
    async def body():
        host = Host(os_name="linux")
        await host.set_settings("echo", local={"greeting": "hi", "motto": "local!"})
        p = await host.load(FIX / "echo")
        assert (await host.call("echo.greet", {"name": "bake"}, ME)).strip() == "hi bake"
        # motto is where: hub, so the local value doesn't count.
        assert p.unavailable["echo.motto"] == "setting not set: motto"
        await host.set_settings("echo", hub={"motto": "be kind", "greeting": "yo"})
        assert "echo.motto" not in host.plugins["echo"].unavailable
        assert (await host.call("echo.motto", {}, ME)).strip() == "be kind"
        # greeting is where: worker: the local value wins over the hub's.
        assert (await host.call("echo.greet", {}, ME)).strip() == "hi world"
        # a settings change rebinds in place; it isn't a version to roll back to
        assert "echo" not in host.previous
        await host.close()
    go(body)


def test_merge_settings_rules():
    decls = {"a": {"where": "worker"}, "b": {"where": "hub"}, "c": {"where": "person"}, "d": {}}
    out = merge_settings(decls, {"a": 1, "b": 1, "d": 1, "z": 1}, {"a": 2, "b": 2, "c": 2})
    assert out == {"a": 1, "b": 2, "c": 2, "d": 1}
    assert merge_settings(decls, {}, {"a": 2}) == {"a": 2}


# -- run: plugin ---------------------------------------------------------------

def test_run_plugin_hooks_bind_and_context(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path))
        st = state(host)
        assert st["started"] and st["events"] == ["start"]
        assert await host.call("counter.add", {}, ME) == 1          # arg default
        assert await host.call("counter.add", {"n": 4}, ME) == 5
        assert await host.call("counter.add_ten", {}, ME) == 15      # bind: n=10
        with pytest.raises(CallError, match="bad args"):            # bound args aren't caller args
            await host.call("counter.add_ten", {"n": 1}, ME)
        assert await host.call("counter.whoami", {}, ME) == {
            "person": "bake", "agent": "claude", "why": "testing the host"}
        f = await host.call("counter.file", {}, ME)
        assert f["name"] == "a.txt" and f["data"] == "aGk=" and f["bytes"] == 2
        ann = await host.announce()
        assert ann["status"] == {"counter": {"count": 15, "version": 1}}
        host.publish("worker.online", {"at": 1})   # on: hooks share the caps' module
        await until(lambda: ("warm", "worker.online", {"at": 1}) in st["events"])
        await host.close()
        assert st["events"][-1] == "stop"
    go(body)


def test_plugin_failures_never_take_the_host_down(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path))
        with pytest.raises(CallError, match=r"^counter.crash failed: RuntimeError: kaboom$"):
            await host.call("counter.crash", {}, ME)
        with pytest.raises(CallError, match=r"^counter.quit failed: PluginExit: exited with code 3$"):
            await host.call("counter.quit", {}, ME)
        with pytest.raises(CallError, match=r"^device busy$") as e:
            await host.call("counter.refuse", {}, ME)
        assert e.value.retry is True
        assert await host.call("counter.add", {}, ME) == 1   # still serving
        await host.close()
    go(body)


def test_secrets_are_redacted_from_run_cap_errors(tmp_path):
    async def body():
        vault = {"tv": "tv-pairing-key"}
        host = Host(os_name="linux", resolve_secret=vault.get)
        await host.set_settings("counter", hub={"key": "tv"})
        await host.load(counter(tmp_path))
        with pytest.raises(CallError, match=r"^tv refused \[redacted\]$"):
            await host.call("counter.leak", {}, ME)
        vault["tv"] = "other-key"
        with pytest.raises(CallError, match=r"^counter.leak failed: ValueError: bad key \[redacted\]$"):
            await host.call("counter.leak", {}, ME)
        await host.close()
    go(body)


def test_timeouts_cancel_async_and_hold_exclusive_locks(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path))
        st = state(host)
        with pytest.raises(CallError, match=r"^counter.slow timed out after 0.2s$") as e:
            await host.call("counter.slow", {}, ME)
        assert e.value.retry
        await until(lambda: "slow-cancelled" in st["events"])

        # hold's thread outlives its 200 ms timeout; the device stays locked
        # until it really returns, so poke runs only after hold-end.
        with pytest.raises(CallError, match="timed out"):
            await host.call("counter.hold", {}, ME)
        assert await host.call("counter.poke", {}, ME) == "poked"
        ev = st["events"]
        assert ev.index("hold-end") < ev.index("poke")
        await host.close()
    go(body)


def test_exclusive_lock_wait_counts_against_the_timeout(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path))
        first = asyncio.ensure_future(host.call("counter.poke", {}, ME))
        lock = host._locks.setdefault("device", asyncio.Lock())
        await first
        await lock.acquire()   # someone else holds the device
        try:
            with pytest.raises(CallError, match=r"^busy: device is in use$"):
                await host.call("counter.hold", {}, ME)
        finally:
            lock.release()
        await host.close()
    go(body)


# -- hot reload and rollback -----------------------------------------------------

def test_hot_reload_and_rollback(tmp_path):
    async def body():
        host = Host(os_name="linux")
        v1, v2 = counter(tmp_path, "v1", 1), counter(tmp_path, "v2", 2)
        await host.load(v1)
        st1 = state(host)
        assert await host.call("counter.version", {}, ME) == 1
        p2 = await host.load(v2)
        assert p2.version == "2.0.0"
        assert await host.call("counter.version", {}, ME) == 2
        assert st1["events"] == ["start", "stop"]          # old hooks stopped
        assert state(host)["events"] == ["start"]          # new hooks started, fresh state
        assert host.previous["counter"].version == "1.0.0"

        await host.rollback("counter")
        assert await host.call("counter.version", {}, ME) == 1
        assert host.previous["counter"].version == "2.0.0"  # roll forward again if wanted
        await host.rollback("counter")
        assert await host.call("counter.version", {}, ME) == 2
        await host.close()
    go(body)


def test_failed_loads_keep_the_running_version(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path, "v1", 1))
        st = state(host)

        broken = counter(tmp_path, "v2", 2, script_extra="\ndef broken(:\n")
        with pytest.raises(LoadError, match="SyntaxError"):
            await host.load(broken)
        exits = counter(tmp_path, "v3", 3, script_extra="\nimport sys\nsys.exit(1)\n")
        with pytest.raises(LoadError, match="SystemExit"):
            await host.load(exits)
        bad_start = counter(tmp_path, "v4", 4, script_extra="\ndef start(ctx):\n    raise ctx.Error('no tv')\n")
        with pytest.raises(LoadError, match="start hook failed: no tv"):
            await host.load(bad_start)

        assert await host.call("counter.version", {}, ME) == 1
        # v1's hooks were stopped for the swap and started again.
        assert st["events"] == ["start", "stop", "start"]
        assert "counter" in host.scheduler.plugins
        assert "counter" not in host.previous
        await host.close()
    go(body)


def test_invalid_manifest_is_refused(tmp_path):
    async def body():
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "plugin.yaml").write_text("manifest: 1\nplugin: Bad Name\nversion: 1.0.0\n")
        host = Host(os_name="linux")
        with pytest.raises(LoadError):
            await host.load(bad)
        assert host.plugins == {}
    go(body)


def test_unload_and_rollback_after_unload(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path))
        st = state(host)
        await host.unload("counter")
        assert st["events"] == ["start", "stop"]
        with pytest.raises(CallError, match="unknown capability"):
            await host.call("counter.version", {}, ME)
        assert (await host.announce())["manifests"] == []
        await host.rollback("counter")
        assert await host.call("counter.version", {}, ME) == 1
        with pytest.raises(LoadError):
            await host.unload("nope")
        await host.close()
    go(body)


def test_rollback_refuses_a_changed_or_missing_previous(tmp_path):
    async def body():
        host = Host(os_name="linux")
        with pytest.raises(LoadError, match="no previous version"):
            await host.rollback("counter")
        v1 = counter(tmp_path, "v1", 1)
        await host.load(v1)
        await host.load(counter(tmp_path, "v2", 2))
        y = v1 / "plugin.yaml"
        y.write_text(y.read_text().replace("doc: Counts.", "doc: Counts differently."))
        with pytest.raises(LoadError, match="changed since"):
            await host.rollback("counter")
        assert await host.call("counter.version", {}, ME) == 2
        await host.close()
    go(body)


# -- announce ------------------------------------------------------------------

CORE = {"manifest": 1, "plugin": "core", "version": "2.0.0", "builtin": True,
        "caps": {"manifests": {"doc": "Manifests.", "risk": "read", "scope": "self"},
                 "trust.get": {"doc": "Trust.", "risk": "read", "scope": "self"}}}


def test_announce_and_filtered_view(tmp_path):
    async def body():
        host = Host(os_name="linux")
        host.add_builtin(CORE, {"core.manifests": lambda ctx, **a: host.manifests(**a)})
        await host.load(counter(tmp_path))
        await host.load(FIX / "echo")
        ann = await host.announce()
        names = [m["plugin"] for m in ann["manifests"]]
        assert names == ["core", "counter", "echo"]
        assert ann["manifests"][0] == {"plugin": "core", "version": "2.0.0",
                                       "hash": wire.manifest_hash(CORE), "builtin": True}
        assert ann["manifests_hash"] == hashlib.sha256(wire.canonical(ann["manifests"])).hexdigest()
        assert ann["unavailable"]["core.trust.get"] == "not implemented on this worker"
        assert "core.manifests" in host.caps()
        assert host.caps()["counter.hold"] == {"risk": "write", "effects": []}
        assert host.caps()["echo.speak"] == host.caps()["echo.say"]

        # A band whose ceiling grants only echo.* sees core (core.manifests is
        # within every ceiling) and echo, not counter.
        view = await host.announce({"grants": ["echo.*@kitchen"]})
        assert [m["plugin"] for m in view["manifests"]] == ["core", "echo"]
        assert "status" not in view
        got = await host.call("core.manifests", {}, ME)
        assert [m["plugin"] for m in got["manifests"]] == ["core", "counter", "echo"]
        assert host.manifests(["echo"])["manifests"][0]["version"] == "1.0.0"
        with pytest.raises(LoadError, match="core is not a loaded plugin"):
            await host.unload("core")
        await host.close()
    go(body)


def test_status_hook_failure_is_reported_not_raised(tmp_path):
    async def body():
        host = Host(os_name="linux")
        await host.load(counter(tmp_path, script_extra="\ndef status(ctx):\n    raise RuntimeError('tv gone')\n"))
        assert (await host.announce())["status"] == {"counter": {"error": "tv gone"}}
        await host.load(counter(tmp_path, "v2", script_extra="\ndef status(ctx):\n    raise SystemExit(2)\n"))
        assert (await host.announce())["status"] == {"counter": {"error": "exited with code 2"}}
        await host.close()
    go(body)


# -- loader pieces -------------------------------------------------------------

def test_class_method_run_targets_share_one_instance_per_load(tmp_path):
    (tmp_path / "s.py").write_text(
        "class Tv:\n"
        "    made = 0\n"
        "    def __init__(self):\n"
        "        Tv.made += 1\n"
        "        self.n = 0\n"
        "    def bump(self, ctx):\n"
        "        self.n += 1\n"
        "        return self.n\n"
        "    def peek(self, ctx):\n"
        "        return self.n\n"
        "def plain(ctx):\n"
        "    return 'plain'\n"
        "VALUE = 3\n")
    modules, instances = {}, {}
    bump = resolve_run(tmp_path, "s.py:Tv.bump", modules, instances)
    peek = resolve_run(tmp_path, "s.py:Tv.peek", modules, instances)
    assert bump(None) == 1 and bump(None) == 2 and peek(None) == 2
    assert resolve_run(tmp_path, "s.py:plain", modules, instances)(None) == "plain"
    (mod,) = modules.values()
    assert mod.Tv.made == 1
    fresh = resolve_run(tmp_path, "s.py:Tv.peek", {}, {})   # a new load starts clean
    assert fresh(None) == 0
    with pytest.raises(Exception, match="not a class"):
        resolve_run(tmp_path, "s.py:plain.x", modules, instances)
    with pytest.raises(Exception, match="no method"):
        resolve_run(tmp_path, "s.py:Tv.nope", modules, instances)


def test_load_folder_skips_hub_caps_and_refuses_builtin(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "plugin.yaml").write_text(
        "manifest: 1\nplugin: rec\nversion: 1.0.0\n"
        "records:\n  note:\n    fields:\n      title: { type: str }\n"
        "caps:\n  ping:\n    doc: Ping.\n    risk: read\n    scope: self\n    on: hub\n"
        "    action:\n      http: { url: 'http://127.0.0.1:9/' }\n")
    p = load_folder(d, os_name="linux")
    assert p.caps == {} and p.unavailable == {}   # all hub-side: not this worker's business
    b = tmp_path / "b"
    b.mkdir()
    (b / "plugin.yaml").write_text("manifest: 1\nplugin: b\nversion: 1.0.0\nbuiltin: true\n")
    with pytest.raises(LoadError, match="builtin"):
        load_folder(b, os_name="linux")
