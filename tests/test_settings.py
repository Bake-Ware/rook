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
from rook.hub.settings_schema import Schema
from rook.hub.settings_service import SettingsError, SettingsService
from rook.hub.settings_store import SettingsStore

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


def _svc(tmp_path, environ=None, setup=None, **kw):
    store = SettingsStore(tmp_path / "settings.db")
    return SettingsService(store, Schema(), environ=environ if environ is not None else {},
                           setup_loader=lambda: dict(setup or {}), **kw)


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

def test_store_rows_history_and_runtime(tmp_path):
    st = SettingsStore(tmp_path / "s.db")
    assert st.get("k", "hub") is None and not st.exists()     # reads never create the file
    st.set("core.hub.domain", "hub", "", value="a.example", actor="human:alice", note="first")
    st.set("core.hub.domain", "hub", "", value="b.example", actor="agent:ci")
    with pytest.raises(ValueError):
        st.set("core.hub.domain", "hub", "", value="c", expect_rev=1)
    assert st.get("core.hub.domain", "hub")["value"] == "b.example"
    h = st.history(key="core.hub.domain")
    assert [(x["actor"], x["old"], x["new"]) for x in h] == [
        ("agent:ci", "a.example", "b.example"), ("human:alice", None, "a.example")]
    assert st.delete("core.hub.domain", "hub", actor="human:alice")
    assert st.history(key="core.")[0]["new"] is None
    st.report_runtime("dashboard", {"env": {"core.hub.domain": {"env": "ROOK_DOMAIN", "value": "x"}}})
    assert st.runtime("dashboard")["env"]["core.hub.domain"]["env"] == "ROOK_DOMAIN"
    assert (tmp_path / "s.db").stat().st_mode & 0o077 == 0


# -- the service -------------------------------------------------------------------

def test_schema_declares_the_inventory():
    schema = Schema()
    for key in ("core.hub.domain", "core.hub.public_relay", "core.band.key", "core.mcp.listen",
                "core.worker.announce_interval", "voice.whisper_model", "voice.default_voice",
                "decision.timeout_ms", "knowledge.enabled", "watchdog.telegram_token",
                "agent.wake_command", "memory.notes_dir", "pikvm.password",
                "core.settings.service_readers"):
        assert schema.get(key) is not None, key
    assert schema.get("pikvm.password").setting.secret
    assert "ROOK_WAKE_CMD" in schema.worker_env_names()
    assert "PIKVM_PASS" not in schema.worker_env_names()
    assert schema.get("voice.default_voice").setting.overridable == ("user",)


def test_set_reset_history_and_validation(tmp_path):
    svc = _svc(tmp_path)
    res = svc.set("core.hub.domain", "rook.example.com", actor="human:alice", note="new host")
    assert res["ok"] and res["effective"]["value"] == "rook.example.com"
    assert res["effective"]["source"] == "hub"
    with pytest.raises(SettingsError):
        svc.set("core.worker.announce_interval", 1, scope="band", target="nope")  # below min / no band
    with pytest.raises(SettingsError, match="read before the settings store"):
        svc.set("core.mcp.listen", "0.0.0.0:1")
    with pytest.raises(SettingsError, match="Bands page"):
        svc.set("core.band.key", "x", scope="band", target="b")
    with pytest.raises(SettingsError, match="can be set at"):
        svc.set("core.hub.domain", "x", scope="worker", target="w")
    with pytest.raises(SettingsError, match="unknown setting"):
        svc.set("core.nope", 1)
    dry = svc.set("core.hub.band_name", "lab", dry_run=True)
    assert dry["dry_run"] and svc.store.get("core.hub.band_name", "hub") is None
    out = svc.reset("core.hub.domain", actor="human:alice")
    assert out["removed"] and out["effective"]["source"] == "default"
    hist = svc.store.history(key="core.hub.domain")
    assert [h["actor"] for h in hist] == ["human:alice", "human:alice"] and hist[1]["note"] == "new host"


def test_secrets_go_to_the_vault_and_never_come_back(tmp_path):
    vault = FakeVault()
    svc = _svc(tmp_path, vault=vault)
    res = svc.set("voice.token", SECRET, actor="human:alice")
    assert vault.data == {"plugin.voice.token": SECRET}
    assert SECRET not in json.dumps(res)
    row = svc.store.get("voice.token", "hub")
    assert row["value"] is None and row["secret_ref"] == "plugin.voice.token"
    assert SECRET not in (tmp_path / "settings.db").read_bytes().decode("latin-1")
    hist = svc.store.history(key="voice.token")[0]
    assert hist["new"] == "fp:" + cs.fingerprint(SECRET)
    page = svc.plugin_page("voice")
    assert SECRET not in json.dumps(page)
    svc.reset("voice.token", actor="human:alice")
    assert vault.data == {}


def test_env_wins_locks_and_reports_a_conflict(tmp_path):
    svc = _svc(tmp_path, environ={"ROOK_MCP_BIND": "0.0.0.0:9000"})
    r = svc.resolve("core.mcp.listen")
    assert r["locked"] and r["env"] == "ROOK_MCP_BIND" and r["value"] == "0.0.0.0:9000"
    # A hub plugin key: stored value, then the env var hides it.
    svc.set("core.settings.service_readers", {"voice": ["voice"]})
    svc.environ["ROOK_CORE_SETTINGS_SERVICE_READERS"] = "{}"   # core keys have no canonical env
    assert not svc.resolve("core.settings.service_readers")["locked"]


def test_dashboard_keys_use_the_dashboard_report_and_setup_file(tmp_path):
    svc = _svc(tmp_path, setup={"pyz_domain": "file.example.com", "hub_public": ""})
    r = svc.resolve("core.hub.domain")
    assert (r["source"], r["value"]) == ("file", "file.example.com")
    svc.set("core.hub.domain", "ui.example.com")
    assert svc.resolve("core.hub.domain")["value"] == "ui.example.com"   # a saved value beats the file
    svc.store.report_runtime("dashboard", {"env": {"core.hub.domain": {"env": "ROOK_DOMAIN",
                                                                       "value": "env.example.com"}}})
    r = svc.resolve("core.hub.domain")
    assert r["locked"] and r["value"] == "env.example.com"
    assert "ROOK_DOMAIN" in r["conflict"]["note"]
    assert any(c["key"] == "core.hub.domain" for c in svc.conflicts())


def test_restart_pending_flag(tmp_path):
    svc = _svc(tmp_path, started_at=1.0)
    svc.set("voice.whisper_model", "base.en")          # apply=reload: no restart flag
    assert "pending" not in svc.resolve("voice.whisper_model")
    svc.store.report_runtime("service:voice", {"started_at": 1.0})
    svc.set("voice.acp_port", 9300)                    # live
    assert "pending" not in svc.resolve("voice.acp_port")
    svc.set("voice.allow_anonymous", True)             # apply=restart, service started before
    assert svc.resolve("voice.allow_anonymous")["pending"] == "restart"


def _worker_node(caps=("worker.plugin.list", "worker.config_get", "worker.settings_report")):
    client = FakeClient(workers={"w1": {"name": "box", "caps": list(caps), "band": "*"}})
    return SimpleNamespace(client=client, worker_id="hub")


def test_worker_delivery_band_defaults_overrides_and_secret_refs(tmp_path):
    vault = FakeVault()
    node = _worker_node()
    svc = _svc(tmp_path, vault=vault, node=node)
    svc.set("core.worker.log_level", "info", scope="worker", target="box")
    svc.set("agent.wake_command", "claude -p {prompt_file}", scope="worker", target="box")
    svc.set("pikvm.password", SECRET, scope="worker", target="box")
    svc.set("pikvm.insecure", False, scope="worker", target="box")
    settings, problems = svc.delivery("box", typed=True)
    assert not problems
    assert settings["log_level"] == "info"
    env = settings["env"]
    assert env["ROOK_WAKE_CMD"] == "claude -p {prompt_file}" and env["PIKVM_INSECURE"] == "0"
    assert env["PIKVM_PASS"] == "{{secret:worker.box.pikvm.password}}"
    assert SECRET not in json.dumps(settings)
    # An older build cannot fetch at use: the secret is refused, not written to its disk.
    _, problems = svc.delivery("box", typed=False)
    assert problems and "pikvm.password" in problems[0]
    # The worker may fetch its own secret, and nothing else.
    got = svc.worker_secrets("w1", ["worker.box.pikvm.password", "plugin.voice.token"])
    assert got["secrets"] == {"worker.box.pikvm.password": SECRET}
    assert got["missing"] == ["plugin.voice.token"]
    # A key removed since the last delivery is sent as None (unset on the worker).
    svc.store.report_runtime("worker:box", {"env_keys": ["ROOK_MEMORY_VAULT"],
                                            "config_keys": ["announce_interval", "log_level"]})
    settings, _ = svc.delivery("box", typed=True)
    assert settings["env"]["ROOK_MEMORY_VAULT"] is None
    assert settings["announce_interval"] is None and settings["log_level"] == "info"


@pytest.mark.asyncio
async def test_apply_worker_is_commit_confirmed_and_masked(tmp_path, monkeypatch):
    node = _worker_node()
    epochs = {}

    def apply(args):
        epochs["e"] = args["epoch"]
        return {"ok": True, "result": {"ok": True}}

    node.client.replies = {
        "worker.config_apply": apply,
        "worker.config_get": lambda a: {"ok": True, "result": {"epoch": epochs.get("e"),
                                                               "config": {"psk": SECRET, "env": {"PIKVM_PASS": SECRET}}}},
        "worker.config_confirm": {"ok": True, "result": {"ok": True}},
    }
    svc = _svc(tmp_path, vault=FakeVault(), node=node)
    svc.set("core.worker.announce_interval", 45, scope="worker", target="box")
    import rook.hub.worker_config as wc

    async def fast_sleep(_):
        return None
    monkeypatch.setattr(wc.asyncio, "sleep", fast_sleep)
    res = await svc.apply_worker("box", "human:alice", wait=True)
    assert res["ok"] and res["job"]["state"] == "done"
    assert SECRET not in json.dumps(res["job"]["result"])
    assert [c[0] for c in node.client.calls] == ["worker.config_apply", "worker.config_get",
                                                 "worker.config_confirm"]
    assert node.client.calls[0][1]["settings"] == {"announce_interval": 45}
    assert svc.store.runtime("worker:box")["config_keys"] == ["announce_interval"]


@pytest.mark.asyncio
async def test_plugin_toggle_is_recorded(tmp_path):
    node = _worker_node()
    node.client.replies = {"worker.plugin.enable": {"ok": True, "result": {"ok": True}}}
    svc = _svc(tmp_path, node=node)
    res = await svc.plugin_toggle("box", "pikvm", True, "human:alice")
    assert res["ok"]
    h = svc.store.history(scope="worker", target="box")[0]
    assert h["key"] == "core.worker.plugins.pikvm.enabled" and h["new"] is True


def test_service_fetch_is_gated_to_listed_tokens(tmp_path):
    vault = FakeVault()
    svc = _svc(tmp_path, vault=vault)
    svc.set("voice.token", SECRET)
    svc.set("voice.whisper_model", "base.en")
    svc.set("voice.default_voice", "bf_emma", scope="user", target="u1")
    voice = {"kind": "agent", "label": "voice", "agent_id": "agent_v", "verified": True}
    with pytest.raises(PermissionError):
        svc.fetch("voice", voice)
    svc.set("core.settings.service_readers", {"voice": ["agent_v"]})
    got = svc.fetch("voice", voice)
    assert got["values"]["token"] == SECRET and got["values"]["whisper_model"] == "base.en"
    assert got["values"]["min_speech_ms"] == 450 and got["users"] == {"u1": {"default_voice": "bf_emma"}}
    with pytest.raises(PermissionError):
        svc.fetch("voice", {**voice, "agent_id": "agent_other", "label": "other"})
    with pytest.raises(PermissionError):
        svc.fetch("voice", {"kind": "shared", "label": "voice"})
    with pytest.raises(PermissionError):
        svc.fetch("decision", voice)   # listed for voice only


# -- hub node: caps, live refresh, sensitive journaling --------------------------

@pytest.mark.asyncio
async def test_hub_caps_and_live_refresh(tmp_path, monkeypatch):
    monkeypatch.delenv("ROOK_HUB_MOTD", raising=False)
    from rook.hub.node import HubNode
    import rook.hub.plugins.settings as settings_plugin
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)  # in-process caller
    vault = FakeVault()
    node = HubNode(str(tmp_path), entry_points=False, vault=vault,
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    assert {"settings.get", "settings.set", "settings.fetch", "settings.worker_secret"} <= set(node.caps())
    assert node.host.registry.meta("settings.set").risk == "admin"
    assert "sensitive" in node.host.registry.meta("settings.fetch").tags
    res = await node.dispatch("settings.set", {"key": "hub.motd", "value": "hello"}, "agent:ops")
    assert res["ok"], res
    info = await node.dispatch("hub.info", {})
    assert info["result"]["motd"] == "hello"          # live: the plugin's view was refreshed
    got = await node.dispatch("settings.get", {"key": "hub.motd"})
    assert got["result"]["source"] == "hub"
    hist = await node.dispatch("settings.history", {"key": "hub.motd"})
    assert hist["result"][0]["actor"] == "agent:ops"
    # Over the band only read caps pass.
    denied = await node.dispatch("settings.set", {"key": "hub.motd", "value": "x"}, source="band")
    assert not denied["ok"]


@pytest.mark.asyncio
async def test_sensitive_band_reply_is_not_journaled(tmp_path, monkeypatch):
    from rook.hub.node import attach_hub_node
    records = []
    journal = SimpleNamespace(record=lambda **kw: records.append(kw))
    vault = FakeVault()

    class Client:
        workers = {"w1": {"name": "box", "caps": [], "band": "*"}}

        def attach_local(self, node):
            self.node = node

    monkeypatch.setenv("ROOK_SETTINGS_DB", str(tmp_path / "settings.db"))
    node = attach_hub_node(Client(), str(tmp_path), vault=vault, journal=journal)
    node.settings.set("pikvm.password", SECRET, scope="worker", target="box")
    reply = await node.dispatch("settings.worker_secret",
                                {"worker_id": "w1", "names": ["worker.box.pikvm.password"]},
                                "worker:box", source="band")
    assert reply["ok"] and reply["result"]["secrets"]["worker.box.pikvm.password"] == SECRET
    assert SECRET not in json.dumps(records)


# -- account API -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_api_operator_and_member(tmp_path):
    from rook.hub.settings_web import routes
    svc = _svc(tmp_path, vault=FakeVault())
    accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                  "admin": c == "op"} if c else None)
    app = Starlette(routes=routes(lambda: svc, accounts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        api = "/settings/account-api"
        assert (await c.get(api)).status_code == 401
        op, member = {"Cookie": "rook_account=op"}, {"Cookie": "rook_account=m1"}
        page = (await c.get(api, headers=op, params={"view": "hub"})).json()
        assert page["groups"] and page["csrf"] == "k"
        assert (await c.get(api, headers=member, params={"view": "hub"})).status_code == 403
        assert (await c.post(api, headers=op, json={"action": "set", "key": "core.hub.domain",
                                                    "value": "x"})).status_code == 403  # no csrf
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "set",
                                                "key": "core.hub.domain", "value": "a.example"})
        assert r.status_code == 200 and svc.store.history()[0]["actor"] == "human:op"
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "set",
                                                "key": "voice.token", "value": SECRET})
        assert SECRET not in r.text
        # A member may change only their own preferences.
        r = await c.post(api, headers=member, json={"csrf": "k", "action": "set",
                                                    "key": "core.hub.domain", "value": "evil"})
        assert r.status_code == 403
        r = await c.post(api, headers=member, json={"csrf": "k", "action": "set", "scope": "user",
                                                    "target": "op", "key": "voice.show_thinking",
                                                    "value": True})
        assert r.status_code == 200 and svc.store.get("voice.show_thinking", "user", "m1")["value"]
        assert svc.store.get("voice.show_thinking", "user", "op") is None
        mine = (await c.get(api, headers=member, params={"view": "user"})).json()
        rows = {r["key"]: r for g in mine["groups"] for r in g["rows"]}
        assert rows["voice.show_thinking"]["value"] is True
        bad = await c.post(api, headers=op, json={"csrf": "k", "action": "set",
                                                  "key": "voice.acp_port", "value": 0})
        assert bad.status_code == 400
        found = (await c.get(api, headers=op, params={"view": "search", "q": "ROOK_KNOWLEDGE"})).json()
        assert any(x["key"] == "knowledge.enabled" for x in found["results"])
        over = (await c.get(api, headers=op)).json()
        assert {"bands", "workers", "plugins", "conflicts"} <= set(over)


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


@pytest.mark.asyncio
async def test_combined_server_reports_and_follows_the_mcp_chat_db(tmp_path, monkeypatch):
    from rook.remote import setup_store
    from rook.remote.bootstrap import CombinedServer
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.delenv("ROOK_CHAT_DB", raising=False)
    monkeypatch.setenv("ROOK_DOMAIN", "env.example.com")
    setup_store.save({"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
                      "hub_public": "hub.example.com:443", "pyz_domain": "file.example.com"})
    store = SettingsStore(tmp_path / "settings.db")
    mcp_chat = tmp_path / "mcp" / "chat.db"
    mcp_chat.parent.mkdir()
    mcp_chat.write_bytes(b"")
    store.report_runtime("mcp", {"stores": {"chat_db": str(mcp_chat)}})
    server = CombinedServer(band_psk="alpha-bravo-charlie-delta-echo", domain="env.example.com",
                            explicit={"domain": "env ROOK_DOMAIN"})
    try:
        assert server.domain == "env.example.com"               # env beats setup.json now
        assert server._chat_db == str(mcp_chat)                 # same rooms as the MCP
        assert server._setting_conflicts[0]["key"] == "core.hub.domain"
        server._report_settings()
        rep = store.runtime("dashboard")
        assert rep["env"]["core.hub.domain"]["env"] == "ROOK_DOMAIN"
        assert rep["conflicts"][0]["hidden"] == "setup.json"
        # A Settings-page change to a key the environment does not set applies live.
        store.set("core.hub.band_name", "hub", "", value="renamed")
        server._refresh_settings()
        assert server.band_name == "renamed"
    finally:
        if server._chat:
            server._chat.close()


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


def test_masked_reply_for_config_get():
    from rook.hub.worker_config import masked_reply
    reply = {"ok": True, "result": {"ok": True, "config": {"psk": SECRET, "env": {"X_PASS": SECRET}}}}
    assert SECRET not in json.dumps(masked_reply(reply))


@pytest.mark.asyncio
async def test_writes_need_hub_admin(tmp_path, monkeypatch):
    """settings.set/reset/apply_worker call rook.hub.authz.require_hub_admin."""
    import sys
    import types
    fake = types.ModuleType("rook.hub.authz")
    fake.require_hub_admin = lambda what: f"denied: {what} is for band owners and operator tokens"
    monkeypatch.setitem(sys.modules, "rook.hub.authz", fake)
    from rook.hub.node import HubNode
    node = HubNode(str(tmp_path), entry_points=False, vault=FakeVault(),
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    res = await node.dispatch("settings.set", {"key": "hub.motd", "value": "x"}, "agent:ci")
    assert not res["ok"] and "denied" in res["error"]
    assert node.settings_store.get("hub.motd", "hub") is None
    dry = await node.dispatch("settings.set", {"key": "hub.motd", "value": "x", "dry_run": True})
    assert dry["ok"]                                   # a dry run changes nothing
    assert not (await node.dispatch("settings.reset", {"key": "hub.motd"}))["ok"]
    assert (await node.dispatch("settings.get", {"key": "hub.motd"}))["ok"]


def test_settings_reference_is_current():
    """docs/operations/settings-reference.md is generated from the schema:
    python tools/gen_settings_reference.py"""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("gen_settings_reference",
                                                  root / "tools" / "gen_settings_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TARGET.read_text(encoding="utf-8") == mod.render(), \
        "stale: run python tools/gen_settings_reference.py"
