"""The settings framework: schema, resolution, store, service, account API,
the dashboard's precedence fix (P1), worker delivery and secret masking."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from rook.core import settings as cs
from rook.core.plugin import Plugin, SettingsView, setting

SECRET = "correct-horse-battery-staple"


class FakeVault:
    def __init__(self):
        self.data, self.log = {}, []

    def set(self, name, value, description, actor):
        self.data[name] = value
        self.log.append(("set", name, actor))
        return {"name": name}

    def get(self, name, actor, via="get", task=None):
        self.log.append(("get", name, actor))
        return self.data[name]

    def delete(self, name, actor):
        return self.data.pop(name, None) is not None

    def list(self):
        return [{"name": n} for n in sorted(self.data)]


class FakeClient:
    """Band client double: a roster and scripted replies."""

    def __init__(self, workers=None, replies=None):
        self.workers = workers or {}
        self.replies = replies or {}
        self.calls = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.calls.append((cap, args, target, identity))
        r = self.replies.get(cap)
        return r(args) if callable(r) else (r if r is not None else {"ok": True, "result": {"ok": True}})


# -- the setting() declaration -------------------------------------------------

def test_setting_fields_and_validation():
    s = setting("port", int, 80, env=["NEW_PORT", "OLD_PORT"], min=1, max=65535,
                apply="restart", scope="band", overridable=("worker",), group="Net")
    assert s.env == "NEW_PORT" and s.env_aliases == ("OLD_PORT",)
    assert s.env_names("demo") == ("NEW_PORT", "OLD_PORT", "ROOK_DEMO_PORT")
    d = s.describe()
    assert d["apply"] == "restart" and d["overridable"] == ["worker"] and d["env_aliases"] == ["OLD_PORT"]
    with pytest.raises(ValueError):
        s.coerce(0)
    with pytest.raises(ValueError):
        setting("x", str, apply="sometimes")
    with pytest.raises(ValueError):
        setting("x", str, scope="worker", overridable=("band",))   # overrides go down only
    assert setting("hp", "hostport", "a:1").coerce("hub.example.com:443") == "hub.example.com:443"
    with pytest.raises(ValueError):
        setting("hp", "hostport").coerce("no-port")
    with pytest.raises(ValueError):
        setting("u", "url").coerce("ftp://x")
    assert setting("l", list).coerce("a, b,,c") == ["a", "b", "c"]
    assert setting("p", str, pattern=r"[a-z]+").coerce("abc") == "abc"
    with pytest.raises(ValueError):
        setting("p", str, pattern=r"[a-z]+").coerce("ABC")


def test_settings_view_env_aliases_and_refresh(monkeypatch):
    class Demo(Plugin):
        NAMESPACE = "demo"
        SETTINGS = (setting("mode", str, "a", env=["DEMO_MODE", "OLD_DEMO_MODE"]),)

    view = SettingsView(Demo(), stored={"mode": "b"})
    assert view["mode"] == "b"
    monkeypatch.setenv("ROOK_DEMO_MODE", "c")            # canonical name
    assert view["mode"] == "c" and view.env_var("mode") == "ROOK_DEMO_MODE"
    monkeypatch.setenv("OLD_DEMO_MODE", "d")             # alias beats canonical
    assert view["mode"] == "d"
    monkeypatch.delenv("OLD_DEMO_MODE")
    monkeypatch.delenv("ROOK_DEMO_MODE")
    view.refresh({"mode": "e"})
    assert view["mode"] == "e" and view.source("mode") == "stored"


# -- resolution and masking ----------------------------------------------------

def test_resolve_precedence_inherited_and_invalid():
    s = setting("interval", int, 30, scope="band", overridable=("worker",))
    r = cs.resolve(s, "k", [("band", 60), ("worker", "oops")])
    assert r["value"] == 60 and r["source"] == "band"
    assert r["invalid"] == [{"source": "worker", "error": r["invalid"][0]["error"]}]
    r = cs.resolve(s, "k", [("band", 60), ("worker", 90)], env=("X", "120"))
    assert r["value"] == 120 and r["locked"] and r["env"] == "X" and r["inherited"] == 90
    r = cs.resolve(s, "k", [("band", 60), ("worker", 90)])
    assert r["value"] == 90 and r["inherited"] == 60
    assert cs.resolve(s, "k")["source"] == "default"
    r = cs.resolve(s, "k", [], file="45")
    assert r["value"] == 45 and r["source"] == "file"


def test_resolve_never_shows_a_secret():
    s = setting("token", str, secret=True)
    r = cs.resolve(s, "k", [("hub", "{{secret:plugin.x.token}}")], env=("TOKEN", SECRET))
    assert SECRET not in json.dumps(r)
    assert r["value"] == cs.MASK and r["fingerprint"] == cs.fingerprint(SECRET)
    assert r["layers"][0]["value"] == "{{secret:plugin.x.token}}"   # a reference is not the value


def test_masking_helpers():
    cfg = {"psk": SECRET, "name": "w", "env": {"ROOK_WAKE_CMD": "claude -p {prompt_file}",
                                               "PIKVM_PASS": SECRET, "OTHER": "x",
                                               "PIKVM_PASS2": "{{secret:worker.w.pikvm}}"}}
    out = cs.mask_worker_config(cfg, public_env={"ROOK_WAKE_CMD", "PIKVM_PASS"})
    assert SECRET not in json.dumps(out)
    assert out["psk"] == cs.MASK and out["env"]["ROOK_WAKE_CMD"].startswith("claude")
    assert out["env"]["PIKVM_PASS"] == cs.MASK        # public, but looks like a credential
    assert out["env"]["OTHER"] == cs.MASK             # undeclared: masked
    assert out["env"]["PIKVM_PASS2"].startswith("{{secret:")
    assert cs.vault_name("worker", "My Box!", "pikvm.password") == "worker.my-box.pikvm.password"
    assert len(cs.vault_name("x" * 80)) <= 64
    assert cs.secret_ref("{{secret:a.b}}") == "a.b" and cs.secret_ref("x {{secret:a}}") is None


# -- the store -------------------------------------------------------------------


# -- the service -------------------------------------------------------------------


def _worker_node(caps=("worker.plugin.list", "worker.config_get", "worker.settings_report")):
    client = FakeClient(workers={"w1": {"name": "box", "caps": list(caps), "band": "*"}})
    return SimpleNamespace(client=client, worker_id="hub")


# -- hub node: caps, live refresh, sensitive journaling --------------------------


# -- account API -------------------------------------------------------------------


# -- the dashboard: environment over setup.json (P1), chat path (P4) ------------------

def test_dashboard_explicit_env_beats_setup_and_store():
    from rook.remote import dashboard_settings as ds
    assert ds.explicit_sources(["--domain", "x"], {"ROOK_BAND_NAME": "b"}) == {
        "domain": "flag --domain", "band_name": "env ROOK_BAND_NAME"}
    current = {"domain": "env.example.com", "hub_public": "hub.example.com:443", "band_name": "rook-band"}
    setup = {"pyz_domain": "file.example.com", "hub_public": "file.example.com:443", "band_name": "filed"}
    vals, src, conflicts = ds.resolve(current, {"domain": "env ROOK_DOMAIN"}, setup,
                                      {"band_name": "stored-name"})
    assert vals == {"domain": "env.example.com", "hub_public": "file.example.com:443",
                    "band_name": "stored-name"}
    assert src == {"domain": "env ROOK_DOMAIN", "hub_public": "setup.json", "band_name": "stored"}
    assert conflicts and conflicts[0]["key"] == "core.hub.domain"
    # Older callers (no explicit map): setup.json still wins over the constructor.
    vals, _, conflicts = ds.resolve(current, None, setup, {})
    assert vals["domain"] == "file.example.com" and not conflicts


def test_enrollment_env_key_only_seeds(tmp_path, monkeypatch):
    from rook.remote.enrollment import EnrollmentStore
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    e = EnrollmentStore(tmp_path / "enrollment.db")
    assert e.import_config(["first-key-alpha"], seed_only=True) == []
    assert e.transport_psks() == ["first-key-alpha"]
    # A different key later in the environment is ignored, not added.
    assert e.import_config(["stale-key-bravo"], seed_only=True) == ["stale-key-bravo"]
    assert e.transport_psks() == ["first-key-alpha"]


# -- worker side -----------------------------------------------------------------------

@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    from rook.worker import admin, wconfig
    monkeypatch.setattr(wconfig, "_DIR", tmp_path)
    monkeypatch.setattr(wconfig, "_ACTIVE", tmp_path / "config.json")
    monkeypatch.setattr(wconfig, "_PREV", tmp_path / "config.json.prev")
    monkeypatch.setattr(wconfig, "_PENDING", tmp_path / "config.json.pending")
    monkeypatch.setattr(wconfig, "_PUSHED", set())
    monkeypatch.setattr(wconfig, "_SECRET_REFS", {})
    monkeypatch.setattr(wconfig, "_RESOLVED", set())
    monkeypatch.setattr(admin, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    return tmp_path


def test_worker_config_masks_and_secret_refs_stay_off_disk_values(worker_home, monkeypatch):
    from rook.worker import wconfig
    monkeypatch.delenv("PIKVM_PASS", raising=False)
    monkeypatch.delenv("ROOK_WAKE_CMD", raising=False)
    wconfig.stage_apply({"psk": SECRET, "env": {"PIKVM_PASS": "{{secret:worker.box.pikvm.password}}",
                                                "ROOK_WAKE_CMD": "claude"}}, 5, 60)
    keys = wconfig.apply_env()
    import os
    assert "PIKVM_PASS" not in os.environ and os.environ["ROOK_WAKE_CMD"] == "claude"
    assert set(keys) == {"PIKVM_PASS", "ROOK_WAKE_CMD"}
    assert wconfig.unresolved() == {"PIKVM_PASS": "worker.box.pikvm.password"}
    assert wconfig.set_resolved({"PIKVM_PASS": SECRET}) == ["PIKVM_PASS"]
    assert os.environ["PIKVM_PASS"] == SECRET and not wconfig.unresolved()
    on_disk = (worker_home / "config.json").read_text()
    assert "{{secret:worker.box.pikvm.password}}" in on_disk   # a reference, not the value
    cur = wconfig.current({"ROOK_WAKE_CMD"})
    assert cur["config"]["psk"] == cs.MASK and cur["config"]["env"]["ROOK_WAKE_CMD"] == "claude"
    assert wconfig.masked_env_keys({"ROOK_WAKE_CMD"}) == {"PIKVM_PASS"}
    monkeypatch.delenv("PIKVM_PASS", raising=False)
    monkeypatch.delenv("ROOK_WAKE_CMD", raising=False)


def test_shell_env_reads_mask_pushed_secrets(worker_home, monkeypatch):
    from rook.worker import wconfig
    from rook.worker.plugins.shell import ShellPlugin
    wconfig.apply_env({"env": {"SOME_TOKEN": "tok-value", "ROOK_WAKE_CMD": "claude"}})
    sh = ShellPlugin()

    class P:
        SETTINGS = (setting("wake_command", str, env="ROOK_WAKE_CMD"),)
    sh.bind_worker(SimpleNamespace(plugins=[P()]))
    listed = sh._env_list(prefix="")
    assert listed["SOME_TOKEN"] == "***" and listed["ROOK_WAKE_CMD"] == "claude"
    assert sh._env_get("SOME_TOKEN") == "***"
    monkeypatch.delenv("SOME_TOKEN", raising=False)
    monkeypatch.delenv("ROOK_WAKE_CMD", raising=False)


@pytest.mark.asyncio
async def test_runtime_enable_survives_restart_despite_enable_flag(worker_home):
    from rook.worker.core import Worker
    transport = SimpleNamespace(send=None)
    w = Worker(transport=transport, enabled=["info", "shell"])
    assert "log" not in {p._module for p in w.plugins}
    listed = w.admin.plugin_list()["plugins"]
    row = next(p for p in listed if p["module"] == "log")
    assert row["excluded"] and "--enable" in row["reason"]
    assert (await w.admin.plugin_enable("log"))["ok"]
    assert json.loads((worker_home / "plugins.json").read_text())["enabled"] == ["log"]
    # Restart with the same --enable: the runtime enable is still there.
    w2 = Worker(transport=transport, enabled=["info", "shell"])
    assert "log" in {p._module for p in w2.plugins}
    assert "log" not in w2.admin.excluded()
    assert (await w2.admin.plugin_disable("log"))["ok"]
    w3 = Worker(transport=transport, enabled=["info", "shell"])
    assert "log" not in {p._module for p in w3.plugins}
    report = w3._settings_report()
    assert "secret_refs" in report["features"] and "log" in report["excluded"]


@pytest.mark.asyncio
async def test_worker_fetches_secret_refs_from_the_hub(worker_home, monkeypatch):
    from rook.worker import wconfig
    from rook.worker.core import Worker
    sent = []

    class Transport:
        async def send(self, data):
            msg = json.loads(data)
            sent.append(msg)
            if msg.get("cap") == "settings.worker_secret":
                reply = {"id": msg["id"], "from": "hub", "ok": True,
                         "result": {"secrets": {"worker.box.pikvm.password": SECRET}, "missing": []}}
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(w._on_message(json.dumps(reply).encode(), ("h", 1))))

    w = Worker(transport=Transport(), enabled=["info"])
    monkeypatch.delenv("PIKVM_PASS", raising=False)
    wconfig.apply_env({"env": {"PIKVM_PASS": "{{secret:worker.box.pikvm.password}}"}})
    done = await w.resolve_secret_refs()
    import os
    assert done == ["PIKVM_PASS"] and os.environ["PIKVM_PASS"] == SECRET
    assert sent[0]["args"] == {"worker_id": w.worker_id, "names": ["worker.box.pikvm.password"]}
    monkeypatch.delenv("PIKVM_PASS", raising=False)


