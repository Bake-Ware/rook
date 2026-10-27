"""browser.* worker plugin against a fake Playwright: availability gating,
session lifecycle and limits, target resolution, URL rules and error text.
tests/test_browser_live.py drives a real Chromium where one is installed."""

import asyncio
import os
import sys
import time
import types
from pathlib import Path

import pytest

from rook.core.plugin import SettingsView
from rook.core.pluginkit import ArgError, CapError
from rook.worker.plugins import browser


# -- a fake Playwright ---------------------------------------------------------

class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    async def count(self):
        return self.page.matches.get(self.selector, 0)

    async def click(self, timeout=None):
        self.page.actions.append(("click", self.selector))

    async def fill(self, text, timeout=None):
        self.page.actions.append(("fill", self.selector, text))

    async def press(self, key, timeout=None):
        self.page.actions.append(("press", self.selector, key))

    async def evaluate(self, js):
        return False  # not a target=_blank link


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.closed = False
        self.matches = {}
        self.actions = []

    def is_closed(self):
        return self.closed

    async def title(self):
        return "T:" + self.url

    async def goto(self, url, wait_until=None, timeout=None):
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        if "redirect-to-lan" in url:
            url = "http://192.168.1.1/"  # a redirect hop the route never saw
        if "unreachable" in url:
            raise RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED at " + url
                               + "\nCall log:\n  - navigating to ...")
        self.url = url
        return types.SimpleNamespace(status=200)

    async def wait_for_load_state(self, state, timeout=None):
        return None

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def bring_to_front(self):
        return None

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, browser, **opts):
        self.browser, self.opts = browser, opts
        self.pages = []
        self.closed = False
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn

    async def route(self, pattern, handler):
        self.handlers["route"] = handler

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        self.handlers["page"](page)  # Playwright fires "page" for every new page
        return page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, opts):
        self.opts = opts
        self.contexts = []
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def new_context(self, **opts):
        ctx = FakeContext(self, **opts)
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launched = []

    async def launch(self, **opts):
        b = FakeBrowser(opts)
        self.launched.append(b)
        return b

    async def launch_persistent_context(self, *a, **k):  # must never be used
        raise AssertionError("browser plugin must not use a persistent profile")


class FakePW:
    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_pw(monkeypatch):
    state = types.SimpleNamespace(started=[])

    async def private(host):  # no real DNS in unit tests
        return host in ("localhost", "router.lan") or host.startswith(("127.", "192.168.", "10."))
    monkeypatch.setattr(browser, "_private_host", private)

    class Starter:
        async def start(self):
            pw = FakePW()
            state.started.append(pw)
            return pw

    api = types.ModuleType("playwright.async_api")
    api.async_playwright = lambda: Starter()
    pkg = types.ModuleType("playwright")
    pkg.__path__ = []
    pkg.async_api = api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    return state


def make(tmp_path, **stored):
    p = browser.BrowserPlugin()
    p.__dict__["_settings"] = SettingsView(p, stored)
    p.__dict__["_data_root"] = str(tmp_path)
    return p


def run(coro):
    return asyncio.run(coro)


# -- availability ----------------------------------------------------------------

def test_off_without_playwright(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    p = make(tmp_path)
    assert p.available() is False
    assert "pip install" in p.unavailable_reason()


def test_off_without_chromium_then_on(tmp_path, monkeypatch, fake_pw):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))
    p = make(tmp_path)
    assert p.available() is False
    assert "playwright install chromium" in p.unavailable_reason()
    (tmp_path / "pw" / "chromium_headless_shell-1248").mkdir(parents=True)
    assert p.available() is True


def test_executable_setting_must_exist(tmp_path, fake_pw):
    p = make(tmp_path, executable=str(tmp_path / "nope"))
    assert p.available() is False
    assert "does not exist" in p.unavailable_reason()
    exe = tmp_path / "chromium"
    exe.write_text("")
    assert make(tmp_path, executable=str(exe)).available() is True


def test_every_cap_declares_risk_scope_and_doc():
    for name, fn in browser.BrowserPlugin.declared_caps().items():
        meta = fn._rook_cap_meta
        assert meta.risk and meta.scope and fn.__doc__, name
    tiers = {n: f._rook_cap_meta.risk for n, f in browser.BrowserPlugin.declared_caps().items()}
    assert tiers["browser.eval"] == "exec"
    assert tiers["browser.snapshot"] == tiers["browser.read"] == "read"


# -- urls and errors ---------------------------------------------------------------

@pytest.mark.parametrize("url,want", [
    ("https://example.com", "https://example.com"),
    ("example.com/a", "https://example.com/a"),
    ("http://127.0.0.1:8000/x", "http://127.0.0.1:8000/x"),
    ("about:blank", "about:blank"),
])
def test_urls_allowed(url, want):
    assert browser._check_url(url) == want


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
                                 "chrome://settings", "ftp://x/y", "view-source:https://x", ""])
def test_urls_refused(url):
    with pytest.raises(ArgError):
        browser._check_url(url)


def test_clean_error_drops_call_log_and_colour():
    exc = RuntimeError("\x1b[31mLocator.click: Timeout 10000ms exceeded.\x1b[0m\n"
                       "Call log:\n  - waiting for locator('x')\n    - retrying")
    assert browser._clean(exc) == "Locator.click: Timeout 10000ms exceeded."


# -- sessions -----------------------------------------------------------------------

def test_open_is_isolated_and_named(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        r1 = await p.open(url="example.com")
        r2 = await p.open()
        assert (r1["session"], r2["session"]) == ("b1", "b2")
        assert r1["url"] == "https://example.com" and r1["title"] == "T:https://example.com"
        assert "note" not in r1  # the first page is not a popup
        pw = fake_pw.started[0]
        launch = pw.chromium.launched[0].opts
        assert launch["headless"] is True and launch["chromium_sandbox"] is True
        ctx = pw.chromium.launched[0].contexts[0]
        assert ctx.opts["accept_downloads"] is False
        assert ctx.opts["viewport"] == {"width": 1280, "height": 800}
        again = await p.open(session="b1", url="https://example.org")
        assert again["reused"] is True and again["url"] == "https://example.org"
        assert len(pw.chromium.launched) == 2
        listed = (await p.sessions())["sessions"]
        assert [s["session"] for s in listed] == ["b1", "b2"]
        await p.stop()
        assert pw.stopped and all(b.closed for b in pw.chromium.launched)
    run(go())


def test_session_limit_and_close_stops_playwright(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path, max_sessions=1)
        await p.open(session="one")
        with pytest.raises(CapError, match="most allowed"):
            await p.open(session="two")
        assert (await p.close(session="one")) == {"closed": ["one"]}
        assert fake_pw.started[0].stopped and p._pw is None
        with pytest.raises(CapError, match="no browser session 'one'"):
            await p.snapshot(session="one")
        await p.stop()
    run(go())


def test_bad_session_name_and_viewport(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        with pytest.raises(ArgError):
            await p.open(session="has space")
        with pytest.raises(ArgError):
            await p.open(viewport="big")
        assert p._sessions == {}
    run(go())


def test_headed_needs_a_display(tmp_path, fake_pw, monkeypatch):
    monkeypatch.setattr(browser, "has_display", lambda: False)

    async def go():
        p = make(tmp_path)
        with pytest.raises(CapError, match="desktop session"):
            await p.open(headless=False)
    run(go())


def test_idle_sessions_are_reaped(tmp_path, fake_pw, monkeypatch):
    real_sleep = asyncio.sleep

    async def fast(_):
        await real_sleep(0)
    monkeypatch.setattr(browser.asyncio, "sleep", fast)

    async def go():
        p = make(tmp_path, idle_timeout=60)
        await p.open(session="old")
        await p.open(session="new")
        p._sessions["old"].used = time.monotonic() - 3600
        for _ in range(20):
            await real_sleep(0)
            if "old" not in p._sessions:
                break
        assert list(p._sessions) == ["new"]
        await p.stop()
    run(go())


# -- acting on a page ------------------------------------------------------------------

def test_refs_and_selectors_resolve_to_one_element(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s")
        page = p._sessions["s"].page
        attr = p._sessions["s"].attr
        assert attr.startswith("data-rook-") and attr != "data-rook-ref"  # not guessable
        page.matches = {f'[{attr}="e3"]': 1, "button": 2, "#go": 1}
        await p.click(session="s", target="e3")
        await p.type(session="s", target="#go", text="hi", submit=True)
        assert page.actions == [("click", f'[{attr}="e3"]'), ("fill", "#go", "hi"),
                                ("press", "#go", "Enter")]
        with pytest.raises(CapError, match="take a new browser.snapshot"):
            await p.click(session="s", target="e9")
        with pytest.raises(CapError, match="matches 2 elements"):
            await p.click(session="s", target="button")
        with pytest.raises(CapError, match="nothing matches"):
            await p.click(session="s", target=".missing")
        await p.stop()
    run(go())


def test_navigation_errors_are_one_line(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s")
        with pytest.raises(CapError) as e:
            await p.goto(session="s", url="https://unreachable.invalid")
        assert str(e.value) == "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://unreachable.invalid"
        with pytest.raises(ArgError):
            await p.goto(session="s", url="file:///etc/passwd")
        await p.stop()
    run(go())


def test_popup_becomes_current_and_is_reported(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s", url="https://a.example")
        s = p._sessions["s"]
        popup = FakePage(s.context)
        popup.url = "https://b.example"
        s.context.pages.append(popup)
        s.context.handlers["page"](popup)
        out = await p._reply(s)
        assert out["url"] == "https://b.example" and out["tab"] == 1 and "new tab" in out["note"]
        tabs = (await p.tabs(session="s", switch=0))["tabs"]
        assert [t["current"] for t in tabs] == [True, False]
        await p.stop()
    run(go())


def test_crashed_browser_frees_the_session(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s")
        p._sessions["s"].browser.closed = True  # Chromium died under us
        with pytest.raises(CapError, match="has exited"):
            await p.snapshot(session="s")
        assert p._sessions == {}
        assert (await p.open(session="s"))["session"] == "s"
        await p.stop()
    run(go())


# -- review fixes: owners, local network, bounds ------------------------------------------

def test_sessions_belong_to_their_opener(tmp_path, fake_pw):
    from rook.core.context import caller_identity

    async def go():
        p = make(tmp_path)
        tok = caller_identity.set("alice")
        await p.open(session="mine")
        caller_identity.reset(tok)
        tok = caller_identity.set("bob")
        try:
            with pytest.raises(CapError, match="belongs to alice"):
                await p.snapshot(session="mine")
            with pytest.raises(CapError, match="belongs to alice"):
                await p.open(session="mine")
            await p.open(session="bobs")
            assert (await p.close(all=True))["closed"] == ["bobs"]
            listed = (await p.sessions())["sessions"]
            assert [(x["session"], x["opened_by"]) for x in listed] == [("mine", "alice")]
        finally:
            caller_identity.reset(tok)
        await p.stop()
    run(go())


def test_local_network_refused_unless_allowed(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s")
        ctx = p._sessions["s"].context
        assert "route" in ctx.handlers  # every request passes the guard
        for url in ("http://localhost:8000/", "http://192.168.1.1/", "http://router.lan/x"):
            with pytest.raises(CapError, match="allow_private is off"):
                await p.goto(session="s", url=url)
        # A redirect hop is not routed: the landing check catches it.
        with pytest.raises(CapError, match="192.168.1.1 is on a local"):
            await p.goto(session="s", url="https://redirect-to-lan.example")
        assert p._sessions["s"].page.url == "about:blank"
        await p.goto(session="s", url="https://example.com")

        aborted = []
        req = lambda u: types.SimpleNamespace(  # noqa: E731
            request=types.SimpleNamespace(url=u),
            abort=lambda why: _note(aborted, ("abort", u, why)),
            continue_=lambda: _note(aborted, ("go", u)))
        await ctx.handlers["route"](req("http://10.0.0.5/img.png"))
        await ctx.handlers["route"](req("https://example.com/app.js"))
        assert aborted == [("abort", "http://10.0.0.5/img.png", "blockedbyclient"),
                           ("go", "https://example.com/app.js")]
        await p.stop()

        q = make(tmp_path, allow_private=True)
        await q.open(session="dev", url="http://localhost:8000/")
        assert "route" not in q._sessions["dev"].context.handlers
        await q.stop()
    run(go())


async def _note(log, item):
    log.append(item)


@pytest.mark.parametrize("addr,private", [
    ("127.0.0.1", True), ("10.1.2.3", True), ("192.168.0.9", True), ("100.101.102.103", True),
    ("169.254.169.254", True), ("::1", True), ("fe80::1", True), ("::ffff:192.168.1.1", True),
    ("93.184.215.14", False), ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", False),
])
def test_what_counts_as_private(addr, private):
    assert browser._not_public(addr) is private


def test_open_passes_its_timeout(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s", url="https://example.com", timeout=7)
        assert p._sessions["s"].page.timeouts == [7000]
        with pytest.raises(ArgError):
            await p.open(session="t", url="https://example.com", timeout=999)
        await p.stop()
    run(go())


def test_popup_storm_is_capped(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s")
        s = p._sessions["s"]
        for _ in range(browser._TABS_MAX + 3):
            page = FakePage(s.context)
            s.context.pages.append(page)
            s.context.handlers["page"](page)
        await asyncio.sleep(0)
        assert len(s.pages()) == browser._TABS_MAX
        await p.stop()
    run(go())


def test_read_pages_and_eval_is_cut(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        await p.open(session="s")
        page = p._sessions["s"].page
        text = "word " * 100

        async def evaluate(js, arg=None):
            return text if arg is None and "innerText" in js else {"big": "x" * 5000}
        page.evaluate = evaluate
        first = await p.read(session="s", max_chars=100)
        assert len(first["text"]) == 100 and first["next_offset"] == 100 and first["total"] == len(text.strip())
        last = await p.read(session="s", max_chars=1000, offset=400)
        assert "next_offset" not in last
        out = await p.eval(session="s", js="() => 1", max_chars=200)
        assert out["truncated"] is True and len(out["value"]) == 200
        await p.stop()
    run(go())


def test_screenshots_keep_the_newest_by_time(tmp_path, fake_pw):
    async def go():
        p = make(tmp_path)
        shots = p.data_dir / "shots"
        shots.mkdir(parents=True)
        # Session "a" is newer than "z": name order would delete a's first.
        for i in range(browser._SHOTS_KEPT + 2):
            f = shots / (("z" if i < 2 else "a") + f"-{i:02d}.png")
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        await p.open(session="s")
        page = p._sessions["s"].page

        async def screenshot(path=None, **kw):
            Path(path).write_bytes(b"png")
        page.screenshot = screenshot
        out = await p.screenshot(session="s")
        left = sorted(f.name for f in shots.glob("*.png"))
        assert not any(n.startswith("z-") for n in left) and Path(out["path"]).name in left
        assert len(left) == browser._SHOTS_KEPT
        await p.stop()
    run(go())
