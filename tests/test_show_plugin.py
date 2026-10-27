"""show.present: a short spoken line plus the subject on a screen (hub plugin).

Fakes only: no real phone, TV or voice is ever called."""
import asyncio
import json
import re
import time

import pytest

from rook.hub.authz import current_principal
from rook.hub.plugins.show import Show, render
from rook.hub.policy import Principal


class FakeClient:
    def __init__(self, workers, replies=None):
        self.workers = workers
        self.calls = []
        self.replies = replies or {}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        self.calls.append((cap, target, dict(args or {}), current_principal.get()))
        reply = self.replies.get((cap, target))
        if isinstance(reply, Exception):
            raise reply
        return reply or {"ok": True, "result": {"ok": True}}


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakePlugin:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeNode:
    def __init__(self, client, plugins=None):
        self.client = client
        self.worker_id = "hub1"
        self.settings_store = None
        self._plugins = plugins or {}

    def plugin(self, name):
        return self._plugins.get(name)


def worker(name, *caps, age=1.0):
    return {"name": name, "caps": list(caps), "last_seen": time.time() - age}


PHONE = ("device.open_url", "device.launch", "voice.speak")
TV = ("tv.open_url", "tv.launch")


def make(tmp_path, monkeypatch, workers, *, brief_phone="", protected=(), screens=None,
         replies=None, plugins=None):
    client = FakeClient(workers, replies)
    extra = dict(plugins or {})
    extra.setdefault("home", FakePlugin(settings=FakeSettings(brief_phone=brief_phone)))
    extra.setdefault("role", FakePlugin(protected=lambda: list(protected)))
    p = Show()
    p.__dict__["_data_root"] = str(tmp_path)
    p.bind_host(FakeNode(client, extra))
    if screens is not None:
        p.settings.refresh({"screens": screens})
    monkeypatch.setattr(Show, "_base_url", lambda self: "https://hub.test")
    return p, client


def run(coro):
    return asyncio.run(coro)


def test_diagram_opens_a_show_page_on_the_owners_phone_and_speaks_there(tmp_path, monkeypatch):
    p, client = make(tmp_path, monkeypatch, {"p1": worker("bakephone", *PHONE),
                                             "p2": worker("kidphone", *PHONE),
                                             "tv1": worker("office-tv", *TV)},
                     brief_phone="bakephone", protected=("kidphone",))
    out = run(p.present(say="Here is the plan.", diagram="graph TD; A-->B", title="Plan"))
    assert out["shown"] and out["spoken"] and out["screen"] == "bakephone" == out["voice"]
    assert out["kind"] == "mermaid" and re.match(r"^https://hub\.test/show/[A-Za-z0-9_-]{32}$",
                                                 out["url"])
    assert [(c[0], c[1]) for c in client.calls] == [("device.open_url", "p1"), ("voice.speak", "p1")]
    assert client.calls[0][2] == {"url": out["url"]}
    assert client.calls[1][2] == {"text": "Here is the plan.", "wait": False}
    assert "unsaid" not in out
    html = p.page_html(out["id"], "n0nce")
    assert "graph TD; A--&gt;B" in html and render.MERMAID_SRC in html and 'nonce="n0nce"' in html
    # A restart reads the same page from disk.
    again = Show()
    again.__dict__["_data_root"] = str(tmp_path)
    assert again.page(out["id"])["title"] == "Plan"


def test_auto_never_picks_a_tv_or_a_protected_phone_and_refuses_to_guess(tmp_path, monkeypatch):
    workers = {"tv1": worker("office-tv", *TV), "k": worker("kidphone", *PHONE)}
    p, client = make(tmp_path, monkeypatch, workers, protected=("kidphone",))
    out = run(p.present(say="Look.", markdown="# Hi"))
    assert not out["shown"] and not out["spoken"] and out["unsaid"] == "Look."
    assert client.calls == []
    assert any("no screen is online" in n for n in out["notes"])
    assert any("link is saved" in n for n in out["notes"])
    assert p.list()["shows"][0]["id"] == out["id"]           # the link is kept
    # Two unprotected phones and nothing says which is the person's: none.
    p2, c2 = make(tmp_path / "b", monkeypatch, {"a": worker("a", *PHONE), "b": worker("b", *PHONE)})
    out2 = run(p2.present(text="hello"))
    assert not out2["shown"] and c2.calls == [] and "several phones" in out2["notes"][0]
    # The one phone online is used; a TV listed in show.screens wins.
    p3, c3 = make(tmp_path / "c", monkeypatch, {"a": worker("a", *PHONE), "tv1": worker("tv", *TV)},
                  screens=["tv"])
    out3 = run(p3.present(say="On the TV.", image="https://img.test/x.png"))
    assert out3["screen"] == "tv" and out3["voice"] == "a"   # the TV cannot speak: the phone does
    assert [(c[0], c[1]) for c in c3.calls] == [("tv.open_url", "tv1"), ("voice.speak", "a")]
    assert p3.screens()["screen"] == "tv"


def test_named_worker_url_and_app_subjects(tmp_path, monkeypatch):
    p, client = make(tmp_path, monkeypatch, {"tv1": worker("office-tv", *TV),
                                             "p1": worker("bakephone", *PHONE)},
                     brief_phone="bakephone")
    out = run(p.present(url="https://example.org/a", where="office-tv"))
    assert out["url"] == "https://example.org/a" and out["screen"] == "office-tv"
    assert client.calls[-1][:3] == ("tv.open_url", "tv1", {"url": "https://example.org/a"})
    assert p.page_html(out["id"], "n") is None                # a URL has no show page
    out = run(p.present(app="YouTube", where="office-tv"))
    assert out["shown"] and client.calls[-1][:3] == ("tv.launch", "tv1", {"app": "YouTube"})
    out = run(p.present(app="com.android.settings"))
    assert client.calls[-1][:3] == ("device.launch", "p1", {"package": "com.android.settings"})
    out = run(p.present(text="x", where="nowhere-tv"))
    assert not out["shown"] and "nowhere-tv is not online" in out["notes"][0]


def test_where_none_makes_only_the_link(tmp_path, monkeypatch):
    p, client = make(tmp_path, monkeypatch, {"p1": worker("bakephone", *PHONE)},
                     brief_phone="bakephone")
    out = run(p.present(say="Hi", text="just a link", where="none"))
    assert client.calls == [] and not out["shown"] and out["unsaid"] == "Hi"
    assert out["url"].startswith("https://hub.test/show/")
    assert "just a link" in p.page_html(out["id"], "n")


def test_device_failures_degrade_to_notes(tmp_path, monkeypatch):
    replies = {("device.open_url", "p1"): {"ok": False, "error": "denied: no grant covers it"},
               ("voice.speak", "p1"): asyncio.TimeoutError()}
    p, client = make(tmp_path, monkeypatch, {"p1": worker("bakephone", *PHONE)},
                     brief_phone="bakephone", replies=replies)
    out = run(p.present(say="Here.", markdown="**x**"))
    assert not out["shown"] and not out["spoken"] and out["unsaid"] == "Here."
    assert "not shown on bakephone: denied: no grant covers it" in out["notes"]
    assert any("did not answer voice.speak" in n for n in out["notes"])
    # A worker reply whose own result says ok: false is a failure too.
    replies = {("device.open_url", "p1"): {"ok": True, "result": {"ok": False, "error": "no activity"}}}
    p2, _ = make(tmp_path / "b", monkeypatch, {"p1": worker("bakephone", *PHONE)},
                 brief_phone="bakephone", replies=replies)
    assert "no activity" in run(p2.present(text="x"))["notes"][0]


def test_bad_input_is_refused(tmp_path, monkeypatch):
    p, _ = make(tmp_path, monkeypatch, {})
    with pytest.raises(ValueError, match="exactly one subject"):
        run(p.present(say="hi"))
    with pytest.raises(ValueError, match="exactly one subject"):
        run(p.present(url="https://a.test", text="b"))
    with pytest.raises(ValueError, match="url: an http"):
        run(p.present(url="javascript:alert(1)"))
    with pytest.raises(ValueError, match="image: an https"):
        run(p.present(image="http://plain.test/x.png"))
    with pytest.raises(ValueError, match="image: an https"):
        run(p.present(image="data:text/html;base64,PGI+"))
    with pytest.raises(ValueError, match="short sentences"):
        run(p.present(say="x" * 301, text="y"))
    ok = run(p.present(image="data:image/png;base64,iVBORw0KGgo=", where="none"))
    assert ok["kind"] == "image"
    assert run(p.present(diagram="<svg xmlns='http://www.w3.org/2000/svg'/>", where="none"))["kind"] == "svg"


def test_pages_expire_and_close_and_ids_are_unguessable(tmp_path, monkeypatch):
    p, _ = make(tmp_path, monkeypatch, {})
    out = run(p.present(text="soon gone", where="none", minutes=5))
    assert out["expires"] - time.time() == pytest.approx(300, abs=5)
    assert p.page(out["id"]) is not None
    assert p.page(out["id"], now=time.time() + 301) is None   # expired: gone, file removed
    assert not list((tmp_path / "show" / "pages").glob(out["id"] + ".json"))
    out = run(p.present(text="close me", where="none"))
    assert p.close(out["id"]) == {"id": out["id"], "closed": True}
    assert p.page(out["id"]) is None and p.close(out["id"])["closed"] is False
    for bad in ("", "../etc/passwd", "short", "a" * 65, "x y" * 10):
        assert p.page(bad) is None and p.page_html(bad, "n") is None
    ids = {run(p.present(text="x", where="none"))["id"] for _ in range(20)}
    assert len(ids) == 20 and all(len(i) == 32 for i in ids)
    assert run(p.present(text="x", where="none", minutes=99999))["expires"] - time.time() <= 86400 + 5


def test_secret_values_are_masked_out_of_pages(tmp_path, monkeypatch):
    from rook.band_mcp import secret_mask
    monkeypatch.setattr(secret_mask, "scrub",
                        lambda obj, extra=None: obj.replace("hunter2-value", "{{secret:pw}}")
                        if isinstance(obj, str) else obj)
    p, _ = make(tmp_path, monkeypatch, {})
    out = run(p.present(text="the password is hunter2-value", title="hunter2-value", where="none"))
    html = p.page_html(out["id"], "n")
    assert "hunter2-value" not in html and "{{secret:pw}}" in html
    raw = (tmp_path / "show" / "pages" / f"{out['id']}.json").read_text()
    assert "hunter2-value" not in raw


def test_members_name_their_screen_and_see_only_their_own_shows(tmp_path, monkeypatch):
    p, client = make(tmp_path, monkeypatch, {"p1": worker("bakephone", *PHONE),
                                             "r1": worker("robinphone", *PHONE)},
                     brief_phone="bakephone")
    owner = Principal("human:bake", "human", "owner", ("human:owner",))
    member = Principal("human:robin", "human", "member", ("human:member",))
    tok = current_principal.set(owner)
    try:
        mine = run(p.present(text="owner's", where="none"))
    finally:
        current_principal.reset(tok)
    tok = current_principal.set(member)
    try:
        out = run(p.present(say="hi", text="robin's"))
        assert not out["shown"] and client.calls == []      # auto never uses the owner's phone
        assert "name one with where" in out["notes"][0]
        named = run(p.present(text="robin's", where="robinphone"))
        assert named["shown"] and client.calls[-1][1] == "r1"
        assert client.calls[-1][3] is member                # the device call is the member's
        assert {s["id"] for s in p.list()["shows"]} == {out["id"], named["id"]}
        with pytest.raises(PermissionError):
            p.close(mine["id"])
    finally:
        current_principal.reset(tok)
    tok = current_principal.set(owner)
    try:
        assert len(p.list()["shows"]) == 3 and p.close(out["id"])["closed"]
    finally:
        current_principal.reset(tok)


def test_knowledge_subject_is_read_as_the_caller(tmp_path, monkeypatch):
    seen = {}

    async def read(action="search", id=None, **kw):
        seen["who"] = current_principal.get()
        seen["args"] = (action, id)
        return {"title": "Floor plan", "body": "# Rooms\n\n- hall\n- kitchen"}
    p, client = make(tmp_path, monkeypatch, {}, plugins={"knowledge": FakePlugin(read=read)})
    out = run(p.present(knowledge="floor-plan", where="none"))
    assert seen["args"] == ("get", "floor-plan") and out["title"] == "Floor plan"
    html = p.page_html(out["id"], "n")
    assert "<h1>Rooms</h1>" in html and "<li>kitchen</li>" in html


def test_markdown_renderer_escapes_everything():
    html = render.markdown_html(
        "# T <b>x</b>\n\n<script>alert(1)</script>\n\n[ok](https://a.test/?q=\"x\") "
        "[bad](javascript:alert(1)) `<i>` **b** *i*\n\n- one\n- two\n\n1. a\n2. b\n\n"
        "| a | b |\n|---|---|\n| 1 | <x> |\n\n> quote\n\n```\n<code>\n```\n\n```mermaid\nA-->B\n```\n"
        "![pic](https://img.test/a.png)\n\n---\n")
    assert "<script>" not in html and "&lt;script&gt;" in html and "<b>x</b>" not in html
    assert '<a href="https://a.test/?q=&quot;x&quot;"' in html and 'href="javascript' not in html
    assert "<code>&lt;i&gt;</code>" in html and "<strong>b</strong>" in html and "<em>i</em>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html and "<ol><li>a</li><li>b</li></ol>" in html
    assert "<td>&lt;x&gt;</td>" in html and "<blockquote>" in html and "<hr>" in html
    assert "<pre><code>&lt;code&gt;</code></pre>" in html and '<pre class="mermaid">A--&gt;B</pre>' in html
    assert '<img src="https://img.test/a.png" alt="pic">' in html
    svg = render.body_html({"kind": "svg", "body": "<svg onload='x()'/>"})[0]
    assert "onload" not in svg and svg.startswith('<img class="subject" src="data:image/svg+xml;base64,')
    csp = render.csp("abc")
    assert "sandbox allow-scripts" in csp and "'nonce-abc'" in csp and "default-src 'none'" in csp
    assert "allow-same-origin" not in csp


@pytest.mark.asyncio
async def test_page_api_and_public_dashboard_route(tmp_path, monkeypatch):
    """/show/page-api (MCP side) and the dashboard's public /show/<id>."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from starlette.applications import Starlette
    from starlette.testclient import TestClient as StarletteClient

    from rook.hub.plugins.show import web as show_web
    from rook.remote import show_web as dash, upstream
    p, _ = make(tmp_path, monkeypatch, {})
    out = await p.present(markdown="# Hello <there>", title="Hi", where="none")
    api = Starlette(routes=show_web.routes(lambda: p))
    with StarletteClient(api) as sc:
        r = sc.get("/show/page-api", params={"id": out["id"]})
        assert r.status_code == 200 and "<h1>Hello &lt;there&gt;</h1>" in r.json()["html"]
        assert sc.get("/show/page-api", params={"id": "x" * 32}).status_code == 404

    # The dashboard reaches the MCP side in memory, as on `rook hub serve`.
    undo = upstream.register_local(["http://127.0.0.1:8765"], api)
    app = web.Application()
    dash.install(app)
    async with TestClient(TestServer(app)) as c:
        r = await c.get(f"/show/{out['id']}")
        body = await r.text()
        assert r.status == 200 and "<h1>Hello &lt;there&gt;</h1>" in body
        assert "sandbox" in r.headers["Content-Security-Policy"]
        assert r.headers["Cache-Control"] == "no-store" and r.headers["Referrer-Policy"] == "no-referrer"
        for bad in ("x" * 32, "nope", out["id"] + "%2F.."):
            r = await c.get(f"/show/{bad}")
            assert r.status == 404 and "This page is gone" in await r.text()
    undo()


def test_dashboard_lets_show_pages_through_without_a_sign_in():
    from rook.remote.bootstrap import CombinedServer
    assert CombinedServer._auth_exempt("/show/abc") and not CombinedServer._auth_exempt("/showx")


def test_house_agent_show_tool_runs_as_the_house_agent_for_the_asker(tmp_path, monkeypatch):
    from rook.hub.plugins.home import front
    assert "show" in front.TOOLS
    p, client = make(tmp_path, monkeypatch, {"p1": worker("bakephone", *PHONE)},
                     brief_phone="bakephone")
    house = Principal("home:Rook", "token", "agent", label="agent:home")

    class Home:
        identity = "agent:home"

        def may_call(self, principal, cap):
            return cap == "show.present" and principal is not None and principal.id != "human:nobody"

        def _as_self(self):
            import contextlib
            return contextlib.nullcontext()

        def journal(self, *a, **k):
            self.logged = a

    door = front.FrontDoor.__new__(front.FrontDoor)
    door.home = Home()
    monkeypatch.setattr(front.FrontDoor, "plugin", lambda self, name: p if name == "show" else None)
    monkeypatch.setattr(front.FrontDoor, "principal", lambda self: house)
    state = {"did": [], "started": [], "asked": [], "onboard": []}
    asker = Principal("human:bake", "human", "owner", ("human:owner",))
    out = run(door._show({"say": "Here.", "markdown": "# x", "title": "X"}, state,
                         asker="Bake", principal=asker))
    assert out["shown"] and state["did"] == [{"summary": "showed X on bakephone", "ok": True}]
    assert client.calls[0][3] is house                       # device calls as the house agent
    assert front.compose(state, "")[0].startswith("Done: showed X on bakephone.")
    with pytest.raises(front.FrontError, match="may not show"):
        run(door._show({"text": "x"}, state, asker="nobody",
                       principal=Principal("human:nobody", "human", "member")))
