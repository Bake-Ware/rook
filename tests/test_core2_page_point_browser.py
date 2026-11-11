"""The shell asks the open page where a control is (bridge.js ``locate``), in
a real browser: the page scrolls the control marked ``data-target`` into
view, marks it, and the shell gets its box in its own coordinates. Core only
(no agents package). Skipped without Playwright and its Chromium."""
from __future__ import annotations

import asyncio

import pytest

from tests.test_core2_dashboard import Client, run
from tests.test_core2_worksurface import start

pw = pytest.importorskip("playwright.async_api")

PAGE = """<!doctype html><html><head><title>Far</title></head><body>
<div style="height:2400px">A long page</div>
<label>Model <select data-target="far.model"><option>opus</option></select></label>
<div data-target="far.hidden" hidden>not shown</div>
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
                await shell.wait_for_function("current && current.page === 'page/far'", timeout=10000)
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
                # nothing there, or nothing shown: null, not a box
                assert await shell.evaluate("locate('far.nothing')") is None
                assert await shell.evaluate("locate('far.hidden')") is None
                await browser.close()
        finally:
            await hub.stop()
    run(asyncio.wait_for(go(), 60))
