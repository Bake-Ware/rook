"""Model providers (rook/hub/plugins/llm) and the llmanifold plugin
(rook/hub/plugins/llmanifold.py): a registry of OpenAI-compatible endpoints
by name, llmanifold contributing itself once its address is set, and the
home agent (and so its memory) using one by name."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode
from rook.hub.plugins.llm import Provider, ProviderError, provider_from_row
from rook.hub.settings_store import SettingsStore
from tests.test_home_agent import KEY, FakeLLM, FakeVault

ADMIN_STATUS = {"version": "0.5.4", "reload_error": None, "queue": [{"id": 1}],
                "endpoints": [
                    {"name": "strata-a", "inflight": 1, "lanes": 1, "healthy": True,
                     "model": "flash", "secret_field": "x"},
                    {"name": "deepseek", "inflight": 0, "lanes": 14, "healthy": True,
                     "metered": True}]}


class FakeManifold(FakeLLM):
    """llmanifold: an OpenAI-compatible API plus its admin /api/status."""

    def __init__(self, **kw):
        super().__init__(models=("qwen3.8-flash-next-iq2_xs", "qwopus3.6-35b-a3b-v1-mtp"), **kw)

    async def __call__(self, method, url, body, headers, timeout):
        if url.endswith("/api/status"):
            self.requests.append((method, url, body, headers))
            return 200, ADMIN_STATUS
        return await super().__call__(method, url, body, headers, timeout)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import rook.hub.plugins.settings as settings_plugin
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)
    vault = FakeVault({"llm-key": KEY, "manifold-token": "mf-token-not-real-0001"})
    node = HubNode(str(tmp_path), entry_points=False, vault=vault,
                   chat=ChatStore(str(tmp_path / "chat.db")),
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    node.journal = SimpleNamespace(rows=[], record=lambda **kw: None)
    llm = node.plugin("llm")
    fake = FakeManifold()
    llm._http = fake
    return SimpleNamespace(node=node, llm=llm, fake=fake, vault=vault, svc=node.settings)


async def wait_for(pred, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


# -- the registry ---------------------------------------------------------------------------

def test_provider_rows_are_validated():
    p = provider_from_row({"id": "Lab-GPU", "base_url": "http://192.0.2.5:8080/v1/chat/completions",
                           "api_key": "{{secret:llm-key}}", "models": "a, b, a", "label": "Lab"})
    assert p.id == "lab-gpu" and p.base_url == "http://192.0.2.5:8080/v1" and p.models == ("a", "b")
    d = p.describe()
    assert "base_url" not in d and "api_key" not in d and d["key_set"] and d["host"] == "192.0.2.5:8080"
    for bad in ({"id": "x y", "base_url": "http://a/v1"}, {"id": "x", "base_url": "ftp://a"},
                {"id": "x", "base_url": "http://a/v1", "api_key": "sk-plain-key"}, "nope"):
        with pytest.raises(ProviderError):
            provider_from_row(bad)


@pytest.mark.asyncio
async def test_add_check_and_remove_a_provider_from_the_page(env):
    llm, svc = env.llm, env.svc
    page = await llm.page(svc)
    assert page["providers"] == [] and page["plugins"][0]["module"] == "llmanifold"
    assert "llmanifold address" in page["plugins"][0]["reason"]
    res = await llm.page_action(svc, {"action": "llm_save", "provider": {
        "id": "lab", "base_url": "http://192.0.2.5:8080/v1", "api_key": "{{secret:llm-key}}"}},
        "human:op")
    assert [p["id"] for p in res["providers"]] == ["lab"]
    assert res["providers"][0]["base_url"] == "http://192.0.2.5:8080/v1"   # the operator's page
    check = await llm.check("lab", actor="human:op")
    assert check["ok"] and check["models"] == 2
    # The key went to the provider, from the vault, attributed.
    assert env.fake.requests[-1][3]["Authorization"] == f"Bearer {KEY}"
    assert env.vault.log[-1] == ("llm-key", "human:op", "model providers")
    assert (await llm.page_action(svc, {"action": "llm_models", "provider": "lab"},
                                  "human:op"))["models"][0] == "qwen3.8-flash-next-iq2_xs"
    # Band readers see hosts and models, never the URL or the key reference.
    cap = llm.providers_cap()["providers"][0]
    assert cap["host"] == "192.0.2.5:8080" and "api_key" not in cap and cap["last_check"]["ok"]
    assert "llm-key" not in str(cap)
    res = await llm.page_action(svc, {"action": "llm_delete", "provider": "lab"}, "human:op")
    assert res["providers"] == [] and svc.store.history(key="llm.providers")[0]["actor"] == "human:op"
    with pytest.raises(ProviderError, match="no model provider named 'lab'"):
        llm.client("lab")


@pytest.mark.asyncio
async def test_unreachable_provider_is_reported_not_raised(env):
    env.fake.status = 401
    await env.llm.page_action(env.svc, {"action": "llm_save", "provider": {
        "id": "lab", "base_url": "http://192.0.2.5:8080/v1", "api_key": "{{secret:llm-key}}"}}, "op")
    check = await env.llm.check("lab")
    assert check["ok"] is False and "HTTP 401" in check["error"] and KEY not in check["error"]
    out = await env.llm.models_cap("nothing-here")
    assert "no model provider" in out["error"]


# -- llmanifold -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llmanifold_loads_when_its_address_is_set_and_contributes_itself(env):
    node, svc = env.node, env.svc
    await node.start()
    try:
        assert node.plugin("llmanifold") is None
        assert "llmanifold address" in node.host.status["llmanifold"]["reason"]
        svc.set("llmanifold.base_url", "http://192.0.2.64:1234/v1", actor="human:op")
        assert await wait_for(lambda: node.plugin("llmanifold") is not None)
        lm = node.plugin("llmanifold")
        lm._http = env.fake
        svc.set("llmanifold.api_key", "{{secret:manifold-token}}", actor="human:op")
        svc.set("llmanifold.admin_url", "http://192.0.2.64:1240", actor="human:op")
        p = env.llm.get("llmanifold")
        assert p is not None and p.kind == "llmanifold" and p.source == "plugin:llmanifold"
        assert p.base_url == "http://192.0.2.64:1234/v1" and p.api_key == "{{secret:manifold-token}}"
        # A hand-made provider cannot take its name.
        with pytest.raises(ProviderError, match="belongs to a plugin"):
            await env.llm.page_action(svc, {"action": "llm_save", "provider": {
                "id": "llmanifold", "base_url": "http://192.0.2.9/v1"}}, "human:op")
        st = await lm.status_cap()
        assert st["ok"] and st["api"]["flow_count"] == 2
        assert st["admin"]["version"] == "0.5.4" and st["admin"]["inflight"] == 1
        assert st["admin"]["queued"] == 1
        assert "secret_field" not in st["admin"]["endpoints"][0]
        assert env.fake.requests[-2][3]["Authorization"] == "Bearer mf-token-not-real-0001"
        check = await env.llm.check("llmanifold")
        assert check["ok"] and check["detail"]["admin"]["endpoints"][1]["name"] == "deepseek"
        # Offered flows narrow what models reports (without asking it).
        svc.set("llmanifold.models", ["qwen3.8-flash-next-iq2_xs"], actor="human:op")
        n = len(env.fake.requests)
        assert await env.llm.models("llmanifold") == ["qwen3.8-flash-next-iq2_xs"]
        assert len(env.fake.requests) == n
        # Switched off: the provider goes with it.
        await node.set_plugin_enabled("llmanifold", False, "human:op")
        assert env.llm.get("llmanifold") is None
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_llmanifold_admin_unreachable_keeps_the_api_status(env):
    node, svc = env.node, env.svc
    await node.start()
    svc.set("llmanifold.admin_url", "http://192.0.2.64:1240", actor="human:op")
    svc.set("llmanifold.base_url", "http://192.0.2.64:1234/v1", actor="human:op")
    try:
        assert await wait_for(lambda: node.plugin("llmanifold") is not None)
        lm = node.plugin("llmanifold")

        async def no_admin(method, url, body, headers, timeout):
            if "/api/status" in url:
                raise OSError("connection refused")
            return await env.fake(method, url, body, headers, timeout)
        lm._http = env.llm._http = no_admin
        st = await lm.status()
        assert st["ok"] and st["admin"] == {"ok": False, "error": "cannot reach 192.0.2.64:1240: OSError"}
    finally:
        await node.stop()


# -- the home agent by provider name ---------------------------------------------------------

@pytest.mark.asyncio
async def test_home_agent_uses_a_provider_by_name(env):
    node, svc, llm = env.node, env.svc, env.llm
    home = node.plugin("home")
    home._http = env.fake
    await llm.page_action(svc, {"action": "llm_save", "provider": {
        "id": "lab", "base_url": "http://192.0.2.5:8080/v1", "api_key": "{{secret:llm-key}}",
        "default_model": "qwen3.8-flash-next-iq2_xs"}}, "human:op")
    for k, v in {"enabled": True, "llm_provider": "lab",
                 "base_url": "http://ignored.example:1/v1"}.items():
        svc.set(f"home.{k}", v, actor="human:op")
    cfg = home.config()
    # Everything that reads the home agent's endpoint (its memory too) follows the provider.
    assert cfg["base_url"] == "http://192.0.2.5:8080/v1" and cfg["api_key"] == "{{secret:llm-key}}"
    assert cfg["model"] == "qwen3.8-flash-next-iq2_xs" and home.missing() == []
    assert home.status()["llm_provider"] == "lab" and home.status()["endpoint_host"] == "192.0.2.5:8080"
    page = home.page(svc)
    assert [p["id"] for p in page["llm_providers"]] == ["lab"]
    res = await home.page_action(svc, {"action": "home_test", "values": {"llm_provider": "lab"}},
                                 "human:op")
    assert res["ok"], res
    method, url, body, headers = env.fake.chats()[-1]
    assert url == "http://192.0.2.5:8080/v1/chat/completions" and headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "qwen3.8-flash-next-iq2_xs"
    # The provider disappears: the agent says what is missing, never falls back.
    await llm.page_action(svc, {"action": "llm_delete", "provider": "lab"}, "human:op")
    assert home.missing() == ["llm_provider", "model"] or home.missing()[0] == "llm_provider"
    from rook.hub.plugins.home import HomeError
    with pytest.raises(HomeError, match="model provider 'lab' is not available"):
        home.client()


def test_provider_dataclass_is_what_plugins_contribute():
    p = Provider(id="x", base_url="http://192.0.2.1/v1", source="plugin:y")
    assert p.describe()["source"] == "plugin:y" and p.host == "192.0.2.1"


# -- review fixes: credentials in addresses, band callers ------------------------------------

def test_credentials_in_an_address_are_refused_and_never_shown():
    from rook.hub.plugins.home.llm import ChatClient, url_host
    with pytest.raises(ProviderError, match="user name or password"):
        provider_from_row({"id": "lab", "base_url": "http://bob:hunter2@192.0.2.5:8080/v1"})
    # Wherever an address with them still shows up, only host:port is shown.
    assert url_host("http://bob:hunter2@192.0.2.5:8080/v1") == "192.0.2.5:8080"
    assert url_host("https://u@[2001:db8::1]:443/v1") == "[2001:db8::1]:443"
    assert Provider(id="x", base_url="http://u:p@192.0.2.1/v1").host == "192.0.2.1"
    assert ChatClient("http://u:p@192.0.2.1:9/v1").host == "192.0.2.1:9"


@pytest.mark.asyncio
async def test_llmanifold_refuses_an_address_with_credentials(env):
    node, svc = env.node, env.svc
    await node.start()
    try:
        svc.set("llmanifold.base_url", "http://bob:hunter2@192.0.2.64:1234/v1", actor="human:op")
        await asyncio.sleep(0.1)
        assert node.plugin("llmanifold") is None
        reason = node.host.status["llmanifold"]["reason"]
        assert "user name or password" in reason and "hunter2" not in reason
        svc.set("llmanifold.base_url", "http://192.0.2.64:1234/v1", actor="human:op")
        assert await wait_for(lambda: node.plugin("llmanifold") is not None)
        lm = node.plugin("llmanifold")
        lm._http = env.fake
        svc.set("llmanifold.admin_url", "http://bob:hunter2@192.0.2.64:1240", actor="human:op")
        st = await lm.status()
        assert "user name or password" in st["admin"]["error"] and "hunter2" not in str(st)
        # Band readers share one answer for a few seconds.
        n = len(env.fake.requests)
        assert (await lm.status_cap()) is lm.last_status
        assert len(env.fake.requests) == n
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_band_checks_are_the_callers_and_rate_limited(env, monkeypatch):
    from rook.core import context
    import rook.hub.plugins.llm as llm_mod
    llm, svc = env.llm, env.svc
    await llm.page_action(svc, {"action": "llm_save", "provider": {
        "id": "lab", "base_url": "http://192.0.2.5:8080/v1", "api_key": "{{secret:llm-key}}"}},
        "human:op")
    monkeypatch.setattr(llm_mod, "CALLS_PER_MINUTE", 2)
    tok = context.caller_identity.set("agent:reader")
    try:
        assert (await llm.check_cap("lab"))["checks"][0]["ok"]
        # The vault read is logged as the band caller, not as the hub.
        assert env.vault.log[-1][:2] == ("llm-key", "agent:reader")
        assert (await llm.models_cap("lab", live=True))["models"]
        assert "too many" in (await llm.check_cap("lab"))["error"]
        assert "too many" in (await llm.models_cap("lab"))["error"]
    finally:
        context.caller_identity.reset(tok)
    # Another caller has its own allowance.
    tok = context.caller_identity.set("agent:other")
    try:
        assert (await llm.check_cap("lab"))["checks"][0]["ok"]
    finally:
        context.caller_identity.reset(tok)
