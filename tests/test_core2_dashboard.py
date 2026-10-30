"""The hub site (rook/core2/hub/doors/web): sign-in, the shell's pages.* calls,
sandboxed pages reaching only their declared caps and events under the
viewer's rights, agent-made pages with their note, archive and restore, and
the core pages' caps. A hub runs in-process (loopback band) and every request
goes through its HTTP door.

Every test runs under a hard timeout (``run``); HTTP requests run in a
thread with their own socket timeout."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2.hub.doors.web import caps as web_caps  # noqa: E402
from rook.core2.hub.doors.web.site import PAGE_CSP, inject  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the hub site."

BOARD = """\
manifest: 1
plugin: board
version: 1.0.0
doc: A bulletin board kept on the hub.
runs_on: hub
settings:
  motto: { type: str, where: hub, default: hello, doc: Shown on top. }
  size: { type: int, where: hub, default: 3 }
caps:
  count:
    doc: How many notes.
    on: hub
    risk: read
    scope: self
    run: scripts/board.py:count
  secret:
    doc: Not for the page.
    on: hub
    risk: read
    scope: self
    run: scripts/board.py:secret
  wipe:
    doc: Wipe the board.
    on: hub
    risk: write
    scope: self
    effects: [destructive]
    run: scripts/board.py:wipe
  motto:
    doc: The motto setting.
    on: hub
    risk: read
    scope: self
    run: scripts/board.py:motto
pages:
  board: { title: Board, file: pages/board.html, caps: ["board.count", "board.motto"], events: ["board.*"], nav: { section: Home } }
"""

BOARD_PY = """\
def count(ctx):
    return {"count": 3}

def secret(ctx):
    return {"secret": "no"}

def wipe(ctx):
    return {"wiped": True}

def motto(ctx):
    return {"motto": ctx.settings["motto"]}
"""

BOARD_HTML = "<!doctype html><html><head><title>Board</title></head><body><p id=n></p></body></html>"


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Client:
    """One browser: keeps the session cookie and the CSRF token."""

    def __init__(self, hub: Hub):
        self.port = next(d.port for d in hub.doors if d.name == "http")
        self.cookie = ""
        self.csrf = ""

    async def req(self, method, path, body=None, *, form=None, bearer=None, csrf=True, timeout=10):
        def go():
            data, headers = None, {}
            if form is not None:
                data = form.encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            if self.cookie:
                headers["Cookie"] = self.cookie
            if csrf and self.csrf and method == "POST":
                headers["X-Rook-CSRF"] = self.csrf
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                       headers=headers)
            try:
                with _opener.open(r, timeout=timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()
        return await asyncio.to_thread(go)

    async def json(self, method, path, body=None, **kw):
        status, _, data = await self.req(method, path, body, **kw)
        return status, json.loads(data or b"{}")

    async def sign_in(self, token):
        status, headers, _ = await self.req("POST", "/login", {"token": token})
        assert status == 200, status
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = (await self.json("GET", "/web/api/me"))[1]["csrf"]

    async def call(self, cap, args=None):
        return (await self.json("POST", "/web/api/call", {"cap": cap, "args": args or {}}))[1]

    async def open(self, page):
        return (await self.json("POST", "/web/api/open", {"page": page}))[1]

    async def page(self, token, cap, args=None, **kw):
        return (await self.json("POST", "/web/api/page", {"token": token, "cap": cap, "args": args or {}, **kw}))[1]


def make_library(tmp_path: Path) -> Path:
    lib = tmp_path / "library"
    folder = lib / "board"
    (folder / "scripts").mkdir(parents=True)
    (folder / "pages").mkdir()
    (folder / "plugin.yaml").write_text(BOARD)
    (folder / "scripts" / "board.py").write_text(BOARD_PY)
    (folder / "pages" / "board.html").write_text(BOARD_HTML)
    return lib


async def start(tmp_path: Path, **over):
    cfg = Config(data=tmp_path / "data", band=BAND, library=make_library(tmp_path),
                 users={"bake": {"preset": "owner"}, "kim": {"preset": "viewer"}},
                 presets={"viewer": {"grants": ["pages.*", "board.count", "approval.list"], "max_risk": "write"}},
                 agents={"claude": {"grants": ["*"]}, "narrow": {"grants": ["pages.*", "board.count"]}},
                 **{"doors": {"http": {"bind": "127.0.0.1:0"}}, **over})
    hub = Hub(cfg, transport=Loopback())
    await hub.start()
    tokens = {
        "bake": (cfg.data / "owner.token").read_text().strip(),
        "kim": hub.add_token(Caller(person="person:kim")),
        "claude": hub.add_token(Caller(person="person:bake", agent="agent:claude")),
        "narrow": hub.add_token(Caller(agent="agent:narrow")),
    }
    return hub, tokens


@pytest.fixture
def site(tmp_path):
    hubs = []

    async def make(**over):
        hub, tokens = await start(tmp_path, **over)
        hubs.append(hub)
        return hub, tokens
    yield make
    for hub in hubs:
        if hub.started:
            asyncio.run(hub.stop())


# -- sign-in --------------------------------------------------------------------

def test_sign_in_and_the_session_cookie(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            status, headers, _ = await c.req("GET", "/")
            assert status == 303 and headers["Location"] == "/login"
            status, headers, body = await c.req("GET", "/login")
            assert status == 200 and b'action="/login"' in body and "frame-ancestors 'none'" in \
                headers["Content-Security-Policy"]
            assert (await c.json("GET", "/web/api/me"))[0] == 401
            # an agent's token (alone or for a person) and a wrong one don't sign in
            for bad in (tokens["claude"], tokens["narrow"], "nope"):
                assert (await c.req("POST", "/login", {"token": bad}))[0] == 401
            status, headers, _ = await c.req("POST", "/login", form="token=nope")
            assert status == 303 and headers["Location"] == "/login?failed=1"
            status, headers, _ = await c.req("POST", "/login", form=f"token={tokens['bake']}")
            assert status == 303 and headers["Location"] == "/"
            cookie = headers["Set-Cookie"]
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
            c.cookie = cookie.split(";")[0]
            status, headers, body = await c.req("GET", "/")
            assert status == 200 and b"shell.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
            status, me = await c.json("GET", "/web/api/me")
            assert me["person"] == "person:bake" and me["csrf"]
            # POSTs need the CSRF token
            status, out = await c.json("POST", "/web/api/call", {"cap": "pages.list"})
            assert status == 403
            c.csrf = me["csrf"]
            assert (await c.call("pages.list"))["ok"]
            # the shell calls pages.* only
            status, out = await c.json("POST", "/web/api/call", {"cap": "hub.workers"})
            assert status == 403 and "pages.*" in out["error"]
            # static files and no path tricks
            assert (await c.req("GET", "/web/static/shell.js"))[0] == 200
            assert (await c.req("GET", "/web/static/..%2Fsite.py"))[0] == 404
            # the bearer API is untouched
            status, out = await c.json("GET", "/workers", bearer=tokens["bake"])
            assert status == 200 and out["ok"]
            # sign out
            status, _, _ = await c.req("POST", "/logout")
            assert status == 200
            assert (await c.json("GET", "/web/api/me"))[0] == 401
        finally:
            await hub.stop()
    run(go())


def test_sessions_survive_a_restart_and_end_with_the_person(site, tmp_path):
    async def go():
        hub, tokens = await site()
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        await hub.stop()
        hub2 = Hub(hub.cfg, transport=Loopback())
        await hub2.start()
        try:
            c.port = next(d.port for d in hub2.doors if d.name == "http")
            assert (await c.json("GET", "/web/api/me"))[1]["person"] == "person:bake"
            hub2.identities.users.pop("bake")     # a person taken out of the config
            assert (await c.json("GET", "/web/api/me"))[0] == 401
        finally:
            await hub2.stop()
    run(go())


def test_the_site_can_be_turned_off(site):
    async def go():
        hub, tokens = await site(doors={"http": {"bind": "127.0.0.1:0", "web": False}})
        try:
            c = Client(hub)
            for path in ("/", "/login", "/web/api/me"):
                assert (await c.req("GET", path))[0] == 401          # the JSON API wants a token
        finally:
            await hub.stop()
    run(go())


# -- pages ------------------------------------------------------------------------

def test_a_page_plugin_renders_sandboxed_with_the_bridge(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            listed = (await c.call("pages.list"))["result"]["pages"]
            ids = [p["page"] for p in listed]
            assert {"board/board", "hub/workers", "approval/approvals", "journal/journal",
                    "setting/settings"} <= set(ids)
            board = next(p for p in listed if p["page"] == "board/board")
            assert board["section"] == "Home" and board["caps"] == ["board.count", "board.motto"]
            opened = await c.open("board/board")
            assert opened["ok"] and opened["page"]["title"] == "Board"
            status, headers, body = await c.req("GET", opened["frame"])
            assert status == 200
            assert headers["Content-Security-Policy"] == PAGE_CSP
            assert PAGE_CSP.startswith("sandbox allow-scripts;") and "allow-same-origin" not in PAGE_CSP
            assert "connect-src 'none'" in PAGE_CSP
            html = body.decode()
            assert html.index("window.rook") < html.index("<p id=n>")       # the bridge comes first
            assert '"caps": ["board.count", "board.motto"]' in html
            # the frame link works once, and only with the session
            assert (await c.req("GET", opened["frame"]))[0] == 404
            again = await c.open("board/board")
            stranger = Client(hub)
            assert (await stranger.req("GET", again["frame"]))[0] == 404
            # opening is a journaled pages.view call
            _, j = await c.json("GET", "/journal?cap=pages.view", bearer=tokens["bake"])
            assert j["entries"] and j["entries"][0]["args"] == {"page": "board/board"}
            # unknown pages
            assert not (await c.open("board/nope"))["ok"]
        finally:
            await hub.stop()
    run(go())


def test_a_sandboxed_page_cannot_call_undeclared_caps(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])          # an owner: the person layer allows everything
            t = (await c.open("board/board"))["token"]
            out = await c.page(t, "board.count")
            assert out["ok"] and out["result"] == {"count": 3}
            for cap in ("board.secret", "hub.workers", "pages.archive", "journal.search"):
                out = await c.page(t, cap, {"page": "board/board"})
                assert out["error"] == "denied: page: grants", (cap, out)
            # the call ran as the person, in the page's own session, with a why
            _, j = await c.json("GET", "/journal?cap=board.count", bearer=tokens["bake"])
            e = j["entries"][0]
            assert e["person"] == "person:bake" and e["agent"] is None and e["why"] == "Shown on the board/board page."
            _, j = await c.json("GET", "/journal?cap=board.secret", bearer=tokens["bake"])
            assert j["entries"][0]["layer"] == "page: grants"
            # a token only works in the session that opened it
            other = Client(hub)
            await other.sign_in(tokens["bake"])
            status, out = await other.json("POST", "/web/api/page", {"token": t, "cap": "board.count"})
            assert status == 403
            status, out = await c.json("POST", "/web/api/page", {"token": "made-up", "cap": "board.count"})
            assert status == 403
        finally:
            await hub.stop()
    run(go())


def test_pages_run_under_the_viewers_rights(site):
    async def go():
        hub, tokens = await site()
        try:
            kim = Client(hub)
            await kim.sign_in(tokens["kim"])          # viewer: pages.*, board.count, approval.list
            t = (await kim.open("hub/workers"))["token"]
            out = await kim.page(t, "hub.workers")
            assert out["error"] == "denied: person: grants"
            t = (await kim.open("board/board"))["token"]
            assert (await kim.page(t, "board.count"))["ok"]
            out = await kim.page(t, "board.motto")         # declared by the page, not granted to kim
            assert out["error"] == "denied: person: grants"
            t = (await kim.open("approval/approvals"))["token"]
            assert (await kim.page(t, "approval.list"))["result"] == {"holds": []}
        finally:
            await hub.stop()
    run(go())


def test_archive_and_restore_are_caps_and_lose_nothing(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            t = (await c.open("board/board"))["token"]
            out = await c.call("pages.archive", {"page": "board/board", "reason": "done with it"})
            assert out["ok"], out
            live = [p["page"] for p in (await c.call("pages.list"))["result"]["pages"]]
            gone = (await c.call("pages.list", {"archived": True}))["result"]["pages"]
            assert "board/board" not in live
            assert gone[0]["page"] == "board/board" and gone[0]["archived"]["by"] == "person:bake"
            assert "archived" in (await c.open("board/board"))["error"]
            assert "archived" in (await c.page(t, "board.count"))["error"]   # open pages stop too
            assert (await c.call("pages.restore", {"page": "board/board"}))["ok"]
            assert (await c.open("board/board"))["ok"]
            assert json.loads((hub.data / "pages.json").read_text()) == {"archived": {}}
            # an agent (or a job) archives the same way, through the HTTP door
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "pages.archive",
                                                    "args": {"page": "journal/journal"}, "why": WHY},
                                  bearer=tokens["claude"])
            assert out["ok"], out
            assert hub.pages.archived["journal/journal"]["by"] == "agent:claude"
        finally:
            await hub.stop()
    run(go())


def test_agent_made_pages_keep_a_note_and_never_exceed_their_author(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            body = {"worker": "rook", "cap": "pages.create", "why": WHY, "args": {
                "name": "uptime", "title": "Uptime", "html": "<html><head></head><body>up</body></html>",
                "note": "Bake asked to keep an eye on the board.", "caps": ["board.*"], "events": ["board.*"],
                "task": "t_123"}}
            _, out = await c.json("POST", "/call", body, bearer=tokens["narrow"])
            assert out["ok"] and out["result"] == {"page": "pages/uptime"}, out
            _, out = await c.json("POST", "/call", body, bearer=tokens["narrow"])
            assert "exists" in out["error"]
            note = (hub.data / "pages" / "uptime" / "NOTE.md").read_text()
            assert note.startswith("# Uptime\n\nBake asked to keep an eye on the board.")
            assert "agent:narrow: made the page" in note
            got = (await c.call("pages.get", {"page": "pages/uptime"}))["result"]
            assert got["made"] and got["made_by"]["authors"] == [{"person": None, "agent": "agent:narrow"}]
            assert got["made_by"]["task"] == "t_123"
            assert got["note"] == note
            # viewed by an owner, the page still can't do more than its author (narrow: board.count)
            t = (await c.open("pages/uptime"))["token"]
            assert (await c.page(t, "board.count"))["ok"]
            assert (await c.page(t, "board.secret"))["error"] == "denied: author agent: grants"
            # changes go into the note's history; plugin pages aren't changed this way
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "pages.update", "why": WHY, "args": {
                "page": "pages/uptime", "change": "Added the count.", "html": "<p>3</p>"}}, bearer=tokens["claude"])
            assert out["ok"], out
            assert "agent:claude for person:bake: Added the count." in (hub.data / "pages/uptime/NOTE.md").read_text()
            # claude (all rights) changed it, but narrow wrote it too: still no more than narrow
            assert (await c.page(t, "board.secret"))["error"] == "denied: author agent: grants"
            assert (hub.data / "pages/uptime/page.html").read_text() == "<p>3</p>"
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "pages.update", "why": WHY, "args": {
                "page": "board/board", "change": "x", "html": "x"}}, bearer=tokens["claude"])
            assert "comes with a plugin" in out["error"]
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "pages.create", "why": WHY, "args": {
                **body["args"], "name": "../etc"}}, bearer=tokens["claude"])
            assert "bad args" in out["error"]
            # the frame is the page with the bridge (no <head>: one is added)
            status, headers, html = await c.req("GET", (await c.open("pages/uptime"))["frame"])
            assert status == 200 and html.decode().endswith("<p>3</p>") and "window.rook" in html.decode()
        finally:
            await hub.stop()
    run(go())


def test_pages_hear_only_declared_events_the_viewer_may_hear(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            t = (await c.open("board/board"))["token"]
            _, first = await c.json("GET", f"/web/api/events?token={t}")
            since = first["seq"]
            hub._emit("worker.online", {"worker": "x"}, "core")       # not declared by the board page
            hub._emit("board.changed", {"n": 4}, "board")
            await asyncio.sleep(0.2)
            _, got = await c.json("GET", f"/web/api/events?token={t}&since={since}")
            assert [e["event"] for e in got["events"]] == ["board.changed"]
            assert got["events"][0]["payload"] == {"n": 4}
            # a long poll returns when an event comes
            waiter = asyncio.create_task(c.json("GET", f"/web/api/events?token={t}&since={got['seq']}", timeout=30))
            await asyncio.sleep(0.3)
            hub._emit("board.cleared", {}, "board")
            _, later = await asyncio.wait_for(waiter, 10)
            assert [e["event"] for e in later["events"]] == ["board.cleared"]
            # kim may open the board but not hear board.* (not in her rights)
            kim = Client(hub)
            await kim.sign_in(tokens["kim"])
            tk = (await kim.open("board/board"))["token"]
            seq = (await kim.json("GET", f"/web/api/events?token={tk}"))[1]["seq"]
            hub._emit("board.changed", {"n": 5}, "board")
            await asyncio.sleep(0.2)
            next(d for d in hub.doors if d.name == "http").web.poll_secs = 0.2     # nothing is coming
            _, out = await kim.json("GET", f"/web/api/events?token={tk}&since={seq}")
            assert out["events"] == []
        finally:
            await hub.stop()
    run(go())


# -- the core pages' caps ------------------------------------------------------------

def test_approvals_page_answers_holds(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            _, held = await c.json("POST", "/call", {"worker": "rook", "cap": "board.wipe", "why": WHY},
                                   bearer=tokens["claude"])
            assert held["held"], held
            t = (await c.open("approval/approvals"))["token"]
            holds = (await c.page(t, "approval.list"))["result"]["holds"]
            assert [h["hold"] for h in holds] == [held["hold"]] and holds[0]["cap"] == "board.wipe"
            out = await c.page(t, "approval.decide", {"hold": held["hold"], "decision": "approve"})
            assert out["ok"] and out["result"]["ok"] and out["result"]["result"] == {"wiped": True}, out
            out = await c.page(t, "approval.decide", {"hold": held["hold"], "decision": "approve"})
            assert "unknown hold" in out["error"]
            out = await c.page(t, "approval.decide", {"hold": "h_x", "decision": "maybe"})
            assert out["error"].startswith("bad args")

            # nothing but the person answers a hard stop: not an agent acting for them ...
            _, held = await c.json("POST", "/call", {"worker": "rook", "cap": "board.wipe", "why": WHY},
                                   bearer=tokens["claude"])
            for path, body in ((f"/holds/{held['hold']}", {"decision": "approve"}),
                               ("/call", {"worker": "rook", "cap": "approval.decide", "why": WHY,
                                          "args": {"hold": held["hold"], "decision": "approve"}})):
                _, out = await c.json("POST", path, body, bearer=tokens["claude"])
                assert "agent:claude may not release" in out["error"], out
            # ... and not a page an agent wrote, even while the person views it
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "pages.create", "why": WHY, "args": {
                "name": "sneaky", "title": "Sneaky", "html": "<p>hi</p>", "note": "x",
                "caps": ["approval.*"]}}, bearer=tokens["claude"])
            assert out["ok"], out
            ts = (await c.open("pages/sneaky"))["token"]
            assert (await c.page(ts, "approval.list"))["ok"]
            out = await c.page(ts, "approval.decide", {"hold": held["hold"], "decision": "approve"})
            assert out["error"] == "denied: page: a page an agent made can't answer holds"
            assert held["hold"] in hub.dispatcher.holds
        finally:
            await hub.stop()
    run(go())


def test_workers_journal_and_settings_pages(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            t = (await c.open("hub/workers"))["token"]
            workers = (await c.page(t, "hub.workers"))["result"]["workers"]
            assert workers[0]["name"] == "rook" and workers[0]["hub"]
            t = (await c.open("journal/journal"))["token"]
            entries = (await c.page(t, "journal.search", {"cap": "pages.view", "limit": 5}))["result"]["entries"]
            assert entries and all(e["cap"] == "pages.view" for e in entries)

            t = (await c.open("setting/settings"))["token"]
            status = (await c.page(t, "hub.status"))["result"]
            assert status["hub"] == hub.hub_id and status["people"][0]["name"] == "bake"
            assert {"pages", "hub", "approval", "journal", "setting", "board"} <= {p["plugin"] for p in status["plugins"]}
            listed = (await c.page(t, "setting.list"))["result"]["settings"]
            assert {(s["plugin"], s["key"], s["value"], s["source"]) for s in listed} == {
                ("board", "motto", "hello", "default"), ("board", "size", 3, "default")}
            out = await c.page(t, "setting.set", {"plugin": "board", "key": "size", "value": "big"})
            assert out["error"].startswith("bad value")
            assert (await c.page(t, "setting.set", {"plugin": "board", "key": "motto", "value": "hi"}))["ok"]
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "board.motto", "why": WHY},
                                  bearer=tokens["bake"])
            assert out["result"] == {"motto": "hi"}           # the plugin sees it on its next call
            assert json.loads((hub.data / "settings.json").read_text()) == {"board": {"motto": "hi"}}
            # only owners set; null goes back to the default
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "setting.set", "why": WHY,
                                                    "args": {"plugin": "board", "key": "motto", "value": "x"}},
                                  bearer=tokens["narrow"])
            assert "denied" in out["error"]
            assert (await c.page(t, "setting.set", {"plugin": "board", "key": "motto", "value": None}))["ok"]
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "board.motto", "why": WHY},
                                  bearer=tokens["bake"])
            assert out["result"] == {"motto": "hello"}
        finally:
            await hub.stop()
    run(go())


def test_stored_settings_apply_at_start(site, tmp_path):
    async def go():
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "settings.json").write_text('{"board": {"motto": "stored"}}')
        hub, tokens = await site(settings={"board": {"motto": "from config", "size": 9}})
        try:
            c = Client(hub)
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "board.motto", "why": WHY},
                                  bearer=tokens["bake"])
            assert out["result"] == {"motto": "stored"}
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "setting.list", "why": WHY},
                                  bearer=tokens["bake"])
            src = {s["key"]: (s["value"], s["source"]) for s in out["result"]["settings"]}
            assert src == {"motto": ("stored", "set"), "size": (9, "config")}
        finally:
            await hub.stop()
    run(go())


# -- the front end ---------------------------------------------------------------------

def test_inject_puts_the_bridge_first():
    info = {"page": "a/b", "title": "</script><script>alert(1)</script>", "caps": [], "events": []}
    out = inject("<!doctype html><html><HEAD><title>x</title></HEAD><body></body></html>", info)
    assert out.index("window.rook") < out.index("<title>")
    assert "</script><script>alert" not in out          # page info can't close the script
    assert inject("<p>hi</p>", info).startswith("<!doctype html><head>")
    assert "<html lang=en><head>" in inject("<html lang=en><body>x</body></html>", info)


def test_the_javascript_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    static = Path(web_caps.__file__).with_name("static")
    files = sorted(static.glob("*.js"))
    for page in sorted(web_caps.BUILTIN.glob("*/pages/*.html")):
        for i, script in enumerate(_inline_scripts(page.read_text())):
            f = Path(static.parent / f".check-{page.stem}-{i}.js")
            files.append(f)
            f.write_text(script)
    try:
        for f in files:
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"{f.name}: {r.stderr}"
    finally:
        for f in static.parent.glob(".check-*.js"):
            f.unlink()


def _inline_scripts(html: str) -> list[str]:
    import re
    return [m.group(1) for m in re.finditer(r"<script>(.*?)</script>", html, re.S)]


def test_builtin_manifests_are_valid():
    from rook.kit.manifest import check
    for folder in sorted(web_caps.BUILTIN.iterdir()):
        raw, issues = check(folder)
        assert raw is not None and not issues, (folder.name, [str(i) for i in issues])
        assert raw["builtin"] and raw["runs_on"] == "hub"


def test_every_builtin_cap_is_known():
    from rook.kit.manifest import load
    declared = set()
    for folder in web_caps.BUILTIN.iterdir():
        declared |= set(load(folder).data["caps"])
    assert declared == {
        "pages.list", "pages.view", "pages.get", "pages.create", "pages.update", "pages.archive", "pages.restore",
        "hub.status", "hub.workers", "approval.list", "approval.decide", "journal.search", "setting.list",
        "setting.set", "llm.providers", "llm.models", "llm.status"}


def test_a_library_plugin_cant_take_a_builtin_name(site, tmp_path):
    async def go():
        clash = tmp_path / "library" / "hub"
        clash.mkdir(parents=True)
        (clash / "plugin.yaml").write_text("manifest: 1\nplugin: hub\nversion: 9.0.0\ndoc: Not the hub.\nruns_on: hub\n")
        hub, tokens = await site()
        try:
            assert hub.plugins.plugins["hub"].manifest["builtin"]        # refused, logged; the hub runs
            assert "board" in hub.plugins.plugins
        finally:
            await hub.stop()
    run(go())
