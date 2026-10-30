"""browser through the worker host. A small Playwright fake covers the
session rules everywhere (owners, limits, idle reaping, refusals); where
Playwright and its Chromium are installed, real Chromium drives pages from a
local HTTP server. Every test has a hard timeout."""

import asyncio
import base64
import http.server
import sys
import threading
import types
from pathlib import Path

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host
from rook.kit.ctx import Caller

BOB = Caller(person="person:bob", agent="agent:codex", band="b1", why="testing a plugin port")
BAKE = Caller(person="person:bake", agent="agent:claude", band="b1", why="testing a plugin port")


async def _host(tmp_path, **settings) -> Host:
    host = Host(os_name="linux", data_root=tmp_path / "data")
    await host.load(PLUGINS / "browser")
    if settings:
        await host.set_settings("browser", local=settings)
    return host


def _module(host: Host):
    p = host.plugins["browser"]
    return next(iter(p.modules.values()))


# -- pure helpers ------------------------------------------------------------------

def test_urls_and_private_hosts(tmp_path):
    async def body():
        host = await _host(tmp_path)
        await host.call("browser.sessions", {}, ME)       # binds the script
        m = _module(host)
        assert m.check_url("example.com/x") == "https://example.com/x"
        assert m.check_url("about:blank") == "about:blank"
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "ftp://h/x"):
            with pytest.raises(m.Error):
                m.check_url(bad)
        for host_ in ("localhost", "app.localhost", "127.0.0.1", "10.1.2.3", "192.168.1.1", "100.64.0.1",
                      "[::1]", "169.254.0.1"):
            assert await m.private_host(host_), host_
        assert not await m.private_host("8.8.8.8")
        await host.close()
    go(body)


# -- a fake Playwright: sessions, owners, limits ---------------------------------

class _Page:
    def __init__(self, ctx):
        self.ctx, self.url, self.closed = ctx, "about:blank", False

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kw):
        self.url = url
        return types.SimpleNamespace(status=200)

    async def title(self):
        return f"title of {self.url}"

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass


class _Context:
    def __init__(self, browser):
        self.browser, self.pages, self.routed = browser, [], False

    def on(self, event, fn):
        pass

    async def route(self, pattern, fn):
        self.routed = True

    async def new_page(self):
        self.pages.append(_Page(self))
        return self.pages[-1]

    async def close(self):
        self.browser.closed = True


class _Browser:
    def __init__(self, opts):
        self.opts, self.closed = opts, False

    def is_connected(self):
        return not self.closed

    async def new_context(self, **kw):
        self.context_opts = kw
        return _Context(self)

    async def close(self):
        self.closed = True


class _PW:
    launched: list = []
    stopped = 0

    def __init__(self):
        self.chromium = types.SimpleNamespace(launch=self._launch)

    async def _launch(self, **opts):
        _PW.launched.append(opts)
        return _Browser(opts)

    async def start(self):
        return self

    async def stop(self):
        _PW.stopped += 1


@pytest.fixture
def fake_pw(monkeypatch, tmp_path):
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = _PW
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    browsers = tmp_path / "browsers"
    (browsers / "chromium-1").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    _PW.launched, _PW.stopped = [], 0
    return browsers


def test_sessions_belong_to_who_opened_them(tmp_path, fake_pw):
    async def body():
        host = await _host(tmp_path, max_sessions=2)
        opened = await host.call("browser.open", {"url": "example.com"}, BAKE)
        assert opened == {"session": "b1", "url": "https://example.com", "title": "title of https://example.com"}
        assert _PW.launched == [{"headless": True, "chromium_sandbox": True}]
        # bob can't use or close bake's session, and doesn't see it in his error
        with pytest.raises(CallError, match="belongs to person:bake"):
            await host.call("browser.goto", {"session": "b1", "url": "example.org"}, BOB)
        with pytest.raises(CallError, match=r"yours open: none"):
            await host.call("browser.read", {"session": "zz"}, BOB)
        assert (await host.call("browser.close", {"all": True}, BOB)) == {"closed": []}
        # an agent acting for bake is bake
        again = await host.call("browser.open", {"session": "b1"}, Caller(person="person:bake"))
        assert again["reused"] is True
        await host.call("browser.open", {"session": "work"}, BOB)
        with pytest.raises(CallError, match=r"2 browser sessions are open"):
            await host.call("browser.open", {}, BAKE)
        listed = (await host.call("browser.sessions", {}, BAKE))["sessions"]
        assert {(s["session"], s["opened_by"]) for s in listed} == {("b1", "person:bake"), ("work", "person:bob")}
        assert (await host.call("browser.close", {"all": True}, BAKE)) == {"closed": ["b1"]}
        assert (await host.call("browser.close", {"session": "work"}, BOB)) == {"closed": ["work"]}
        assert _PW.stopped == 1                     # Playwright stops with the last session
        await host.close()
    go(body)


def test_refusals_before_chromium_starts(tmp_path, fake_pw):
    async def body():
        host = await _host(tmp_path)
        with pytest.raises(CallError, match="url must be http"):
            await host.call("browser.open", {"url": "file:///etc/passwd"}, BAKE)
        with pytest.raises(CallError, match="bad args"):
            await host.call("browser.open", {"viewport": "big"}, BAKE)
        with pytest.raises(CallError, match="local or private network"):
            await host.call("browser.open", {"url": "http://127.0.0.1:8080/"}, BAKE)
        await host.call("browser.close", {"session": "b1"}, BAKE)
        with pytest.raises(CallError, match="name the session"):
            await host.call("browser.close", {}, BAKE)
        for d in fake_pw.iterdir():
            d.rmdir()
        with pytest.raises(CallError, match="Chromium is not installed"):
            await host.call("browser.open", {}, BAKE)
        await host.set_settings("browser", local={"executable": str(tmp_path / "nope")})
        with pytest.raises(CallError, match="doesn't exist"):
            await host.call("browser.open", {}, BAKE)
        await host.close()
    go(body)


def test_idle_sessions_are_reaped_and_the_guard_is_on_by_default(tmp_path, fake_pw):
    async def body():
        host = await _host(tmp_path, idle_timeout=30)
        await host.call("browser.open", {}, BAKE)
        browser = host.plugins["browser"].instances[("scripts/browser.py", "Browser")]
        s = browser._sessions["b1"]
        assert s.context.routed                         # allow_private is off: requests are guarded
        reap = host.plugins["browser"].resolve("scripts/browser.py:Browser.reap")
        ctx = host._ctx(host.plugins["browser"], BAKE)
        await reap(ctx)
        assert "b1" in browser._sessions
        s.used -= 31
        await reap(ctx)
        assert browser._sessions == {} and s.browser.closed and _PW.stopped == 1
        await host.close()
    go(body)


# -- real Chromium ---------------------------------------------------------------

def _real() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("browser_probe", PLUGINS / "browser" / "scripts" / "browser.py")
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.chromium_installed()


real = pytest.mark.skipif(not _real(), reason="needs Playwright and its Chromium")

PAGES = {
    "/": b"""<html><head><title>Home</title></head><body><main>
<h1>Castling test</h1><p>Hello from the fake site.</p>
<a href="/two">Second page</a>
<form action="/two"><label>Name <input name="q"></label><input type="password" name="pw" value="hunter2">
<select name="c"><option value="r">Red</option><option value="g">Green</option></select>
<button type="submit">Go</button></form></main></body></html>""",
    "/two": b"<html><head><title>Two</title></head><body><main><p>Page two text.</p></main></body></html>",
}


@pytest.fixture
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path.split("?")[0])
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body or b"no")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@real
def test_real_chromium_drives_a_page(tmp_path, site):
    async def body():
        host = await _host(tmp_path, allow_private=True)
        try:
            out = await host.call("browser.open", {"url": site + "/", "snapshot": True}, BAKE)
            assert out["title"] == "Home" and "# Castling test" in out["snapshot"]
            assert "hunter2" not in out["snapshot"] and 'value="***"' in out["snapshot"]
            snap = await host.call("browser.snapshot", {"session": "b1"}, BAKE)
            link = next(line.split("]")[0][1:] for line in snap["text"].splitlines() if "Second page" in line)
            box = next(line.split("]")[0][1:] for line in snap["text"].splitlines() if 'textbox "Name' in line)
            sel = next(line.split("]")[0][1:] for line in snap["text"].splitlines() if line.startswith("[") and " select " in line)
            chose = await host.call("browser.select", {"session": "b1", "target": sel, "option": ["Green"]}, BAKE)
            assert chose["selected"] == ["g"]
            typed = await host.call("browser.type", {"session": "b1", "target": box, "text": "rook"}, BAKE)
            assert typed["url"].endswith("/")
            assert (await host.call("browser.eval", {"session": "b1", "js": "document.querySelector('input[name=q]').value"},
                                    BAKE))["value"] == "rook"
            clicked = await host.call("browser.click", {"session": "b1", "target": link}, BAKE)
            assert clicked["title"] == "Two"
            with pytest.raises(CallError, match="not on this page"):
                await host.call("browser.click", {"session": "b1", "target": box}, BAKE)
            read = await host.call("browser.read", {"session": "b1"}, BAKE)
            assert read["text"] == "Page two text." and read["total"] == len("Page two text.")
            back = await host.call("browser.goto", {"session": "b1", "url": "back"}, BAKE)
            assert back["title"] == "Home"
            shot = await host.call("browser.screenshot", {"session": "b1"}, BAKE)
            assert shot["mime"] == "image/png" and base64.b64decode(shot["data"])[:4] == b"\x89PNG"
            await host.call("browser.goto", {"session": "b1", "url": site + "/two", "new_tab": True}, BAKE)
            tabs = (await host.call("browser.tabs", {"session": "b1"}, BAKE))["tabs"]
            assert [t["current"] for t in tabs] == [False, True]
            switched = await host.call("browser.tab", {"session": "b1", "index": 0}, BAKE)
            assert switched["tabs"][0]["current"]
            left = await host.call("browser.close", {"session": "b1", "tab": 1}, BAKE)
            assert len(left["tabs"]) == 1
            await host.call("browser.wait", {"session": "b1", "text": "Hello from"}, BAKE)
            with pytest.raises(CallError, match="still waiting"):
                await host.call("browser.wait", {"session": "b1", "text": "never there", "timeout": 1}, BAKE)
        finally:
            await host.close()
    go(body, 90)


@real
def test_real_chromium_refuses_the_local_network_by_default(tmp_path, site):
    async def body():
        host = await _host(tmp_path)
        try:
            with pytest.raises(CallError, match="local or private network"):
                await host.call("browser.open", {"url": site + "/"}, BAKE)
            # The session opened; a page's own request to the LAN is blocked too.
            out = await host.call("browser.eval", {"session": "b1", "js":
                                  f"fetch('{site}/two').then(() => 'loaded', () => 'blocked')"}, BAKE)
            assert out["value"] == "blocked"
        finally:
            await host.close()
    go(body, 60)
