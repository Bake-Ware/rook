"""The chat pane in a real browser. The shell asks the open page where a
control is (bridge.js ``locate``; core only): the page scrolls the control
marked ``data-target`` into view, marks it, and the shell gets its box in
its own coordinates. The house agent's chat pops out into its own window
and docks back (the agents package's dock.js). Skipped without Playwright
and its Chromium."""
from __future__ import annotations

import asyncio

import pytest

import time

from tests.test_core2_dashboard import Client, run
from tests.test_core2_worksurface import start

pw = pytest.importorskip("playwright.async_api")

PAGE = """<!doctype html><html><head><title>Far</title></head><body>
<div style="height:2400px">A long page</div>
<label>Model <select data-target="far.model"><option>opus</option></select></label>
<div data-target="far.hidden" hidden>not shown</div>
<details><summary>Folded</summary><details><summary>Deeper</summary>
<input data-target="far.folded" value="inside"></details></details>
<details hidden><summary>Filtered out</summary><input data-target="far.filtered"></details>
</body></html>"""


def test_the_shell_gets_the_box_of_a_target_inside_a_page(tmp_path):
    async def go():
        hub, tokens = await start(tmp_path)
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        made = await c.call("page.create", {"name": "far", "title": "Far", "note": "A test page.", "html": PAGE})
        assert made["ok"], made
        try:
            async with pw.async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as x:          # no Chromium for this Playwright
                    pytest.skip(f"no Chromium: {x}")
                ctx = await browser.new_context(viewport={"width": 1200, "height": 800})
                name, _, value = c.cookie.partition("=")
                await ctx.add_cookies([{"name": name, "value": value, "url": f"http://127.0.0.1:{c.port}"}])
                shell = await ctx.new_page()
                await shell.goto(f"http://127.0.0.1:{c.port}/#/page/far")
                await until(shell, "typeof current !== 'undefined' && current && current.page === 'page/far'")
                frame = shell.frame_locator("#stage iframe")
                await frame.locator("[data-target='far.model']").wait_for(state="attached", timeout=10000)
                box = await shell.evaluate("""async () => {
                  const r = await locate("far.model");
                  const f = current.frame.getBoundingClientRect();
                  return r && {left: r.left, top: r.top, width: r.width, height: r.height,
                               frameTop: f.top, frameBottom: f.bottom, frameLeft: f.left};
                }""")
                assert box, "the page did not answer"
                # scrolled into view: inside the frame on screen, in the shell's coordinates
                assert box["frameTop"] <= box["top"] and box["top"] + box["height"] <= box["frameBottom"], box
                assert box["left"] >= box["frameLeft"] and box["width"] > 0, box
                marked = await frame.locator("[data-target='far.model']").evaluate(
                    "e => e.classList.contains('pointed')")
                assert marked
                # inside closed <details>: opened, scrolled to, its box answered
                folded = await shell.evaluate("locate('far.folded').then(r => r && r.width > 0 && r.top >= current.frame.getBoundingClientRect().top)")
                assert folded
                assert await frame.locator("details").first.evaluate("e => e.open")
                # nothing there, nothing shown, or hidden by the page's own filter: null, not a box
                assert await shell.evaluate("locate('far.filtered')") is None
                assert await shell.evaluate("locate('far.nothing')") is None
                assert await shell.evaluate("locate('far.hidden')") is None
                # a page that ships: the Vault's save button
                await shell.goto(f"http://127.0.0.1:{c.port}/#/secret/vault")
                await until(shell, "typeof current !== 'undefined' && current && current.page === 'secret/vault'")
                await shell.frame_locator("#stage iframe").locator("[data-target='vault.save']").wait_for(state="attached", timeout=10000)
                assert await shell.evaluate("locate('vault.save').then(r => r && r.width > 0)")
                await browser.close()
        finally:
            await hub.stop()
    run(asyncio.wait_for(go(), 60))


async def until(page, expr, seconds=15):
    """Poll ``expr`` in the page (the shell's CSP forbids wait_for_function's eval)."""
    deadline = time.monotonic() + seconds
    while not await page.evaluate(f"() => !!({expr})"):
        assert time.monotonic() < deadline, f"timed out: {expr}"
        await asyncio.sleep(0.2)


def test_the_chat_pops_out_into_its_own_window_and_docks_back(tmp_path):
    """The site design: "The chat can pop out into its own browser window:
    same chat, same stream; closing the window docks it back"."""
    async def go():
        hub, tokens = await start(tmp_path, ("agents",))
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        try:
            async with pw.async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as x:
                    pytest.skip(f"no Chromium: {x}")
                ctx = await browser.new_context(viewport={"width": 1200, "height": 800})
                name, _, value = c.cookie.partition("=")
                await ctx.add_cookies([{"name": name, "value": value, "url": f"http://127.0.0.1:{c.port}"}])
                main = await ctx.new_page()
                await main.goto(f"http://127.0.0.1:{c.port}/")
                await until(main, "document.querySelector('#dock-log .msg.them')")
                async with ctx.expect_page() as popup:
                    await main.click("#dock-pop")
                chat = await popup.value
                assert chat.url.endswith("/?chat")
                await until(main, "pop.out && !document.querySelector('#dock').offsetWidth")
                assert not await chat.is_visible("#nav") and await chat.is_visible("#dock-log")
                # the same conversation: what the main window had, and a reply to the chat window
                await until(chat, "document.querySelector('#dock-log .msg.them')")
                await chat.fill("#dock-text", "hello from the chat window")
                await chat.press("#dock-text", "Enter")
                said = "[...document.querySelectorAll('#dock-log .msg.them')].some(e => e.textContent.includes('brain: hello from the chat window'))"
                await until(chat, said, 30)
                # reloading the main window leaves the chat out, not two chats
                await main.reload()
                await until(main, "typeof pop !== 'undefined' && pop.out")
                assert not await main.is_visible("#dock")
                # closing the chat window docks it back, with the whole conversation
                await chat.close()
                await until(main, "!pop.out && document.querySelector('#dock').offsetWidth")
                await until(main, said)
                await browser.close()
        finally:
            await hub.stop()
    run(asyncio.wait_for(go(), 90))
