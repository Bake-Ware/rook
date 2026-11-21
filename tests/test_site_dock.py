"""The chat dock beside every page (house agent brief HA1), in a real browser: headless Chrome
over the DevTools protocol, as /home/bake/rook/.git/core-briefs/site-render-check.py drives it,
against a real served hub signed in as a person. Skipped where Chrome is not installed.

The agents runtime (brief HA2) is not here yet, so a stand-in answers the three caps the dock
calls, with 0.5's arguments and replies as HA2 builds them: agent.site {dock, ready, look,
setup}; agent.chat.send {agent, text, wait} -> {agent, stream, seq}; agent.chat.read {agent,
after, limit, wait} -> {stream, events: [{seq, at, type, by, data}], next, closed}. It stands in
at the site's call path (Hub.call_or_follow), so its waits are real ones. Each reply it gives
is one the test set for the next message.

With ROOK_DOCK_SHOTS set to a folder, the test leaves screenshots of the dock there."""

import asyncio
import base64
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.request

import pytest
import websockets
import yarl

from helpers import eventually
from rook_hub import Hub
from test_site import browser, login

CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome to drive the dock with")

LOOK = {"sound": False, "rookie": "pixel", "theme": "stone", "name": "Rookie", "next": None,
        "chosen": {"sound": True, "rookie": True, "theme": True, "name": True},
        "styles": ["pixel", "none"], "themes": ["stone"]}     # 0.5's look_state, all four chosen


class StandIn:
    """The house agent's three caps, as the dock sees them, for one person's conversation."""

    def __init__(self, hub):
        self.hub, self.events, self.sent, self.calls = hub, [], [], []
        self.ready, self.setup_done, self.reply = True, True, None
        self.changed = asyncio.Event()

    def _add(self, type, by, data):
        self.events.append({"seq": len(self.events) + 1, "at": time.time(), "type": type, "by": by, "data": data})
        self.changed.set()
        self.changed = asyncio.Event()

    async def answer(self, cap, args):
        self.calls.append(cap)
        if cap == "agent.site":
            return {"dock": "house", "ready": self.ready, "look": LOOK, "setup": {"done": self.setup_done}}
        if cap == "agent.chat.send":
            assert args["agent"] == "house" and args["wait"] is False, args
            self.sent.append(args["text"])
            self._add("input", "person:alice", {"text": args["text"]})
            seq = len(self.events)
            reply, self.reply = self.reply, None
            if reply is not None:
                asyncio.get_running_loop().call_later(0.2, self._add, "turn", "agent:house", {"role": "reply", "text": reply})
            return {"agent": "house", "stream": "s_house_alice", "seq": seq}
        if cap == "agent.chat.read":
            after, wait = args.get("after", 0), args.get("wait", 0)
            if wait and len(self.events) <= after:
                changed = asyncio.ensure_future(self.changed.wait())
                try:
                    await self.hub.waiting(changed, wait)
                finally:
                    changed.cancel()
            out = self.events[after:after + args.get("limit", len(self.events))]
            return {"stream": "s_house_alice" if self.events else None, "events": out,
                    "next": out[-1]["seq"] if out else after, "closed": False}
        raise AssertionError(f"the dock called {cap}")


# Chrome's own work on the internet, off: on a new profile (each test's) it otherwise fetches its
# components, optimization-guide models, a spelling dictionary and more while the test runs, and
# on a busy machine that held a page's script back past the test's waits (brief HA6). Tests reach
# no network but the hub's: everything but loopback goes to a proxy that is not there (port 9) and
# fails at once; Chrome never sends loopback to a proxy.
QUIET = ["--proxy-server=http://127.0.0.1:9",
         "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
         "--disable-extensions", "--disable-client-side-phishing-detection", "--metrics-recording-only",
         "--no-default-browser-check", "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,"
         "OptimizationTargetPrediction,OptimizationHints,Translate,MediaRouter"]


class Chrome:
    """Headless Chrome with one page, driven over the DevTools protocol."""

    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="dock-chrome.")
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                                      "--window-size=1360,850", "--no-first-run", "--enable-unsafe-swiftshader", *QUIET,
                                      "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.n, self.errors = 0, []

    async def __aenter__(self):
        port_file = pathlib.Path(self.profile) / "DevToolsActivePort"
        await eventually(port_file.exists, timeout=30)
        await eventually(lambda: len(port_file.read_text().split()) >= 1, timeout=30)
        port = int(port_file.read_text().split()[0])
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        self.ws = await websockets.connect([t for t in targets if t["type"] == "page"][0]["webSocketDebuggerUrl"],
                                           max_size=50_000_000)
        for domain in ("Network", "Runtime", "Page"):
            await self.cmd(domain + ".enable")
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()
        self.proc.terminate()
        self.proc.wait()
        shutil.rmtree(self.profile, ignore_errors=True)

    async def cmd(self, method, **params):
        self.n += 1
        mine = self.n
        await self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == mine:
                return message.get("result", message)
            if message.get("method") == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                self.errors.append(details.get("exception", {}).get("description", details.get("text", "")))
            if message.get("method") == "Runtime.consoleAPICalled" and message["params"]["type"] == "error":
                self.errors.append(str(message["params"]["args"][0].get("value", "")))

    async def js(self, expression):
        reply = await self.cmd("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in reply, reply
        return reply["result"].get("value")

    async def until(self, expression, timeout=20):
        """Wait until `expression` is truthy in the page (one that throws is not yet); its value. A look
        the page's own navigation cut off (DevTools answers an error, "Execution context was destroyed",
        not a result: the dock opening a page does that) is not yet either."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            reply = await self.cmd("Runtime.evaluate", returnByValue=True, awaitPromise=True,
                                   expression=f"(() => {{ try {{ return ({expression}); }} catch (e) {{ return false; }} }})()")
            assert "exceptionDetails" not in reply, reply
            value = reply["result"].get("value") if "result" in reply else None
            if value:
                return value
            await asyncio.sleep(0.1)
        state = await self.js("JSON.stringify({at: location.pathname, body: document.body.className, "
                              "rookie: [...document.querySelectorAll('.rookie')].map(r => r.className)})")
        raise AssertionError(f"never true in the page: {expression} (the page: {state}; errors: {self.errors})")

    async def shot(self, name):
        folder = os.environ.get("ROOK_DOCK_SHOTS")
        if folder:
            data = (await self.cmd("Page.captureScreenshot", format="png"))["data"]
            (pathlib.Path(folder) / name).write_bytes(base64.b64decode(data))

    async def type(self, text):
        """Type into the dock and send, as a person does (Enter)."""
        await self.js(f"document.querySelector('#dock-text').value = {json.dumps(text)}; "
                      "document.querySelector('#dock-form').requestSubmit(); true")


def test_the_dock_beside_every_page(tmp_path):
    asyncio.run(dock(tmp_path))


async def dock(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    hub.auth.create_user("Alice", "correct horse", first=True)
    agent = StandIn(hub)
    real = hub.call_or_follow

    async def call_or_follow(worker, cap, args, why, **actor):
        if worker == "hub" and cap in ("agent.site", "agent.chat.send", "agent.chat.read"):
            return {"type": "result", "id": "-", "result": await agent.answer(cap, args)}
        return await real(worker, cap, args, why, **actor)
    hub.call_or_follow = call_or_follow
    try:
        async with browser() as client:
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            cookie = client.cookie_jar.filter_cookies(yarl.URL(base))["rook_session"].value
        async with Chrome() as page:
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=base)

            # 1. On every page: the dock, open, with Rookie and the house agent's name. A first
            #    visit tells the agent so, out of the person's sight (0.5's site note).
            for path in ("/workers", "/secrets", "/data", "/auth"):
                await page.cmd("Page.navigate", url=base + path)
                await page.until("document.body.classList.contains('dock-open') && !document.querySelector('#dock').hidden")
                assert await page.js("document.querySelector('#dock-name').textContent") == "Rookie"
                assert await page.js("!!document.querySelector('#rookie-home canvas, .rookie canvas')")
                assert await page.js("!!document.querySelector('main.main .topbar, main.main h1')")    # the page is still there
            assert agent.sent[0].startswith("[site] First visit to the hub site.") and len(agent.sent) == 1
            assert await page.js("document.querySelectorAll('#dock-log .msg.me').length") == 0       # never shown as theirs

            # 2. A message typed goes out with agent.chat.send (wait false), with where the person is;
            #    the reply read back with agent.chat.read shows, under the agent's name.
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("!document.querySelector('#dock').hidden")
            agent.reply = "Hi! I'm **Rookie**. Ask me anything."
            await page.type("Hello Rookie")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.includes('Ask me anything'))")
            assert agent.sent[-1] == "[site] The person is on the page workers (Workers).\nHello Rookie"
            assert await page.js("[...document.querySelectorAll('#dock-log .msg.me .text')].map(t => t.textContent)") == ["Hello Rookie"]
            assert await page.js("document.querySelector('#dock-log .msg.them:last-of-type .text b').textContent") == "Rookie"
            assert await page.js("document.querySelector('#dock-log .msg.them .who b').textContent") == "Rookie"
            await page.shot("dock-workers.png")

            # 3. [[choices: ...]]: buttons under the message; a click sends that answer.
            agent.reply = "Which page next?\n[[choices: Workers | Jobs]]"
            await page.type("Give me a choice")
            buttons = await page.until("[...document.querySelectorAll('#dock-log .widget.choices button')].map(b => b.textContent).join('|')")
            assert buttons == "Workers|Jobs"
            await page.js("[...document.querySelectorAll('#dock-log .widget.choices button')][1].click(); true")
            await eventually(lambda: agent.sent[-1].endswith("\nJobs"))
            assert await page.js("document.querySelector('#dock-log .widget.choices button.picked').textContent") == "Jobs"

            # 4. [[say: ...]]: a speech bubble beside Rookie, a few words.
            agent.reply = "[[say: Hello there]] All set."
            await page.type("Say something")
            await page.until("!document.querySelector('#rookie-say').hidden && document.querySelector('#rookie-say').textContent === 'Hello there'")

            # 5. [[point: ...]]: Rookie points at a target ("page": the page itself), which is marked.
            agent.reply = "This is it. [[point: page | This page]]"
            await page.type("Where am I?")
            await page.until("document.querySelector('main.main').classList.contains('pointed')")
            await page.until("document.querySelector('#rookie-say').textContent === 'This page'")

            # 6. [[open: ...]]: a view the dock does not have says so, as 0.5's does (setup's views,
            #    vault, account, network and look, come with the setup wizard, brief HA3) ...
            agent.reply = "Here it is. [[open: nowhere]]"
            await page.type("Open nowhere")
            await page.until("[...document.querySelectorAll('#dock-log .msg.bad')].some(m => m.textContent === \"There's no nowhere view here yet.\")")
            #    ... and "page <name>" opens that page; the dock is beside it with the conversation.
            agent.reply = "Opening the jobs. [[open: page jobs]]"
            await page.type("Show me the jobs")
            await page.until("location.pathname === '/jobs' && !document.querySelector('#dock').hidden")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.includes('Opening the jobs'))")
            assert await page.js("document.querySelectorAll('#dock-log .msg.me').length") == 7     # all seven, "Jobs" too
            # Rookie is drawn in the chat's head on this later page too (his entrance is once a tab).
            await page.until("(() => { const c = document.querySelector('#rookie-home canvas'); if (!c) return false; "
                             "const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; "
                             "for (let i = 3; i < d.length; i += 4) if (d[i]) return true; return false; })()")
            await page.shot("dock-jobs.png")

            # 7. The chat closes with its X (Rookie waits in the corner) and stays closed on the next page.
            await page.js("document.querySelector('#dock-close').click(); true")
            await page.until("!document.body.classList.contains('dock-open')")
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("!!document.querySelector('#rookie-home') && !document.body.classList.contains('dock-open')")
            await page.until("!!document.querySelector('.rookie.parked')")
            await page.js("document.querySelector('.rookie.parked').click(); true")
            await page.until("document.body.classList.contains('dock-open')")

            # 8. "Getting ready" while no engine answers, as 0.5 shows it: on a hub whose setup is not
            #    finished, the site waits for agent.site's ready before the agent's first turn.
            agent.events.clear()
            agent.ready, agent.setup_done = False, False
            sent = len(agent.sent)
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("!!document.querySelector('#dock-ready') && document.querySelector('#dock-ready').textContent === 'Getting ready…'")
            assert await page.js("document.body.classList.contains('setup')") and len(agent.sent) == sent
            # Setup's two panes, as 0.5's: the work pane (its welcome view) on the left, the chat on the right.
            work, chat = await page.js("[document.querySelector('#work'), document.querySelector('#dock')]"
                                       ".map(e => JSON.stringify(e.getBoundingClientRect()))")
            work, chat = json.loads(work), json.loads(chat)
            assert work["width"] > 400 and chat["left"] >= work["right"] - 1 and chat["width"] > 300
            assert await page.js("!document.querySelector('#work').hidden && document.querySelector('#work').dataset.view === 'idle'")
            await page.shot("dock-getting-ready.png")
            agent.ready = True
            await page.until("!document.querySelector('#dock-ready')")
            await eventually(lambda: len(agent.sent) > sent)
            assert agent.sent[sent].startswith("[site] First visit to a new hub; setup isn't finished.")
            assert "cap agent.skill on worker hub" in agent.sent[sent]        # core's hub is worker "hub", not 0.5's "rook"

            # No errors on any page, from the dock or anything else (WebGL is the background scene's).
            assert [e for e in page.errors if "WebGL" not in e] == []
    finally:
        await hub.stop_serving()
        hub.close()


def test_the_dock_with_the_real_house_agent(tmp_path):
    asyncio.run(real_agent(tmp_path))


async def real_agent(tmp_path):
    """The same dock against the real agent.site, agent.chat.send and agent.chat.read (brief HA2),
    with a real worker whose engine is HA2's stand-in, tests/plugins/echo-engine: it answers each
    message by echoing it, so markup typed into the chat comes back in the agent's reply."""
    from test_house_agent import Rig
    async with Rig(tmp_path) as rig:
        async with browser() as client:
            assert (await login(client, rig.url, "Alice", "a password"))[0] == 200
            cookie = client.cookie_jar.filter_cookies(yarl.URL(rig.url))["rook_session"].value
        async with Chrome() as page:
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=rig.url)
            await page.cmd("Page.navigate", url=rig.url + "/workers")
            await page.until("document.body.classList.contains('dock-open') && !document.querySelector('#dock').hidden")
            # The first visit's note went to the real agent, which answered through its engine.
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.startsWith('echo 1:'))")

            # Typed: sent with agent.chat.send, answered by a turn, read back with agent.chat.read.
            await page.type("Hello Rookie")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.startsWith('echo 2:') && t.textContent.includes('Hello Rookie'))")
            assert await page.js("[...document.querySelectorAll('#dock-log .msg.me .text')].map(t => t.textContent)") == ["Hello Rookie"]

            # Markup in the agent's reply works through the real caps: choices, then a click on one.
            await page.type("[[choices: Yes | No]] [[say: Echoed]]")
            await page.until("[...document.querySelectorAll('#dock-log .widget.choices button')].map(b => b.textContent).join('|') === 'Yes|No'")
            await page.until("document.querySelector('#rookie-say').textContent === 'Echoed'")
            await page.js("document.querySelector('#dock-log .widget.choices button').click(); true")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.startsWith('echo 4:') && t.textContent.includes('Yes'))")
            await page.shot("dock-real-agent.png")

            # The conversation is the hub's: read through the door, it has the same turns.
            events = (await rig.events())["events"]
            assert [e["data"]["text"].split(":")[0] for e in events if e["type"] == "turn" and e["data"].get("role") == "reply"] == \
                ["echo 1", "echo 2", "echo 3", "echo 4"]
            assert [e for e in page.errors if "WebGL" not in e] == []
