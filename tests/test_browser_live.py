"""browser.* against a real headless Chromium and a local page: open,
snapshot, type, select, check, submit, screenshot, close. Skipped where
Playwright or its Chromium is not installed (CI, most dev machines)."""

import asyncio
import functools
import http.server
import threading
import types

import pytest

from rook.core.plugin import SettingsView
from rook.core.pluginkit import CapError
from rook.worker.plugins import browser

PAGE = """<!doctype html><html><head><title>Form</title></head><body><main>
<h1>Sign up</h1><p>Plain words.</p>
<form action="/done.html"><label for="n">Name</label> <input id="n" name="name">
<select name="c"><option value="r">Red</option><option value="g">Green</option></select>
<label><input type="checkbox" name="news"> News</label><button>Send</button></form>
<div style="display:none"><button>Hidden</button></div></main></body></html>"""
DONE = "<!doctype html><title>Done</title><h1>Thanks</h1><p id=q></p>" \
       "<script>q.textContent = location.search</script>"


def _usable() -> bool:
    p = browser.BrowserPlugin()
    p.__dict__["_settings"] = SettingsView(p, {})
    return p.available()


pytestmark = pytest.mark.skipif(not _usable(), reason="Playwright + Chromium not installed")


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    (tmp_path / "done.html").write_text(DONE)
    hits = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            hits.append(self.path)
    handler = functools.partial(Handler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield types.SimpleNamespace(url=f"http://127.0.0.1:{srv.server_address[1]}", hits=hits)
    srv.shutdown()


def test_form_round_trip(tmp_path, site):
    async def go():
        p = browser.BrowserPlugin()
        p.__dict__["_settings"] = SettingsView(p, {"allow_private": True})
        p.__dict__["_data_root"] = str(tmp_path / "data")
        try:
            r = await p.open(session="t", url=site.url + "/index.html")
            assert r["title"] == "Form"
            snap = await p.snapshot(session="t")
            lines = snap["text"].splitlines()
            assert lines[0] == "# Sign up" and "Plain words." in lines
            assert '[e1] textbox "Name"' in lines
            assert "Hidden" not in snap["text"] and snap["refs"] == 4
            await p.type(session="t", target="e1", text="Bake")
            assert (await p.select(session="t", target="e2", option="Green"))["selected"] == ["g"]
            await p.click(session="t", target="e3")
            snap = await p.snapshot(session="t")
            # Same elements keep their refs across snapshots.
            assert '[e1] textbox "Name" value="Bake"' in snap["text"]
            assert '[e3] checkbox "News" checked' in snap["text"]
            shot = await p.screenshot(session="t", inline=True)
            assert shot["bytes"] > 1000 and shot["format"] == "jpeg"
            done = await p.click(session="t", target="text=Send", snapshot=True)
            assert done["title"] == "Done" and "name=Bake&c=g&news=on" in done["snapshot"]
            with pytest.raises(CapError, match="new browser.snapshot"):
                await p.click(session="t", target="e1")
            assert (await p.read(session="t"))["text"].startswith("Thanks")
            await p.goto(session="t", url="back")
            again = await p.snapshot(session="t")
            # A new document gets new numbers, so old refs can't hit new elements.
            assert "[e5] textbox" in again["text"] and "[e1]" not in again["text"]
        finally:
            await p.close(all=True)
            await p.stop()
        assert p._pw is None
    asyncio.run(go())


def test_local_network_refused_by_default(tmp_path, site):
    async def go():
        p = browser.BrowserPlugin()
        p.__dict__["_settings"] = SettingsView(p, {})
        p.__dict__["_data_root"] = str(tmp_path / "data")
        try:
            with pytest.raises(CapError, match="allow_private is off"):
                await p.open(session="t", url=site.url + "/index.html")
            # Nor can a page's own requests reach it: the server never sees them.
            await p.goto(session="t", url="about:blank")
            await p.eval(session="t", js=f"new Promise(ok => {{ const i = new Image(); "
                                         f"i.onload = i.onerror = () => ok(1); i.src = '{site.url}/probe'; }})")
            assert site.hits == []
        finally:
            await p.close(all=True)
            await p.stop()
    asyncio.run(go())
