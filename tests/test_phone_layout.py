"""The site on a phone-width screen (brief AP1): the house agent's chat is the whole screen, with
one control (#dock-phone) to reach the hub's pages and get back; a wide screen is as it was, the
dock beside the page. The phone app (Settings > Phone app, the work pane's [[open: app]], the
public /app page) draws its QR code itself. In headless Chrome with the dock's stand-in agent
(test_site_dock); skipped where Chrome is not installed.

With ROOK_DOCK_SHOTS set to a folder, the test leaves screenshots there."""

import asyncio
import json

import yarl

from rook_hub import Hub
from test_phone_app import fake_apk
from test_site import browser, login
from test_site_dock import Chrome, StandIn, pytestmark  # noqa: F401  (skipped without Chrome)

PHONE = {"width": 412, "height": 860, "deviceScaleFactor": 2, "mobile": True, "screenWidth": 412, "screenHeight": 860}
WIDE = {"width": 1360, "height": 850, "deviceScaleFactor": 1, "mobile": False, "screenWidth": 1360, "screenHeight": 850}
BOX = "(s => JSON.stringify(document.querySelector(s).getBoundingClientRect()))"
SHOWN = "(s => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none' && e.getClientRects().length > 0; })"


async def screen(page, metrics):
    """The page's screen becomes `metrics`, and the test goes on once the page has its width: a page
    loaded before that (on a busy machine) reads the screen it had, a phone's or a wide one."""
    await page.cmd("Emulation.setDeviceMetricsOverride", **metrics)
    await page.until(f"screen.width === {metrics['width']}")


def test_the_chat_is_the_phone_and_the_dock_is_beside_the_page_on_a_wide_screen(tmp_path):
    asyncio.run(layout(tmp_path))


async def layout(tmp_path):
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
            await screen(page, PHONE)

            # 1. A phone: the chat fills the screen, with "Pages" at the top right and no X.
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("document.body.classList.contains('dock-open') && !document.querySelector('#dock').hidden")
            chat = json.loads(await page.js(f"{BOX}('#dock')"))
            assert (chat["left"], chat["top"], chat["width"], chat["height"]) == (0, 0, 412, 860), chat
            assert await page.js(f"{SHOWN}('#dock-phone')") and not await page.js(f"{SHOWN}('#dock-close')")
            assert await page.js("document.querySelector('#dock-phone').textContent") == "Pages"
            agent.reply = "Hello from **Rookie**."
            await page.type("Hi")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.includes('Hello from'))")
            await page.shot("phone-chat.png")

            # 2. "Pages": the page shows, and the same control says "Chat" ...
            await page.js("document.querySelector('#dock-phone').click(); true")
            await page.until("!document.body.classList.contains('dock-open')")
            assert await page.js("document.querySelector('#dock-phone').textContent") == "Chat"
            assert await page.js(f"{SHOWN}('#dock-phone')") and not await page.js(f"{SHOWN}('#dock')")
            assert await page.js("!!document.querySelector('main.main h1')")
            # ... and stays so from page to page, until it goes back to the chat, where it was.
            await page.cmd("Page.navigate", url=base + "/jobs")
            await page.until("!!document.querySelector('#dock-phone') && document.querySelector('#dock-phone').textContent === 'Chat'")
            assert not await page.js("document.body.classList.contains('dock-open')")
            await page.shot("phone-pages.png")
            await page.js("document.querySelector('#dock-phone').click(); true")
            await page.until("document.body.classList.contains('dock-open')")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.includes('Hello from'))")

            # 3. What the agent opens is shown, the chat out of its way: a page ...
            agent.reply = "Here are the workers. [[open: page workers]]"
            await page.type("Show me the workers")
            await page.until("location.pathname === '/workers' && !!document.querySelector('#dock-phone') && "
                             "!document.body.classList.contains('dock-open')")
            # ... and the phone app's view in the work pane: no APK yet, so it says where one goes.
            await page.js("document.querySelector('#dock-phone').click(); true")
            await page.until("document.body.classList.contains('dock-open')")
            agent.reply = "Here is the app. [[open: app]]"
            await page.type("Put Rook on my phone")
            await page.until("!document.body.classList.contains('dock-open') && "
                             "!!document.querySelector('#work[data-view=app] .phone-app-missing')")
            assert str(tmp_path / "hub" / "app" / "rook.apk") in await page.js("document.querySelector('#work .phone-app-missing').textContent")

            # 4. The phone app's public page, with an APK on the hub: the QR code and the button, no login needed.
            (tmp_path / "hub" / "app").mkdir()
            (tmp_path / "hub" / "app" / "rook.apk").write_bytes(fake_apk())
            await page.cmd("Network.clearBrowserCookies")
            await page.cmd("Page.navigate", url=base + "/app")
            await page.until("!!document.querySelector('.phone-app-qr svg path')")
            assert await page.js("document.querySelector('.phone-app-get').getAttribute('href')") == "/app/rook.apk"
            assert await page.js("document.querySelector('.phone-app-link').textContent") == base + "/app/rook.apk"
            await page.shot("phone-app-page.png")

            # 5. Settings > Phone app shows the same, signed in.
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=base)
            await page.cmd("Page.navigate", url=base + "/settings?section=phone")
            await page.until("!!document.querySelector('main.main .phone-app-qr svg path')")

            # 6. A wide screen is as before: the dock beside the page, its X, and no #dock-phone.
            await screen(page, WIDE)
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("document.body.classList.contains('dock-open') && !document.querySelector('#dock').hidden")
            chat = json.loads(await page.js(f"{BOX}('#dock')"))
            assert chat["width"] == 420 and chat["left"] == 1360 - 420 and chat["height"] == 850, chat
            assert await page.js(f"{SHOWN}('#dock-close')") and not await page.js(f"{SHOWN}('#dock-phone')")
            assert await page.js(f"{SHOWN}('main.main') && {SHOWN}('.sidebar')")
            await page.shot("wide.png")

            assert [e for e in page.errors if "WebGL" not in e] == []
    finally:
        await hub.stop_serving()
        hub.close()


class Held(StandIn):
    """The stand-in agent, whose conversation read with no wait (the dock's first, as a page loads) is
    held while `hold` is set, as a slow phone or hub holds it; `read_again` is set once the dock, its
    load done, starts reading with a wait."""

    def __init__(self, hub):
        super().__init__(hub)
        self.hold, self.read_again = None, asyncio.Event()

    async def answer(self, cap, args):
        if cap == "agent.chat.read" and not args.get("wait") and self.hold is not None:
            await self.hold.wait()
        if cap == "agent.chat.read" and args.get("wait") and self.hold is not None and self.hold.is_set():
            self.read_again.set()
        return await super().answer(cap, args)


def test_a_tap_on_chat_while_the_conversation_loads_is_kept(tmp_path):
    asyncio.run(tap_while_loading(tmp_path))


async def tap_while_loading(tmp_path):
    """Brief AP6: the dock read whether the chat was open, then waited for the conversation, then set the
    chat to what it had read, so a tap on Chat made in between was undone."""
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    hub.auth.create_user("Alice", "correct horse", first=True)
    agent = Held(hub)
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
            await screen(page, PHONE)
            # On the pages, as the person left it.
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("document.body.classList.contains('dock-open')")
            await page.js("document.querySelector('#dock-phone').click(); true")
            await page.until("!document.body.classList.contains('dock-open')")

            # The next page's conversation read is held: the person taps Chat while it loads.
            agent.hold = asyncio.Event()
            await page.cmd("Page.navigate", url=base + "/jobs")
            await page.until("location.pathname === '/jobs' && !!document.querySelector('#dock-phone') && "
                             "!document.querySelector('#dock').hidden")
            assert not await page.js("document.body.classList.contains('dock-open')")
            await page.js("document.querySelector('#dock-phone').click(); true")
            await page.until("document.body.classList.contains('dock-open')")

            # The load ends (the dock reads with a wait again): the chat the person opened stays open.
            agent.hold.set()
            await asyncio.wait_for(agent.read_again.wait(), 20)
            assert await page.js("document.body.classList.contains('dock-open')")
            assert await page.js("document.querySelector('#dock-phone').textContent") == "Pages"

            # The screen turns wide while a page loads, with the phone on the pages: what the phone kept is
            # not written over a wide screen's choice; the wide screen's own (never closed) holds.
            await page.js("document.querySelector('#dock-phone').click(); true")
            await page.until("!document.body.classList.contains('dock-open')")
            agent.hold, agent.read_again = asyncio.Event(), asyncio.Event()
            await page.cmd("Page.navigate", url=base + "/data")
            await page.until("location.pathname === '/data' && !!document.querySelector('#dock-phone') && "
                             "!document.querySelector('#dock').hidden")
            await screen(page, WIDE)
            agent.hold.set()
            await asyncio.wait_for(agent.read_again.wait(), 20)
            assert await page.js("document.body.classList.contains('dock-open')")
            assert await page.js("localStorage.getItem('rook.dock')") != "0"
            assert [e for e in page.errors if "WebGL" not in e] == []
    finally:
        await hub.stop_serving()
        hub.close()
