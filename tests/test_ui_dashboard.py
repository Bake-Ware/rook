"""The ui hub plugin: themes, start page layouts, feeds (with the address
guard), the Overview's knowledge data, its account API and the dashboard
proxy (rook/hub/plugins/ui, rook/remote/knowledge_web.py UiWeb)."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette

from rook.hub.plugins.ui import feeds as F
from rook.hub.plugins.ui import layout as L
from rook.hub.plugins.ui import overview as O
from rook.hub.plugins.ui import themes as T

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "rook" / "web"


# -- themes ----------------------------------------------------------------------------

def test_bundled_themes_load_and_cover_every_token():
    themes = T.load()
    assert {"rook-dark", "paper", "graphite", "high-contrast"} <= set(themes)
    assert themes["paper"]["scheme"] == "light"
    for theme in themes.values():
        tokens = T.resolved(theme, themes)
        assert set(T.TOKENS) <= set(tokens), theme["id"]


def test_theme_css_default_matches_the_generated_rule_in_theme_css():
    """theme.css carries rook-dark's :root so pages look right without JS."""
    themes = T.load()
    assert T.css(themes["rook-dark"], themes) in (WEB / "theme.css").read_text(encoding="utf-8")


def test_every_css_variable_the_web_ui_uses_is_defined():
    themes = T.load()
    defined = set(T.TOKENS) | set(T.FONTS) | set(T.ALIASES)
    # Layout variables and per-element ones set inline by the scripts.
    local = {"sidebar-width", "c", "cols", "min"}
    used = set()
    for path in list(WEB.glob("*.css")) + list(WEB.glob("*.js")) + [WEB / "index.html"]:
        if path.name == "rook-scene.js":
            continue
        used |= set(re.findall(r"var\(--([a-z0-9-]+)", path.read_text(encoding="utf-8")))
    assert used - defined - local == set()
    assert T.css(themes["paper"], themes).startswith(":root{color-scheme:light;--bg:")


def test_refactored_stylesheets_have_no_colour_literals():
    literal = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
    for name in ("shell.css", "bands.css", "manage.css", "jobs.css", "settings.css", "sessions.css",
                 "knowledge.css", "work.css", "ui.css"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert not literal.search(text), name
    # theme.css: only its generated :root and the dark 3D scene.
    for line in (WEB / "theme.css").read_text(encoding="utf-8").splitlines():
        if literal.search(line):
            assert line.startswith(":root{color-scheme") or "fleet" in line or "radial-gradient" in line, line


@pytest.mark.parametrize("value", ["red;}body{display:none", "url(https://x.example/a)", "#12345",
                                   "rgb(1,2,3);x", "expression(alert(1))", "<b>", 7])
def test_theme_values_that_could_escape_the_declaration_are_refused(value):
    with pytest.raises(T.ThemeError):
        T.check({"format": T.FORMAT, "id": "bad", "tokens": {"bg": value}})


def test_theme_check_rules():
    ok = T.check({"format": T.FORMAT, "id": "mine", "scheme": "light", "extends": "paper",
                  "tokens": {"accent": "#123abc", "shadow": "rgba(0, 0, 0, .3)", "future-token": "#fff",
                             "sans": "'Atkinson Hyperlegible', sans-serif"}})
    assert "future-token" not in ok["tokens"] and ok["tokens"]["accent"] == "#123abc"
    for bad in ({"id": "x", "tokens": {}}, {"format": T.FORMAT, "id": "Bad Id", "tokens": {}},
                {"format": T.FORMAT, "id": "x", "scheme": "sepia", "tokens": {}},
                {"format": T.FORMAT, "id": "x", "tokens": {"sans": "x;}"}},
                {"format": T.FORMAT, "id": "rook-dark", "tokens": {"bg": "#000"}}):
        with pytest.raises(T.ThemeError):
            T.check(bad)
    themes = T.load()
    mine = dict(ok, id="mine")
    tokens = T.resolved(mine, {**themes, "mine": mine})
    assert tokens["accent"] == "#123abc" and tokens["bg"] == themes["paper"]["tokens"]["bg"]


# -- start page layouts ----------------------------------------------------------------------

def test_default_layout_is_valid_and_round_trips():
    d = L.default()
    assert L.check(d) == d
    assert {w["type"] for col in d["columns"] for w in col} <= set(L.BUILTIN)


def _page(*widgets, columns=None):
    return {"version": 1, "columns": columns or [list(widgets)]}


def test_layout_rejects_unsafe_links_and_bad_shapes():
    bm = lambda url: {"id": "b", "type": "bookmarks", "options": {"groups": [{"name": "g", "links": [{"label": "x", "url": url}]}]}}
    assert L.check(_page(bm("https://a.example")))["columns"][0][0]["options"]["groups"][0]["links"][0]["url"]
    assert L.check(_page(bm("#sessions")))
    for url in ("javascript:alert(1)", "data:text/html,x", "//evil.example", "ftp://x.example", ""):
        with pytest.raises(L.LayoutError):
            L.check(_page(bm(url)))
    with pytest.raises(L.LayoutError):
        L.check({"version": 1, "columns": [[] for _ in range(5)]})
    with pytest.raises(L.LayoutError):
        L.check(_page({"id": "a", "type": "note"}, {"id": "a", "type": "note"}))   # duplicate id
    with pytest.raises(L.LayoutError):
        L.check(_page({"id": "a", "type": "iframe"}))                              # unknown built-in
    with pytest.raises(L.LayoutError):
        L.check(_page({"id": "f", "type": "feed", "options": {"url": "file:///etc/passwd"}}))
    with pytest.raises(L.LayoutError):
        L.check(_page({"id": "f", "type": "tasks", "options": {"limit": 500}}))
    with pytest.raises(L.LayoutError):
        L.check(_page(*[{"id": f"n{i}", "type": "note"} for i in range(L.MAX_WIDGETS + 1)]))
    with pytest.raises(L.LayoutError):
        L.check(_page({"id": "n", "type": "note", "options": {"text": "x" * 9000}}))


def test_plugin_widgets_keep_their_options_and_old_link_lists_become_a_group():
    page = L.check(_page({"id": "p", "type": "weather.forecast", "options": {"city": "Springfield"}},
                         {"id": "b", "type": "bookmarks", "options": {"links": [{"label": "A", "url": "https://a.example"}]}}))
    p, b = page["columns"][0]
    assert p["options"] == {"city": "Springfield"}
    assert b["options"]["groups"] == [{"name": "", "links": [{"label": "A", "url": "https://a.example"}]}]


# -- feeds: the address guard -----------------------------------------------------------------

@pytest.mark.parametrize("ip, public", [
    ("93.184.216.34", True), ("2606:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.10", False), ("172.16.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("::1", False), ("fd00::1", False),
    ("fe80::1", False), ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False), ("0.0.0.0", False),
    ("224.0.0.1", False), ("192.0.2.1", False), ("nonsense", False),
])
def test_feed_addresses(ip, public):
    assert F.address_ok(ip, allow_private=False) is public


def test_allowing_private_addresses_still_refuses_unspecified_and_multicast():
    assert F.address_ok("192.168.1.10", True) and F.address_ok("127.0.0.1", True)
    assert not F.address_ok("0.0.0.0", True) and not F.address_ok("ff02::1", True)


@pytest.mark.parametrize("url", ["ftp://example.com/feed", "file:///etc/passwd", "https://user:pw@example.com/f",
                                 "http://", "javascript:alert(1)", "", "https://example.com:99999/"])
def test_feed_urls_refused(url):
    with pytest.raises(F.FeedError):
        F.check_url(url)


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Homelab Weekly</title>
<link>https://news.example.com/</link>
<item><title>ZFS &amp; you</title><link>https://news.example.com/1</link>
<pubDate>Tue, 13 Oct 2026 10:00:00 -0000</pubDate><description>&lt;p&gt;Pools, &lt;b&gt;explained&lt;/b&gt;.&lt;/p&gt;</description></item>
<item><title>No link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Releases</title><link href="https://releases.example.com/"/>
<entry><title>10.11.2</title><link rel="alternate" href="/notes/10.11.2"/><updated>2026-10-13T10:00:00Z</updated>
<summary>Fixes.</summary></entry></feed>"""


def test_parse_rss_and_atom():
    rss = F.parse(RSS, base="https://news.example.com/feed")
    assert rss["title"] == "Homelab Weekly"
    first, second = rss["items"]
    assert first["title"] == "ZFS & you" and first["summary"] == "Pools, explained ."
    assert first["published"] == 1791885600.0       # -0000 is UTC, not the hub's zone
    assert second["link"] == ""                       # only http(s) links survive
    atom = F.parse(ATOM, base="https://releases.example.com/feed.xml")
    assert atom["items"][0]["link"] == "https://releases.example.com/notes/10.11.2"
    assert atom["items"][0]["published"] == 1791885600.0
    rdf = F.parse(b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">'
                  b'<channel><title>Old</title></channel><item><title>One</title><link>https://o.example/1</link></item></rdf:RDF>')
    assert rdf["title"] == "Old" and rdf["items"][0]["link"] == "https://o.example/1"


def test_parse_refuses_entities_and_non_feeds():
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
    for data in (bomb, b"<html><body>hi</body></html>", b"not xml at all"):
        with pytest.raises(F.FeedError):
            F.parse(data)


@pytest_asyncio.fixture
async def feed_server():
    hits = []

    async def feed(request):
        hits.append(request.path)
        return web.Response(body=RSS, content_type="application/rss+xml")

    async def big(request):
        return web.Response(body=b"<rss>" + b"x" * 200_000 + b"</rss>", content_type="application/rss+xml")

    async def slow(request):
        await asyncio.sleep(3)
        return web.Response(body=RSS)

    async def hop(request):
        raise web.HTTPFound(request.query.get("to", "/feed"))

    app = web.Application()
    app.router.add_get("/feed", feed)
    app.router.add_get("/big", big)
    app.router.add_get("/slow", slow)
    app.router.add_get("/hop", hop)
    async def loop(request):
        raise web.HTTPFound("/loop")

    app.router.add_get("/loop", loop)
    async with TestServer(app, host="127.0.0.1") as server:
        server.hits = hits
        yield server


@pytest.mark.asyncio
async def test_fetch_refuses_loopback_unless_allowed(feed_server):
    url = str(feed_server.make_url("/feed"))
    with pytest.raises(F.FeedError, match="private or local"):
        await F.fetch(url)
    by_name = url.replace("127.0.0.1", "localhost")
    with pytest.raises(F.FeedError, match="private or local"):
        await F.fetch(by_name)                     # through the resolver, not just IP literals
    assert feed_server.hits == []                  # nothing was ever sent
    assert b"Homelab Weekly" in await F.fetch(url, allow_private=True)


@pytest.mark.asyncio
async def test_fetch_size_time_and_redirect_limits(feed_server):
    big = str(feed_server.make_url("/big"))
    with pytest.raises(F.FeedError, match="larger than"):
        await F.fetch(big, allow_private=True, max_bytes=50_000)
    with pytest.raises(F.FeedError, match="longer than"):
        await F.fetch(str(feed_server.make_url("/slow")), allow_private=True, timeout=1)
    with pytest.raises(F.FeedError, match="too many"):
        await F.fetch(str(feed_server.make_url("/loop")), allow_private=True)
    assert b"Homelab" in await F.fetch(str(feed_server.make_url("/hop")), allow_private=True)


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    """A public server that redirects to the metadata address or loopback."""
    calls = []
    real = F.check_url

    def spy(url):
        calls.append(url)
        return real(url)
    monkeypatch.setattr(F, "check_url", spy)

    class Resp:
        status = 302
        headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Session:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def get(self, url, **kw):
            assert "169.254" not in url, "connected to a private address"
            return Resp()
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(F.FeedError, match="private or local"):
        await F.fetch("https://feeds.example.com/rss")
    assert calls == ["https://feeds.example.com/rss", "http://169.254.169.254/latest/meta-data/"]


@pytest.mark.asyncio
async def test_feed_cache_shares_fetches_and_remembers_failures(monkeypatch):
    n = {"ok": 0, "bad": 0}

    async def fake_fetch(url, **kw):
        if "bad" in url:
            n["bad"] += 1
            raise F.FeedError("nope")
        n["ok"] += 1
        return RSS
    monkeypatch.setattr(F, "fetch", fake_fetch)
    cache = F.FeedCache(size=2)
    a = await cache.get("https://a.example/f", ttl=60, limit=1)
    b = await cache.get("https://a.example/f", ttl=60, limit=5)
    assert n["ok"] == 1 and len(a["items"]) == 1 and len(b["items"]) == 2 and b["cached"]
    for _ in range(2):
        with pytest.raises(F.FeedError):
            await cache.get("https://bad.example/f", ttl=60, limit=5)
    assert n["bad"] == 1
    await cache.get("https://c.example/f", ttl=60, limit=5)
    await cache.get("https://d.example/f", ttl=60, limit=5)
    assert len(cache._rows) == 2


# -- the Overview's knowledge half --------------------------------------------------------------

class _Bands:
    def bands(self, active_only=False):
        return [{"id": "b1", "name": "home", "label": "a1b2c3d4", "is_primary": 1}]


AGENT = {"id": "claude.claudecode.laptop", "kind": "agent", "label": "claude"}


async def _seed(tmp_path):
    from rook.hub.plugins.knowledge.service import KnowledgeService
    from rook.hub.plugins.knowledge.hygiene import HygieneEngine
    s = KnowledgeService(tmp_path / "k.db", lambda: AGENT, _Bands())
    s.hygiene = HygieneEngine(s.store)

    async def do(action, rid=None, kind=None, **data):
        return await s.dispatch(action, "b1" if not rid else None, kind, rid, data=data,
                                request_id=uuid.uuid4().hex, actor=AGENT)
    await do("create", kind="knowledge", title="nas", slug="nas", body="Storage.")
    c = await do("create", kind="concept", title="Homelab", slug="homelab", body="x")
    p = await do("create", kind="project", title="Backups", slug="backups", parent=c["id"], body="x")
    t1 = await do("create", kind="task", title="Rotate the key", slug="rotate", parent=p["id"], body="x")
    await s.dispatch("claim", None, None, t1["id"], data={}, request_id=uuid.uuid4().hex, actor=AGENT)
    async def link(ref):
        return await s.dispatch("link", None, None, t1["id"], data={"kind": "chat", "ref": ref},
                                request_id=uuid.uuid4().hex, actor=AGENT)
    await link("room1")
    gone = await link("room2")
    await s.dispatch("retract", None, None, gone["id"], data={}, request_id=uuid.uuid4().hex, actor=AGENT)
    t2 = await do("create", kind="task", title="Back up dns", slug="dns", parent=p["id"], body="x")
    cur = await s.dispatch("get", None, None, t2["id"])
    await s.dispatch("update", None, None, t2["id"], data={"revision": cur["revision"], "patch": {
        "state": "blocked", "attrs": {"blocked_reason": "no SSH key"}}}, request_id=uuid.uuid4().hex, actor=AGENT)
    s.hygiene.raise_("b1", t1["id"], "release_proposed", AGENT["id"], "release [[rotate]]?")
    s.hygiene.raise_("b1", t1["id"], "idle_claim", AGENT["id"], "idle")
    return s, t1, t2


@pytest.mark.asyncio
async def test_overview_build(tmp_path):
    s, t1, t2 = await _seed(tmp_path)
    out = O.build(s)
    [proj] = out["projects"]
    assert proj["project"]["slug"] == "backups"
    assert proj["counts"]["in_progress"] == 1 and proj["counts"]["blocked"] == 1
    assert proj["in_progress"][0]["claimants"] == [AGENT["id"]]
    assert proj["blocked"][0]["blocked_reason"] == "no SSH key"
    assert out["links"][t1["id"]] == [{"kind": "chat", "ref": "room1", "relation": "evidence"}]
    assert [p["slug"] for p in out["recent"]] == ["nas"]
    assert out["recent"][0]["actor"] == AGENT["id"]
    assert [d["kind"] for d in out["decisions"]] == ["release_proposed"]   # idle_claim is for the agent
    assert "[[" not in out["decisions"][0]["text"]


# -- the account API ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_api(tmp_path, monkeypatch):
    from tests.plugins_fakes import band_env, make_node
    from rook.hub.plugins.ui.web import routes
    enroll, _band = band_env(tmp_path, monkeypatch)
    node = make_node(tmp_path, enrollment=enroll)
    await node.start()
    assert node.plugin("ui") is not None
    accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                  "admin": c == "op"} if c else None)
    app = Starlette(routes=routes(lambda: node, accounts))
    api = "/ui/account-api"
    op, member = {"Cookie": "rook_account=op"}, {"Cookie": "rook_account=m1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(api)).status_code == 401
        st = (await c.get(api, headers=member)).json()
        assert st["theme"]["id"] == "rook-dark" and st["theme"]["css"].startswith(":root{")
        assert st["startpage"] == L.default() and {t["id"] for t in st["themes"]} >= {"paper"}
        # Theme: CSRF, known ids only, per account.
        assert (await c.post(api, headers=member, json={"action": "theme", "theme": "paper"})).status_code == 403
        assert (await c.post(api, headers=member, json={"csrf": "k", "action": "theme", "theme": "nope"})).status_code == 400
        r = (await c.post(api, headers=member, json={"csrf": "k", "action": "theme", "theme": "paper"})).json()
        assert r["theme"]["scheme"] == "light"
        assert (await c.get(api, headers=member)).json()["theme"]["id"] == "paper"
        assert (await c.get(api, headers=op)).json()["theme"]["id"] == "rook-dark"
        # Start page: validated, saved per account, reset.
        page = _page({"id": "n", "type": "note", "title": "Hi", "options": {"text": "hello"}})
        r = await c.post(api, headers=member, json={"csrf": "k", "action": "startpage", "layout": page})
        assert r.status_code == 200 and r.json()["startpage"]["columns"][0][0]["options"]["text"] == "hello"
        assert (await c.get(api, headers=member)).json()["startpage"]["columns"][0][0]["id"] == "n"
        bad = _page({"id": "b", "type": "bookmarks", "options": {"groups": [{"links": [{"label": "x", "url": "javascript:x"}]}]}})
        r = await c.post(api, headers=member, json={"csrf": "k", "action": "startpage", "layout": bad})
        assert r.status_code == 400 and "http" in r.json()["error"]
        r = await c.post(api, headers=member, json={"csrf": "k", "action": "startpage_reset"})
        assert r.json()["startpage"] == L.default()
        hist = node.settings_store.history(scope="user", target="m1")
        assert {h["key"] for h in hist} >= {"ui.theme", "ui.startpage"}
        # Feeds: the guard's answer comes back as a plain message.
        r = (await c.post(api, headers=member, json={"csrf": "k", "action": "feed", "url": "http://127.0.0.1:9/f"})).json()
        assert r["ok"] is False and "private or local" in r["error"]
        # The knowledge half is the operator's, and only with the knowledge plugin on.
        assert (await c.get(api, headers=member, params={"view": "overview"})).json()["knowledge"] is None
        assert (await c.get(api, headers=op, params={"view": "overview"})).json() == {"knowledge": None, "reason": "off"}
        assert (await c.post(api, headers=op, json={"csrf": "k", "action": "nope"})).status_code == 400
    await node.stop()


def test_plugin_widgets_are_collected_from_hub_plugins():
    from rook.hub.plugins.ui import DashboardUI
    good = SimpleNamespace(NAMESPACE="weather", WIDGETS=({"type": "weather.now", "title": "Weather",
                                                          "module": "/account/weather/assets/w.js"},))
    bad = SimpleNamespace(NAMESPACE="evil", WIDGETS=({"type": "evil.x", "module": "https://evil.example/x.js"},
                                                    {"type": "nodot", "module": "/x.js"},
                                                    {"type": "evil.y", "module": "//evil.example/y.js"}))
    ui = DashboardUI()
    ui.bind_host(SimpleNamespace(host=SimpleNamespace(plugins=[good, bad, ui])))
    assert ui.widgets() == [{"type": "weather.now", "module": "/account/weather/assets/w.js", "plugin": "weather",
                             "title": "Weather", "description": ""}]


# -- the dashboard side ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_proxy_assets_and_theme_files():
    from rook.remote.knowledge_web import UiWeb
    seen = []

    async def upstream(request):
        seen.append(await request.json() if request.method == "POST" else dict(request.query))
        return web.json_response({"ok": True})
    up = web.Application()
    up.router.add_route("*", "/ui/account-api", upstream)
    async with TestServer(up) as ts:
        user = {"id": "u", "admin": False, "csrf": "k"}
        account = SimpleNamespace(require=lambda r: user,
                                  csrf=lambda r, d, u: None if d.get("csrf") == "k" else (_ for _ in ()).throw(PermissionError("Form expired")))
        ui = UiWeb(account)
        ui.url = str(ts.make_url("/ui/account-api"))
        app = web.Application()
        ui.install(app)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/account/ui/api?view=overview")).status == 200   # any account
            assert (await client.post("/account/ui/api", json={"action": "theme"})).status == 403
            assert (await client.post("/account/ui/api", json={"csrf": "k", "action": "theme"})).status == 200
            for name in UiWeb.ASSETS:
                r = await client.get("/account/ui/assets/" + name)
                assert r.status == 200 and r.headers["X-Content-Type-Options"] == "nosniff", name
            assert (await client.get("/account/ui/assets/index.html")).status == 404
            r = await client.get("/account/ui/themes/paper.json")
            assert r.status == 200 and (await r.json())["id"] == "paper"
            for bad in ("../theme.css", "nope.json", "PAPER.json", "paper.css"):
                assert (await client.get("/account/ui/themes/" + bad)).status == 404, bad
    assert seen == [{"view": "overview"}, {"csrf": "k", "action": "theme"}]


def test_a_dashboard_login_carries_over_to_the_ui_api():
    from rook.remote.bootstrap import CombinedServer
    assert "/account/ui/api" in CombinedServer._BRIDGE_PATHS


def test_index_lands_on_the_overview_and_loads_the_theme_early():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "showView(view||'overview',false)" in html and "if(!viewInfo[v])v='overview'" in html
    head = html[:html.index("</head>")]
    assert head.index("theme.css") < head.index("theme-boot.js")
    for page in ("login.html", "setup.html", "permissions.html", "account-page.html"):
        assert "/account/ui/assets/theme-boot.js" in (WEB / page).read_text(encoding="utf-8"), page
