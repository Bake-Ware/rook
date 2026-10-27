"""Settings > Plugins: the band-wide matrix, switches, presets and band
defaults (rook/hub/plugin_admin.py), the account API's gates and the
dashboard proxy."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from tests.plugins_fakes import FakeClient, band_env, make_node, workers_and_plugins
from rook.hub import plugin_profiles as pp
from rook.hub.plugin_admin import PluginAdmin, cell_from_status, plugin_index

ROOT = Path(__file__).resolve().parents[1]


# -- presets and defaults ------------------------------------------------------------

def test_presets_name_real_modules_and_their_risk():
    index = plugin_index()
    android = {p.stem for p in (ROOT / "android/app/src/main/python/rook_android/plugins").glob("*.py")}
    for p in pp.PRESETS:
        for m in p["hub"]:
            assert f"hub:{m}" in index, m
        tiers = set()
        for m in p["worker"]:
            assert f"worker:{m}" in index or m in android, m
            info = index.get(f"worker:{m}")
            tiers |= {c["risk"] for c in info["caps"]} if info else set(pp.ANDROID_RISK[m])
        for m in p["hub"]:
            tiers |= {c["risk"] for c in index[f"hub:{m}"]["caps"]}
        assert set(p["tiers"]) == tiers, p["id"]          # the literal copy is current
        if "exec" in tiers:
            assert p["high_risk"], p["id"]                 # exec is always called out
    assert pp.HUB_ENABLED_SETTINGS == pp.enabled_setting_keys()


@pytest.mark.asyncio
async def test_apply_presets_switches_the_hub_and_stores_band_defaults(tmp_path, monkeypatch):
    enroll, band = band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path, enrollment=enroll)
    await node.start()
    admin = PluginAdmin(node)
    shown = {p["id"]: p for p in admin.presets(band["id"])}
    # Nothing chosen yet: the boxes show the built-in behaviour, whatever is loaded.
    assert all(p["state"] == "default" for p in shown.values())
    assert shown["home"]["selected"] and shown["alerts"]["selected"]
    assert not shown["knowledge"]["selected"]
    # Saving them unchanged changes nothing.
    r = await admin.apply_presets([k for k, p in shown.items() if p["selected"]], None, "human:op")
    assert r["band"] == band["id"] and r["hub"] == [] and r["worker_defaults"] == {}
    assert pp.hub_state(node.settings_store) == {}
    assert node.host.loaded("notify") is not None and node.host.loaded("home") is not None
    # Tick Knowledge, untick Hardware.
    pick = [k for k, p in shown.items() if p["selected"] and k != "hardware"] + ["knowledge"]
    r = await admin.apply_presets(pick, None, "human:op")
    assert node.host.loaded("knowledge") is not None
    assert {x["module"] for x in r["hub"]} >= {"knowledge"}
    defaults = pp.worker_defaults(band["id"], node.settings_store)
    assert defaults["hid"] is False and defaults["memory"] is True and "shell" not in defaults
    assert pp.band_profile(band["id"], node.settings_store)["disable"].count("hid") == 1
    presets = {p["id"]: p for p in admin.presets(band["id"])}
    assert presets["knowledge"]["state"] == "on" and presets["hardware"]["state"] == "off"
    assert presets["remote"]["state"] == "default" and presets["remote"]["selected"]
    assert node.journal.rows[-1]["cap"] == "plugin.presets.apply"
    assert node.journal.rows[-1]["identity"] == "human:op"
    # One default by hand, then back to built-in.
    admin.set_default(band["id"], "hid", True, "human:op")
    assert pp.worker_defaults(band["id"], node.settings_store)["hid"] is True
    admin.set_default(band["id"], "hid", None, "human:op")
    assert "hid" not in pp.worker_defaults(band["id"], node.settings_store)
    for core in ("selfupdate", "info"):
        with pytest.raises(ValueError):
            admin.set_default(band["id"], core, False, "human:op")
    await node.stop()


# -- the matrix ------------------------------------------------------------------------

def test_cells_explain_why_a_plugin_is_not_on():
    assert cell_from_status(True, {})["toggle"] == "disable"
    assert cell_from_status(True, {}, protected=True)["toggle"] is None
    off = cell_from_status(False, {"state": "disabled"})
    assert off["state"] == "off" and off["toggle"] == "enable"
    place = cell_from_status(False, {"state": "not_placed",
                                     "reason": "placement \"app == 'android'\" does not match"})
    assert place["state"] == "unavailable" and place["kind"] == "platform" and place["toggle"] is None
    dep = cell_from_status(False, {"state": "unavailable", "reason": "depends on knowledge (not loaded)"})
    assert dep["kind"] == "dependency"
    err = cell_from_status(True, {"start_error": "boom"})
    assert err["state"] == "error" and err["reason"] == "boom"


@pytest.mark.asyncio
async def test_matrix_toggles_and_band_wide_switches(tmp_path, monkeypatch):
    enroll, band = band_env(tmp_path, monkeypatch)
    workers, plugins = workers_and_plugins()
    client = FakeClient(workers, plugins)
    node = make_node(tmp_path, client=client, enrollment=enroll)
    await node.start()
    admin = PluginAdmin(node)
    m = await admin.matrix()
    cols = {c["id"]: c for c in m["columns"]}
    assert set(cols) == {"hub", "w1", "w2"}
    assert cols["w1"]["can_toggle"] and not cols["w2"]["can_toggle"]
    assert "does not list" in cols["w2"]["error"]            # an older build: no fan-out to it
    assert [c for c in client.calls if c[2] == "w2"] == []
    rows = {r["key"]: r for r in m["rows"]}
    shell = rows["worker:shell"]
    assert shell["cells"]["w1"] == {"state": "on", "toggle": "disable"}
    assert shell["cells"]["hub"]["state"] == "na" and shell["cells"]["w2"]["state"] == "unknown"
    assert shell["risk"]["top"] == "exec" and shell["origin"] == "builtin"
    assert rows["worker:hid"]["cells"]["w1"]["state"] == "off"
    assert rows["worker:pikvm"]["cells"]["w1"]["toggle"] == "enable"
    assert rows["worker:selfupdate"]["protected"] and rows["worker:selfupdate"]["cells"]["w1"]["toggle"] is None
    assert rows["worker:screen"]["origin"] == "android"
    assert rows["worker:screen"]["cells"]["w1"]["kind"] == "platform"
    assert rows["worker:thirdparty"]["origin"] == "entry-point"
    assert rows["worker:thirdparty"]["risk"]["top"] == "exec"   # undeclared counts as exec
    assert rows["worker:camera"]["cells"]["w1"]["state"] == "absent"
    assert rows["hub:knowledge"]["cells"]["hub"]["toggle"] == "enable"
    assert rows["hub:settings"]["protected"]
    assert rows["hub:knowledge"]["settings_link"] == "settings?view=plugin&target=knowledge"
    # The listing is cached; a switch drops the cache for that worker.
    n = len(client.calls)
    await admin.matrix()
    assert len(client.calls) == n
    r = await admin.toggle_worker("box", "hid", True, "human:op")
    assert r["ok"] and client.calls[-1] == ("worker.plugin.enable", {"module": "hid"}, "w1", "human:op")
    m = await admin.matrix()
    assert {r["key"]: r for r in m["rows"]}["worker:hid"]["cells"]["w1"]["state"] == "on"
    assert node.journal.rows[-1]["cap"] == "worker.plugin.enable"
    assert node.journal.rows[-1]["worker"] == "box" and node.journal.rows[-1]["identity"] == "human:op"
    hist = node.settings_store.history(scope="worker", target="box")[0]
    assert hist["key"] == "core.worker.plugins.hid.enabled" and hist["actor"] == "human:op"
    # host.enable's error comes back for the page to show.
    bad = await admin.toggle_worker("box", "pikvm", True, "human:op")
    assert not bad["ok"] and "did not load" in bad["error"]
    assert not (await admin.toggle_worker("box", "selfupdate", False, "human:op"))["ok"]
    with pytest.raises(LookupError):
        await admin.toggle_worker("gone", "shell", False, "human:op")
    every = await admin.toggle_all("shell", False, "human:op")
    assert every["ok"] and [x["target"] for x in every["results"]] == ["box"]
    assert (await admin.reload("box", "shell", "human:op"))["ok"]
    hub = await admin.toggle_hub("notify", False, "human:op")
    assert hub["ok"] and pp.hub_disabled(node.settings_store) == {"notify"}
    await node.stop()


# -- the account API -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_api_is_operator_only_with_csrf_and_journaled(tmp_path, monkeypatch):
    from rook.hub.plugins_web import routes
    enroll, band = band_env(tmp_path, monkeypatch)
    workers, plugins = workers_and_plugins()
    node = make_node(tmp_path, client=FakeClient(workers, plugins), enrollment=enroll)
    await node.start()
    accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                  "admin": c == "op"} if c else None)
    app = Starlette(routes=routes(lambda: node, accounts))
    api = "/plugins/account-api"
    op, member = {"Cookie": "rook_account=op"}, {"Cookie": "rook_account=m1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(api)).status_code == 401
        assert (await c.get(api, headers=member)).status_code == 403
        assert (await c.post(api, headers=member, json={"csrf": "k", "action": "toggle",
                                                        "module": "shell"})).status_code == 403
        page = (await c.get(api, headers=op, params={"view": "matrix"})).json()
        assert page["csrf"] == "k" and page["band"] == band["id"] and page["rows"]
        assert (await c.post(api, headers=op, json={"action": "toggle", "target": "box",
                                                    "module": "shell"})).status_code == 403  # no csrf
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "toggle", "target": "box",
                                                "module": "shell", "enable": False})
        assert r.status_code == 200 and r.json()["ok"]
        assert node.journal.rows[-1]["identity"] == "human:op"
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "toggle", "target": "hub",
                                                "module": "notify", "enable": False})
        assert r.json()["ok"] and node.host.loaded("notify") is None
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "apply_presets",
                                                "presets": ["remote"]})
        assert r.status_code == 200 and r.json()["band"] == band["id"]
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "default", "module": "hid",
                                                "value": True})
        assert r.json()["defaults"]["hid"] is True
        r = await c.post(api, headers=op, json={"csrf": "k", "action": "marketplace_install",
                                                "id": "com.example.weather"})
        assert r.status_code == 501 and r.json()["code"] == "not_implemented"
        cat = (await c.get(api, headers=op, params={"view": "catalog"})).json()
        assert cat["catalogs"][0]["example"] and cat["install"] is False
        presets = (await c.get(api, headers=op, params={"view": "presets"})).json()
        assert [p["id"] for p in presets["presets"]] == list(pp.PRESET_IDS)
        assert (await c.post(api, headers=op, json={"csrf": "k", "action": "toggle"})).status_code == 400
        assert (await c.post(api, headers=op, json={"csrf": "k", "action": "toggle", "target": "gone",
                                                    "module": "x"})).status_code == 404
        assert (await c.post(api, headers=op, json={"csrf": "k", "action": "nope"})).status_code == 400
    await node.stop()


@pytest.mark.asyncio
async def test_dashboard_proxies_the_plugins_api():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from rook.remote.knowledge_web import PluginsWeb
    seen = []

    async def upstream(request):
        seen.append(await request.json() if request.method == "POST" else dict(request.query))
        return web.json_response({"ok": True})
    up = web.Application()
    up.router.add_route("*", "/plugins/account-api", upstream)
    async with TestServer(up) as ts:
        user = {"id": "u", "admin": False, "csrf": "k"}
        account = SimpleNamespace(require=lambda r: user,
                                  csrf=lambda r, d, u: None if d.get("csrf") == "k" else (_ for _ in ()).throw(PermissionError("Form expired")))
        pw = PluginsWeb(account)
        pw.url = str(ts.make_url("/plugins/account-api"))
        app = web.Application()
        pw.install(app)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/account/plugins/api")).status == 403   # operators only
            user["admin"] = True
            assert (await client.get("/account/plugins/api?view=matrix")).status == 200
            assert (await client.post("/account/plugins/api", json={"action": "x"})).status == 403
            assert (await client.post("/account/plugins/api", json={"csrf": "k", "action": "x"})).status == 200
            assert (await client.get("/account/plugins/assets/plugins.js")).status == 200
    assert seen == [{"view": "matrix"}, {"csrf": "k", "action": "x"}]


def test_a_dashboard_login_carries_over_to_the_plugins_api():
    # The Plugins page and the onboarding presets open with a GET on it.
    from rook.remote.bootstrap import CombinedServer
    assert "/account/plugins/api" in CombinedServer._BRIDGE_PATHS


@pytest.mark.asyncio
async def test_band_wide_switches_get_the_long_timeout_in_process_too(monkeypatch):
    """`rook hub serve` answers /account/plugins/api in process (asgi_bridge):
    the proxy's 60 s budget reaches that path, not a shorter default."""
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from rook.remote import upstream
    from rook.remote.knowledge_web import PluginsWeb
    assert PluginsWeb.TIMEOUT == 60
    seen = []
    real = upstream.request_json

    async def spy(method, url, **kw):
        seen.append(kw["timeout"])
        return await real(method, url, **kw)
    monkeypatch.setattr(upstream, "request_json", spy)

    async def slow(request):
        await asyncio.sleep(0.3)
        return JSONResponse({"ok": True})
    inner = Starlette(routes=[Route("/plugins/account-api", slow, methods=["GET", "POST"])])
    unregister = upstream.register_local(["http://127.0.0.1:8765"], inner)
    try:
        user = {"id": "u", "admin": True, "csrf": "k"}
        pw = PluginsWeb(SimpleNamespace(require=lambda r: user, csrf=lambda r, d, u: None))
        pw.url = "http://127.0.0.1:8765/plugins/account-api"
        app = web.Application()
        pw.install(app)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/account/plugins/api")
            assert r.status == 200 and (await r.json()) == {"ok": True}
            monkeypatch.setattr(PluginsWeb, "TIMEOUT", 0.05)
            assert (await client.get("/account/plugins/api")).status == 503
        assert seen == [60, 0.05]
    finally:
        unregister()


@pytest.mark.asyncio
async def test_marketplace_refresh_is_for_admins_and_rate_limited(tmp_path, monkeypatch):
    from rook.hub.authz import current_principal, principal_for_user
    band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path)
    await node.start()
    calls = []
    from rook.hub.plugin_admin import admin_for
    admin = admin_for(node)
    monkeypatch.setattr(admin, "catalog_urls", lambda: ["https://example.com/c.json"])
    admin.catalogs._fetch = lambda url: (calls.append(url), {"name": "c", "source": url,
                                                             "example": False, "plugins": [],
                                                             "errors": []})[1]
    assert (await node.dispatch("plugin.marketplace.list", {}))["ok"] and len(calls) == 1
    # A read-only caller asking for a refresh gets the cached copy.
    assert (await node.dispatch("plugin.marketplace.list", {"refresh": True}))["ok"]
    assert len(calls) == 1
    tok = current_principal.set(principal_for_user(None, True))
    try:
        await node.dispatch("plugin.marketplace.list", {"refresh": True})
        assert len(calls) == 2
        await node.dispatch("plugin.marketplace.list", {"refresh": True})   # within a minute
        assert len(calls) == 2
    finally:
        current_principal.reset(tok)
    await node.stop()


def test_worker_info_is_core_on_the_page():
    assert "info" in pp.PROTECTED_WORKER and "info" in pp.PROTECTED_HUB
    assert pp.normalize_profile({"disable": ["info"]}) is None
