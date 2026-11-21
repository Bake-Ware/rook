"""A page that is left lets go of the reads the hub holds for it (house agent brief HA7), in headless
Chrome against a served hub, with the dock test's stand-in for the house agent.

On plain HTTP/1.1 a browser keeps six connections to a host. Each page a person leaves goes into
the back-forward cache, and before this brief it kept its dock's 40 s agent.chat.read open on one
of them; six pages left within 40 s and the next page's own requests waited for the oldest of those
reads to end. Now a page being left (pagehide) lets go of every read the hub may hold for it (the
dock's, a terminal's, a long call's followed stream: core's shell.js held()), and a page shown
again from the cache (pageshow) asks again from where it was.

The walk needs no clock: the hub answers each held read at the end of its 40 s by itself, so a
page whose requests wait behind them gets through then, and the test sees that a held read was
answered first."""

import asyncio
import json

import yarl

from rook_hub import Hub
from test_site import browser, login
from test_site_dock import Chrome, StandIn, pytestmark  # noqa: F401  (skipped without Chrome)

PAGES = ["/workers", "/secrets", "/data", "/auth", "/jobs", "/settings", "/settings?section=plugins",
         "/settings?section=hub", "/workers?again"]


class Holding(StandIn):
    """The stand-in, noting the reads that wait (nothing new yet, so the hub holds them): how many began,
    and how many the hub has answered."""

    def __init__(self, hub):
        super().__init__(hub)
        self.begun, self.answered = 0, 0

    async def answer(self, cap, args):
        if cap != "agent.chat.read" or not args.get("wait") or len(self.events) > args.get("after", 0):
            return await super().answer(cap, args)
        self.begun += 1
        try:
            return await super().answer(cap, args)
        finally:
            self.answered += 1


async def until(check):
    while not check():
        await asyncio.sleep(0.02)


async def served(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    hub.auth.create_user("Alice", "correct horse", first=True)
    agent, real = Holding(hub), hub.call_or_follow

    async def call_or_follow(worker, cap, args, why, **actor):
        if worker == "hub" and cap in ("agent.site", "agent.chat.send", "agent.chat.read"):
            return {"type": "result", "id": "-", "result": await agent.answer(cap, args)}
        return await real(worker, cap, args, why, **actor)
    hub.call_or_follow = call_or_follow
    base = f"http://{host}:{port}"
    async with browser() as client:
        assert (await login(client, base, "Alice", "correct horse"))[0] == 200
        cookie = client.cookie_jar.filter_cookies(yarl.URL(base))["rook_session"].value
    return hub, base, agent, cookie


def test_pages_left_hold_no_connection(tmp_path):
    asyncio.run(left(tmp_path))


async def left(tmp_path):
    hub, base, agent, cookie = await served(tmp_path)
    try:
        async with Chrome() as page:
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=base)
            await page.cmd("Page.addScriptToEvaluateOnNewDocument", source=(
                "addEventListener('pagehide', e => { try { localStorage.setItem('left', "
                "(localStorage.getItem('left') || '') + (e.persisted ? 'cached ' : 'unloaded ')); } catch (x) {} })"))
            # Nine pages, one after the other, each as soon as its dock reads with a wait: eight left behind.
            for n, path in enumerate(PAGES, 1):
                begun = agent.begun
                await page.cmd("Page.navigate", url=base + path)
                await until(lambda: agent.begun > begun)          # this page's dock read is at the hub
                assert agent.answered == 0, (
                    f"page {n} ({path}) got its dock read to the hub only after {agent.answered} read(s) of pages "
                    "left before it had been answered: its requests waited behind them")
            left_how = (await page.js("localStorage.getItem('left')")).split()
            assert left_how == ["cached"] * (len(PAGES) - 1), left_how     # every page left went into the cache
            # A file no page has loaded yet, as a page loads one: its response, timed, and nothing answered first.
            took = await page.js("(async () => { const t = performance.now(); "
                                 "await (await fetch('/site/vendor/xterm/xterm.css')).text(); return performance.now() - t; })()")
            print(f"a new file after {len(PAGES) - 1} pages left: {took:.0f} ms; held reads answered first: {agent.answered}")
            assert agent.answered == 0, f"the file waited {took / 1000:.1f} s, behind {agent.answered} answered read(s)"
    finally:
        await hub.stop_serving()
        hub.close()


def test_a_page_shown_again_reads_on(tmp_path):
    asyncio.run(shown_again(tmp_path))


async def shown_again(tmp_path):
    """Back to a page from the back-forward cache: its chat shows what the agent said while it was away,
    and it reads on from there (the next reply shows too)."""
    hub, base, agent, cookie = await served(tmp_path)
    try:
        async with Chrome() as page:
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=base)
            await page.cmd("Page.addScriptToEvaluateOnNewDocument", source=(
                "addEventListener('pageshow', e => { window.shownFromCache = e.persisted; })"))
            begun = agent.begun
            await page.cmd("Page.navigate", url=base + "/workers")
            await until(lambda: agent.begun > begun)
            await page.js("window.firstLoad = true; true")              # gone if the page is loaded again
            begun = agent.begun
            await page.cmd("Page.navigate", url=base + "/jobs")
            await until(lambda: agent.begun > begun)
            agent._add("turn", "agent:house", {"role": "reply", "text": "Said while you were on the jobs."})
            await page.js("history.back(); true")
            await page.until("location.pathname === '/workers' && window.shownFromCache === true")
            assert await page.js("window.firstLoad === true")           # the same page, from the cache
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')]"
                             ".some(t => t.textContent.includes('Said while you were on the jobs.'))")
            agent._add("turn", "agent:house", {"role": "reply", "text": "And this after you came back."})
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')]"
                             ".some(t => t.textContent.includes('And this after you came back.'))")
            texts = await page.js("[...document.querySelectorAll('#dock-log .msg.them .text')].map(t => t.textContent)")
            assert sum("Said while you were on the jobs." in t for t in texts) == 1, texts     # once, not twice
            assert [e for e in page.errors if "WebGL" not in e] == []
    finally:
        await hub.stop_serving()
        hub.close()


class Requests(Chrome):
    """Chrome that notes, in order, each request for a stream's read the page sends. (DevTools says
    nothing more of a request once its page has gone into the back-forward cache, so what the page
    lets go of is seen from inside the page: ABORTS.)"""

    async def __aenter__(self):
        self.reads = []
        return await super().__aenter__()

    async def cmd(self, method, **params):
        self.n += 1
        mine = self.n
        await self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == mine:
                return message.get("result", message)
            event, p = message.get("method"), message.get("params", {})
            if event == "Network.requestWillBeSent" and "/api/streams/" in p["request"]["url"]:
                self.reads.append(p["request"]["url"].split("/api/streams/")[1])
            elif event == "Runtime.exceptionThrown":
                details = p["exceptionDetails"]
                self.errors.append(details.get("exception", {}).get("description", details.get("text", "")))


# Each request the page lets go of (its AbortController's abort), kept where the next page can read it.
ABORTS = ("const fetched = window.fetch; window.fetch = (url, options) => { if (options && options.signal) "
          "options.signal.addEventListener('abort', () => { try { localStorage.setItem('aborted', "
          "(localStorage.getItem('aborted') || '') + location.pathname + ' ' + String(url).replace(location.origin, '') + '\\n'); "
          "} catch (x) {} }); return fetched(url, options); };")

# What the terminal shows, as text (xterm draws a space as a no-break space).
SHOWN = "(document.querySelector('#left-term .xterm-rows') || {textContent: ''}).textContent.replace(/\\u00a0/g, ' ')"


def test_a_terminal_left_lets_go_and_reads_on(tmp_path):
    from test_site_streams import streams
    streams.clear()
    asyncio.run(terminal_left(tmp_path))


async def terminal_left(tmp_path):
    """A terminal's waiting read on a page left is let go of, and back from the cache the terminal
    asks it again from where it was: what the worker sent meanwhile shows, once."""
    from helpers import eventually
    from memory_link import connector
    from rook_worker import Worker
    from test_site_streams import CAPS, streams
    hub, base, agent, cookie = await served(tmp_path)
    alice = hub.auth.list_users()[0]["guid"]
    worker = Worker(tmp_path / "w", base, connector(hub), name="box", caps=CAPS, macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    try:
        await eventually(hub.list_pending)
        box = hub.approve(worker.key, user=alice)["guid"]
        await eventually(lambda: hub.get_worker(box)["online"])
        moniker = (await hub.call(box, "demo.stream", {"title": "build"}, "a terminal to leave", user=alice))["stream"]
        await streams[0].send(b"before you left\r\n")
        async with Requests() as page:
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=base)
            await page.cmd("Page.addScriptToEvaluateOnNewDocument", source=ABORTS + (
                "addEventListener('pageshow', e => { window.shownFromCache = e.persisted; })"))
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("document.body.classList.contains('dock-open')")
            await page.js("import('/site/terminal.js').then(m => Vue.createApp(m.default, {stream: {moniker: "
                          f"{json.dumps(moniker)}, open: true, title: 'build'}}}})"
                          ".mount(document.body.appendChild(Object.assign(document.createElement('div'), "
                          "{id: 'left-term', style: 'position: fixed; top: 0; left: 0; width: 640px; z-index: 99'})))); true")
            await page.until(f"{SHOWN}.includes('before you left')")
            waiting = f"{moniker}/read?from=1&wait=25"
            while waiting not in page.reads:                # the terminal, caught up, reads with a wait
                await page.js("true")

            await page.cmd("Page.navigate", url=base + "/jobs")
            await page.until("document.body.classList.contains('dock-open')")
            aborted = (await page.js("localStorage.getItem('aborted')") or "").splitlines()
            assert f"/workers /api/streams/{waiting}" in aborted, aborted      # let go of as its page was left
            await streams[0].send(b"while you were away\r\n")
            await eventually(lambda: hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == 2)
            before = len(page.reads)

            await page.js("history.back(); true")
            await page.until("location.pathname === '/workers' && window.shownFromCache === true")
            await page.until(f"{SHOWN}.includes('while you were away')")
            assert page.reads[before] == waiting, page.reads[before:]         # asked again, from where it was
            text = await page.js(SHOWN)
            assert text.count("before you left") == 1 and text.count("while you were away") == 1, text
            assert [e for e in page.errors if "WebGL" not in e] == []
    finally:
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub.close()
