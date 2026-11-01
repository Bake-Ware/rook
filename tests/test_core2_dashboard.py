"""The hub site (rook/core2/hub/doors/web): sign-in, the shell's page.* calls,
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

from rook.core2.hub import passwords  # noqa: E402
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
emits:
  board.inbox: { doc: A message came in from outside., taints: true, payload: { text: { type: str } } }
  board.note: { doc: A note for someone., payload: { person: { type: str }, text: { type: str } } }
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

    async def req(self, method, path, body=None, *, form=None, bearer=None, csrf=True, timeout=10, headers=None):
        extra = dict(headers or {})

        def go():
            data, headers = None, dict(extra)
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
    # Tests written for hub.security on (holds, gates, device-code approval); security off has its own tests.
    over.setdefault("settings", {}).setdefault("hub", {"security": "on"})
    cfg = Config(data=tmp_path / "data", band=BAND, library=make_library(tmp_path),
                 users={"bake": {"preset": "owner"}, "kim": {"preset": "viewer"}},
                 presets={"viewer": {"grants": ["page.*", "board.count", "approval.list"], "max_risk": "write"}},
                 agents={"claude": {"grants": ["*"]}, "narrow": {"grants": ["page.*", "board.count"]}},
                 **{"doors": {"http": {"bind": "127.0.0.1:0"}}, **over})
    hub = Hub(cfg, transport=Loopback())
    await hub.start()
    for name in ("bake", "kim"):
        passwords.set_password(cfg.data, name, f"{name}-pw", by="passwd")
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
            assert b'name="user"' in body and b'name="password"' in body and b'name="token"' not in body
            assert (await c.json("GET", "/web/api/me"))[0] == 401
            # a wrong password, an unknown name, or no password don't sign in
            for bad in ({"user": "bake", "password": "nope"}, {"user": "eve", "password": "bake-pw"},
                        {"user": "bake"}, {"user": "kim", "password": "bake-pw"}, {}):
                assert (await c.req("POST", "/login", bad))[0] == 401, bad
            # an app's token sign-in: an agent's token (alone or for a person) and a wrong one don't
            for bad in (tokens["claude"], tokens["narrow"], "nope"):
                assert (await c.req("POST", "/login", {"token": bad}))[0] == 401
            status, headers, _ = await c.req("POST", "/login", form="user=bake&password=nope")
            assert status == 303 and headers["Location"] == "/login?failed=1"
            # login CSRF: another site's form can't sign this browser in (review D11)
            for bad in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
                status, headers, _ = await c.req("POST", "/login", form="user=bake&password=bake-pw", headers=bad)
                assert status == 403 and "Set-Cookie" not in headers, bad
            own = {"Origin": f"http://127.0.0.1:{c.port}", "Sec-Fetch-Site": "same-origin"}
            status, headers, _ = await c.req("POST", "/login", form="user=Bake&password=bake-pw", headers=own)
            assert status == 303 and headers["Location"] == "/"
            cookie = headers["Set-Cookie"]
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
            c.cookie = cookie.split(";")[0]
            status, headers, body = await c.req("GET", "/")
            assert status == 200 and b"shell.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
            status, me = await c.json("GET", "/web/api/me")
            assert me["person"] == "person:bake" and me["csrf"]
            # POSTs need the CSRF token
            status, out = await c.json("POST", "/web/api/call", {"cap": "page.list"})
            assert status == 403
            c.csrf = me["csrf"]
            assert (await c.call("page.list"))["ok"]
            # the shell calls page.* only
            status, out = await c.json("POST", "/web/api/call", {"cap": "hub.workers"})
            assert status == 403 and "page.*" in out["error"]
            # no agents package: no dock, so no chat either
            status, out = await c.json("POST", "/web/api/call", {"cap": "agent.chat.send",
                                                                 "args": {"agent": "house", "text": "hi"}})
            assert status == 403
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


def test_the_account_page_changes_the_password_and_names_the_connector(site, tmp_path):
    """Castling-switchover-decisions: "a normal username and password login,
    like 0.4 is"; anything the user must do gets a page."""
    async def go():
        hub, tokens = await site(doors={"http": {"bind": "127.0.0.1:0"},
                                        "mcp": {"bind": "127.0.0.1:0", "oauth": "http://127.0.0.1:1"}})
        try:
            c = Client(hub)
            assert (await c.req("GET", "/account"))[1]["Location"] == "/login"
            # a password init made: the first sign-in goes to the account page to change it
            passwords.set_password(hub.data, "bake", "made-1", by=passwords.GENERATED)
            status, headers, _ = await c.req("POST", "/login", form="user=bake&password=made-1")
            assert status == 303 and headers["Location"] == "/account?first=1"
            c.cookie = headers["Set-Cookie"].split(";")[0]
            status, headers, body = await c.req("GET", "/account")
            assert status == 200 and b"account.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
            assert (await c.req("GET", "/web/static/account.js"))[0] == 200
            status, me = await c.json("GET", "/web/api/me")
            assert me["connector"] == "http://127.0.0.1:1/mcp"
            # change it: CSRF, then the current password
            assert (await c.json("POST", "/web/api/password", {"current": "made-1", "password": "mine"}))[0] == 403
            c.csrf = me["csrf"]
            out = (await c.json("POST", "/web/api/password", {"current": "wrong", "password": "mine"}))[1]
            assert not out["ok"] and "current password" in out["error"]
            assert not (await c.json("POST", "/web/api/password", {"current": "made-1", "password": ""}))[1]["ok"]
            assert (await c.json("POST", "/web/api/password", {"current": "made-1", "password": "mine"}))[1]["ok"]
            assert passwords.set_by(hub.data, "bake") == "web"
            other = Client(hub)
            assert (await other.req("POST", "/login", {"user": "bake", "password": "made-1"}))[0] == 401
            status, headers, _ = await other.req("POST", "/login", form="user=bake&password=mine")
            assert status == 303 and headers["Location"] == "/"
            # the way back in when it's lost: the hub CLI's passwd (passwords.set_password), no restart
            passwords.set_password(hub.data, "bake", "reset", by="passwd")
            assert (await other.req("POST", "/login", {"user": "bake", "password": "reset"}))[0] == 200
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
            listed = (await c.call("page.list"))["result"]["pages"]
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
            # opening is a journaled page.view call
            _, j = await c.json("GET", "/journal?cap=page.view", bearer=tokens["bake"])
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
            for cap in ("board.secret", "hub.workers", "page.archive", "journal.search"):
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
            await kim.sign_in(tokens["kim"])          # viewer: page.*, board.count, approval.list
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
            out = await c.call("page.archive", {"page": "board/board", "reason": "done with it"})
            assert out["ok"], out
            live = [p["page"] for p in (await c.call("page.list"))["result"]["pages"]]
            gone = (await c.call("page.list", {"archived": True}))["result"]["pages"]
            assert "board/board" not in live
            assert gone[0]["page"] == "board/board" and gone[0]["archived"]["by"] == "person:bake"
            assert "archived" in (await c.open("board/board"))["error"]
            assert "archived" in (await c.page(t, "board.count"))["error"]   # open pages stop too
            assert (await c.call("page.restore", {"page": "board/board"}))["ok"]
            assert (await c.open("board/board"))["ok"]
            assert json.loads((hub.data / "pages.json").read_text()) == {"archived": {}, "start": {}}
            # an agent (or a job) archives the same way, through the HTTP door
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "page.archive",
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
            body = {"worker": "rook", "cap": "page.create", "why": WHY, "args": {
                "name": "uptime", "title": "Uptime", "html": "<html><head></head><body>up</body></html>",
                "note": "Bake asked to keep an eye on the board.", "caps": ["board.*"], "events": ["board.*"],
                "task": "t_123"}}
            _, out = await c.json("POST", "/call", body, bearer=tokens["narrow"])
            assert out["ok"] and out["result"] == {"page": "page/uptime"}, out
            _, out = await c.json("POST", "/call", body, bearer=tokens["narrow"])
            assert "exists" in out["error"]
            note = (hub.data / "pages" / "uptime" / "NOTE.md").read_text()
            assert note.startswith("# Uptime\n\nBake asked to keep an eye on the board.")
            assert "agent:narrow: made the page" in note
            got = (await c.call("page.get", {"page": "page/uptime"}))["result"]
            assert got["made"] and got["made_by"]["authors"] == [{"person": None, "agent": "agent:narrow"}]
            assert got["made_by"]["task"] == "t_123"
            assert got["note"] == note
            # viewed by an owner, the page still can't do more than its author (narrow: board.count)
            t = (await c.open("page/uptime"))["token"]
            assert (await c.page(t, "board.count"))["ok"]
            assert (await c.page(t, "board.secret"))["error"] == "denied: agent: grants"
            # changes go into the note's history; plugin pages aren't changed this way
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "page.update", "why": WHY, "args": {
                "page": "page/uptime", "change": "Added the count.", "html": "<p>3</p>"}}, bearer=tokens["claude"])
            assert out["ok"], out
            assert "agent:claude for person:bake: Added the count." in (hub.data / "pages/uptime/NOTE.md").read_text()
            # claude (all rights) changed it, but narrow wrote it too: still no more than narrow
            assert (await c.page(t, "board.secret"))["error"] == "denied: agent: grants"
            assert (hub.data / "pages/uptime/page.html").read_text() == "<p>3</p>"
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "page.update", "why": WHY, "args": {
                "page": "board/board", "change": "x", "html": "x"}}, bearer=tokens["claude"])
            assert "comes with a plugin" in out["error"]
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "page.create", "why": WHY, "args": {
                **body["args"], "name": "../etc"}}, bearer=tokens["claude"])
            assert "bad args" in out["error"]
            # the frame is the page with the bridge (no <head>: one is added)
            status, headers, html = await c.req("GET", (await c.open("page/uptime"))["frame"])
            assert status == 200 and html.decode().endswith("<p>3</p>") and "window.rook" in html.decode()
        finally:
            await hub.stop()
    run(go())


def test_a_made_page_cant_launder_its_author_through_a_new_page(site):
    # narrow makes P1 (caps page.create); bake opens it; P1 makes P2 with
    # caps ["*"]. P2 must still carry narrow as an author, never bake alone.
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "page.create", "why": WHY, "args": {
                "name": "p1", "title": "P1", "html": "<p>1</p>", "note": "n", "caps": ["page.create"]}},
                bearer=tokens["narrow"])
            assert out["ok"], out
            t1 = (await c.open("page/p1"))["token"]
            out = await c.page(t1, "page.create", {"name": "p2", "title": "P2", "html": "<p>2</p>",
                                                    "note": "n", "caps": ["*"]})
            assert out["ok"], out
            got = (await c.call("page.get", {"page": "page/p2"}))["result"]
            assert {"person": "person:bake", "agent": "agent:narrow"} in got["made_by"]["authors"]
            t2 = (await c.open("page/p2"))["token"]
            assert (await c.page(t2, "board.count"))["ok"]
            assert "denied" in (await c.page(t2, "board.secret"))["error"]
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


def test_page_events_reach_only_the_people_they_belong_to(site):
    # An adult's page hears its own holds and messages, not other people's;
    # outside content naming nobody reaches owners only (review D6).
    async def go():
        hub, tokens = await site()
        try:
            hub.identities.add_user("ann", "adult")
            ann, bake = Client(hub), Client(hub)
            await ann.sign_in(hub.add_token(Caller(person="person:ann")))
            await bake.sign_in(tokens["bake"])
            pages = {}
            for who, c in (("ann", ann), ("bake", bake)):
                for page in ("board/board", "approval/approvals"):
                    t = (await c.open(page))["token"]
                    pages[who, page] = (c, t, (await c.json("GET", f"/web/api/events?token={t}"))[1]["seq"])
            hub._emit("board.inbox", {"text": "a stranger's text"}, "board")
            hub._emit("board.note", {"person": "person:bake", "text": "for bake"}, "board")
            hub._emit("board.note", {"person": "person:ann", "text": "for ann"}, "board")
            hub._emit("board.changed", {"n": 1}, "board")
            hub._emit("hold.created", {"hold": "h_1", "cap": "x.y", "person": "person:bake", "reasons": []}, "core")
            hub._emit("hold.created", {"hold": "h_2", "cap": "x.y", "person": "person:ann", "reasons": []}, "core")
            hub._emit("hold.created", {"hold": "h_3", "cap": "x.y", "person": None, "reasons": []}, "core")
            await asyncio.sleep(0.2)
            next(d for d in hub.doors if d.name == "http").web.poll_secs = 0.2

            async def heard(who, page):
                c, t, seq = pages[who, page]
                _, out = await c.json("GET", f"/web/api/events?token={t}&since={seq}")
                return [(e["event"], e["payload"].get("text") or e["payload"].get("hold") or e["payload"].get("n"))
                        for e in out["events"]]
            assert await heard("ann", "board/board") == [("board.note", "for ann"), ("board.changed", 1)]
            assert await heard("ann", "approval/approvals") == [("hold.created", "h_2")]
            assert await heard("bake", "board/board") == [
                ("board.inbox", "a stranger's text"), ("board.note", "for bake"), ("board.note", "for ann"),
                ("board.changed", 1)]
            assert [h for _, h in await heard("bake", "approval/approvals")] == ["h_1", "h_2", "h_3"]
            # hold.decided says whose hold it was
            got = []
            hub.listeners.append(lambda e, p, src: got.append(p) if e == "hold.decided" else None)
            _, held = await bake.json("POST", "/call", {"worker": "rook", "cap": "board.wipe", "why": WHY},
                                      bearer=tokens["claude"])
            _, out = await bake.json("POST", f"/holds/{held['hold']}", {"decision": "refuse"}, bearer=tokens["bake"])
            assert out == {"ok": True} and got[-1]["person"] == "person:bake", (out, got)
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
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "page.create", "why": WHY, "args": {
                "name": "sneaky", "title": "Sneaky", "html": "<p>hi</p>", "note": "x",
                "caps": ["approval.*"]}}, bearer=tokens["claude"])
            assert out["ok"], out
            ts = (await c.open("page/sneaky"))["token"]
            assert (await c.page(ts, "approval.list"))["ok"]
            out = await c.page(ts, "approval.decide", {"hold": held["hold"], "decision": "approve"})
            assert out["error"] == "denied: page: a page an agent made can't answer holds"
            assert held["hold"] in hub.dispatcher.holds
            # ... and not the approval.decide cap, even with the person's own
            # token: a job or hub plugin acting as them dispatches it (review H7)
            for decision in ("approve", "refuse"):
                _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "approval.decide", "why": WHY,
                                                         "args": {"hold": held["hold"], "decision": decision}},
                                      bearer=tokens["bake"])
                assert "person:bake may not" in out["error"], out
            assert held["hold"] in hub.dispatcher.holds
            # the person at a door where they are there: POST /holds with their token
            _, out = await c.json("POST", f"/holds/{held['hold']}", {"decision": "approve"}, bearer=tokens["bake"])
            assert out["ok"] and out["result"] == {"wiped": True}, out
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
            entries = (await c.page(t, "journal.search", {"cap": "page.view", "limit": 5}))["result"]["entries"]
            assert entries and all(e["cap"] == "page.view" for e in entries)

            t = (await c.open("setting/settings"))["token"]
            status = (await c.page(t, "hub.status"))["result"]
            assert status["hub"] == hub.hub_id and status["people"][0]["name"] == "bake"
            assert {"page", "hub", "approval", "journal", "setting", "board"} <= {p["plugin"] for p in status["plugins"]}
            listed = (await c.page(t, "setting.list"))["result"]["settings"]
            assert {(s["plugin"], s["key"], s["value"], s["source"]) for s in listed} == {
                ("board", "motto", "hello", "default"), ("board", "size", 3, "default"),
                ("page", "dock", "house", "default"), ("hub", "security", "on", "config")}
            # the security switch (castling-switchover-decisions, "Move-in"): a dropdown, owners only
            sec = next(s for s in listed if (s["plugin"], s["key"]) == ("hub", "security"))
            assert sec["enum"] == ["off", "on"] and sec["default"] == "off"
            assert status["security"] == "on" and not hub.security_off()
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "setting.set", "why": WHY,
                                                    "args": {"plugin": "hub", "key": "security", "value": "off"}},
                                  bearer=tokens["narrow"])
            assert "denied" in out["error"] and not hub.security_off()
            assert (await c.page(t, "setting.set", {"plugin": "hub", "key": "security", "value": "off"}))["ok"]
            assert hub.security_off() and (await c.page(t, "hub.status"))["result"]["security"] == "off"
            flipped = hub.journal.query(cap="setting.set", limit=5)
            assert any(e["args"] == {"plugin": "hub", "key": "security", "value": "off"} for e in flipped)
            out = await c.page(t, "setting.set", {"plugin": "hub", "key": "security", "value": "yolo"})
            assert out["error"].startswith("bad value")
            assert (await c.page(t, "setting.set", {"plugin": "hub", "key": "security", "value": "on"}))["ok"]
            assert not hub.security_off()
            out = await c.page(t, "setting.set", {"plugin": "board", "key": "size", "value": "big"})
            assert out["error"].startswith("bad value")
            assert (await c.page(t, "setting.set", {"plugin": "board", "key": "motto", "value": "hi"}))["ok"]
            _, out = await c.json("POST", "/call", {"worker": "rook", "cap": "board.motto", "why": WHY},
                                  bearer=tokens["bake"])
            assert out["result"] == {"motto": "hi"}           # the plugin sees it on its next call
            assert json.loads((hub.data / "settings.json").read_text()) == {"board": {"motto": "hi"},
                                                                            "hub": {"security": "on"}}
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
            src = {s["key"]: (s["value"], s["source"]) for s in out["result"]["settings"] if s["plugin"] == "board"}
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
        "page.list", "page.view", "page.get", "page.create", "page.update", "page.archive", "page.restore",
        "page.components", "page.site", "page.start", "hub.status", "hub.workers", "hub.manifests", "hub.door", "approval.list", "approval.decide", "approval.ask", "approval.get", "journal.search", "setting.list",
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


def test_approval_ask_is_the_one_question_path(site):
    """castling-caps C4: any agent asks the person through approval.ask; the
    question waits beside the holds and is answered with approval.decide."""
    async def go():
        hub, tokens = await site()
        try:
            async def call(who, cap, **args):
                _, out = await Client(hub).json("POST", "/call", {"worker": "rook", "cap": cap, "why": WHY,
                                                                  "args": args}, bearer=tokens[who])
                return out

            hub.identities.add_agent("solo", {"grants": ["approval.*"]})
            tokens["solo"] = hub.add_token(Caller(agent="agent:solo"))
            heard = []
            hub.listeners.append(lambda e, p, s: heard.append((e, p.get("hold"))) if e.startswith("approval.") else None)
            out = await call("claude", "approval.ask", question="Ship the docs today?")
            assert out["ok"] and out["result"]["answered"] is False, out
            q1 = out["result"]["hold"]
            assert q1.startswith("q_") and heard == [("approval.asked", q1)]
            assert hub.taints("approval.asked")               # the asker's words are outside content
            out = await call("solo", "approval.ask", question="Which disk?", options=["sda", "sdb"])
            q2 = out["result"]["hold"]                         # nobody behind it: asked of the owners

            _, view = await Client(hub).json("GET", "/holds", bearer=tokens["bake"])
            asks = {h["hold"]: h for h in view["holds"] if h["kind"] == "ask"}
            assert asks[q1]["person"] == "person:bake" and asks[q1]["agent"] == "agent:claude"
            assert asks[q2]["person"] is None and asks[q2]["options"] == ["sda", "sdb"]
            _, view = await Client(hub).json("GET", "/holds", bearer=tokens["kim"])
            assert view["holds"] == []                         # not hers

            out = await call("claude", "approval.decide", hold=q1, answer="yes")
            assert "may not answer" in out["error"]            # never its own question
            out = await call("kim", "approval.decide", hold=q1, answer="yes")
            assert not out["ok"]
            out = await call("claude", "approval.decide", hold=q2, answer="sda")
            assert "may not answer" in out["error"]            # an owners' question: an owner answers
            out = await call("bake", "approval.decide", hold=q2, answer="sdc")
            assert "one of: sda, sdb" in out["error"]

            # the person on the Approvals page
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            t = (await c.open("approval/approvals"))["token"]
            out = await c.page(t, "approval.decide", {"hold": q1, "answer": "Yes, after lunch"})
            assert out["ok"] and out["result"] == {"ok": True, "hold": q1, "answered": True,
                                                   "answer": "Yes, after lunch", "by": "person:bake"}, out
            out = await call("claude", "approval.get", hold=q1)
            assert out["result"]["answer"] == "Yes, after lunch" and out["result"]["by"] == "person:bake"
            assert not (await call("kim", "approval.get", hold=q1))["ok"]
            assert ("approval.answered", q1) in heard
            out = await call("bake", "approval.decide", hold=q1, answer="no")
            assert "answered already" in out["error"]
            assert (await call("bake", "approval.decide", hold=q2, answer="sdb"))["result"]["answered"]

            # a waiting asker gets the answer as it comes; another agent may relay the person's words
            hub.identities.add_agent("house", {"grants": ["*"]})
            house = hub.add_token(Caller(person="person:bake", agent="agent:house"))
            tokens["house"] = house
            waiting = asyncio.ensure_future(call("claude", "approval.ask", question="Merge?", options=["yes", "no"],
                                                 wait=20))
            for _ in range(200):
                q3 = next((a.id for a in hub.asks.open.values() if a.question == "Merge?"), None)
                if q3:
                    break
                await asyncio.sleep(0.02)
            out = await call("house", "approval.decide", hold=q3, answer="yes")
            assert out["ok"] and out["result"]["by"] == "agent:house for person:bake", out
            out = await asyncio.wait_for(waiting, 10)
            assert out["result"] == {"hold": q3, "answered": True, "answer": "yes",
                                     "by": "agent:house for person:bake"}, out
            out = await call("claude", "approval.ask", question="  ")
            assert out["error"].startswith("bad args"), out
        finally:
            await hub.stop()
    run(go())
