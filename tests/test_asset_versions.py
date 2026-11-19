"""Site files carry a version in their address (house agent brief HA5), over real HTTP on localhost.

Cloudflare in front of a hub keeps scripts and stylesheets for hours whatever the hub says, so a
browser could run an old shell.js against a new hub. Every file the pages load is now named with a
short hash of its bytes (`?v=`), which changes when the file does. The page shell names its own
files that way and carries an import map from each plain address to its versioned one, so a
module's `import ... from '/site/shell.js'` reaches the address the page loaded; /api/session gives
plugins' files versioned. A file asked for at its current version may be kept for good; the same
file without a version (or at an old one) is no-cache, as before. The last test walks every address
the shell, core's pages, the dock and a plugin's page load in headless Chrome."""

import asyncio
import hashlib
import json
import pathlib
import re
import shutil

import yarl

from rook_hub import Hub
from rook_hub.site import FILES
from test_site import browser, login

SHELF = pathlib.Path(__file__).parent / "plugins" / "shelf"
AGENTS = pathlib.Path(__file__).parents[1] / "hub" / "rook_hub" / "defaults" / "agents"
FOR_GOOD = "public, max-age=31536000, immutable"


def version(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()[:12]


def import_map(html):
    return json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S)[1])["imports"]


async def text(client, url):
    async with client.get(url) as response:
        assert response.status == 200, url
        return await response.text()


def test_versions_over_http(tmp_path):
    asyncio.run(over_http(tmp_path))


async def over_http(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    hub.auth.create_user("Alice", "correct horse")
    try:
        async with browser() as client, browser() as stranger:
            # 1. The page shell names its files at their versions: a hash of each file's bytes.
            async with stranger.get(base + "/") as response:
                assert response.headers["Cache-Control"] == "no-cache"
                html, etag = await response.text(), response.headers["ETag"]
            for plain in ("/site/assets/favicon.png", "/site/style.css", "/site/vendor/vue.global.prod.js",
                          "/site/shell.js"):
                assert f'"{plain}?v={version(FILES / plain[len("/site/"):])}"' in html, plain
            assert not re.search(r'(src|href)="/site/[^"?]+"', html)                 # none without one
            async with stranger.get(base + "/", headers={"If-None-Match": etag}) as response:
                assert response.status == 304                                       # unchanged: 304, as before

            # 2. Its import map holds every file under /site/ (page modules, components, vendored
            #    code), each at its version; plugins' files only for a person signed in.
            imports = import_map(html)
            files = [p for p in FILES.rglob("*") if p.is_file()]
            assert len(imports) == len(files)
            for p in files:
                plain = "/site/" + p.relative_to(FILES).as_posix()
                assert imports[plain] == f"{plain}?v={version(p)}", plain
            assert "/site/pages/workers.js" in imports and "/site/vendor/xterm/xterm.css" in imports

            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            signed = import_map(await text(client, base + "/workers"))
            for file in ("dock.css", "rookie.js", "rookie-map.js", "chat-widgets.js", "dock.js"):
                plain = f"/plugin-files/agents/shell/{file}"
                assert signed[plain] == f"{plain}?v={version(AGENTS / 'shell' / file)}", plain

            # 3. /api/session gives the dock's shell files, and a plugin's page, settings section
            #    and component, at their versions.
            async with client.post(base + "/api/plugins", json={"path": str(copy(SHELF, tmp_path / "shelf"))}) as r:
                assert r.status == 200, await r.text()
            session = json.loads(await text(client, base + "/api/session"))
            shell = {part["url"].split("?")[0]: part["url"] for part in session["shell"]}
            assert shell["/plugin-files/agents/shell/dock.js"] == \
                f"/plugin-files/agents/shell/dock.js?v={version(AGENTS / 'shell' / 'dock.js')}"
            assert all(re.search(r"\?v=[0-9a-f]{12}\Z", url) for url in shell.values()), shell
            page = [p for s in session["nav"] for p in s["pages"] if p["id"] == "shelf/shelf"][0]
            assert page["script"] == f"/plugin-files/shelf/site/shelf.js?v={version(SHELF / 'site' / 'shelf.js')}"
            assert session["components"][0]["script"].endswith(f"book-card.js?v={version(SHELF / 'site' / 'book-card.js')}")
            settings = json.loads(await text(client, base + "/api/settings"))
            assert [s["script"] for s in settings["sections"] if s.get("plugin") == "shelf"] == \
                [f"/plugin-files/shelf/site/settings.js?v={version(SHELF / 'site' / 'settings.js')}"]

            # 4. At its current version a file may be kept for good; without one, or at another, it
            #    is no-cache, as before. The same for a plugin's file (to a session, as before).
            shell_js = imports["/site/shell.js"]
            for url, who, cache in [(shell_js, stranger, FOR_GOOD), ("/site/shell.js", stranger, "no-cache"),
                                    ("/site/shell.js?v=000000000000", stranger, "no-cache"),
                                    (imports["/site/assets/rook-logo.png"], stranger, FOR_GOOD),
                                    (shell["/plugin-files/agents/shell/dock.js"], client, FOR_GOOD),
                                    ("/plugin-files/agents/shell/dock.js", client, "no-cache"),
                                    (page["script"], client, FOR_GOOD)]:
                async with who.get(base + url) as response:
                    assert response.status == 200, url
                    assert response.headers["Cache-Control"] == cache, url
            async with stranger.get(base + shell["/plugin-files/agents/shell/dock.js"]) as response:
                assert response.status == 401 and response.headers["Cache-Control"] == "no-cache"
            async with stranger.get(base + "/?v=" + version(FILES / "index.html")) as response:
                assert response.headers["Cache-Control"] == "no-cache"              # the page itself never
            async with client.get(base + "/api/session?v=1") as response:
                assert response.headers["Cache-Control"] == "no-store"              # nor an API answer

            # 5. A plugin installed again with a changed file: its address changes on the next load,
            #    and the old address is no longer kept for good.
            changed = copy(SHELF, tmp_path / "shelf2")
            (changed / "site" / "shelf.js").write_text((changed / "site" / "shelf.js").read_text() + "\n// changed\n")
            async with client.post(base + "/api/plugins", json={"path": str(changed)}) as r:
                assert r.status == 200, await r.text()
            session = json.loads(await text(client, base + "/api/session"))
            again = [p for s in session["nav"] for p in s["pages"] if p["id"] == "shelf/shelf"][0]["script"]
            assert again == f"/plugin-files/shelf/site/shelf.js?v={version(changed / 'site' / 'shelf.js')}" != page["script"]
            assert import_map(await text(client, base + "/plugins/shelf/shelf"))["/plugin-files/shelf/site/shelf.js"] == again
            async with client.get(base + again) as response:
                assert response.headers["Cache-Control"] == FOR_GOOD and "// changed" in await response.text()
            async with client.get(base + page["script"]) as response:
                assert response.headers["Cache-Control"] == "no-cache"
    finally:
        await hub.stop_serving()


def copy(folder, to):
    shutil.copytree(folder, to)
    return to


def test_every_address_a_browser_loads_carries_a_version(tmp_path):
    from test_site_dock import CHROME
    import pytest
    if CHROME is None:
        pytest.skip("no Chrome to load the site with")
    asyncio.run(in_chrome(tmp_path))


def watching(Chrome):
    """Chrome that notes every response the page gets, with its Cache-Control, as the briefs'
    net-look.py does: what the browser asked for, not only what it kept."""
    class Watching(Chrome):
        async def cmd(self, method, **params):
            self.n += 1
            mine = self.n
            await self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
            while True:
                message = json.loads(await self.ws.recv())
                if message.get("id") == mine:
                    return message.get("result", message)
                if message.get("method") == "Network.responseReceived":
                    response = message["params"]["response"]
                    headers = {k.lower(): v for k, v in response["headers"].items()}
                    self.responses.append((response["url"], response["status"], headers.get("cache-control")))
                if message.get("method") == "Runtime.exceptionThrown":
                    details = message["params"]["exceptionDetails"]
                    self.errors.append(details.get("exception", {}).get("description", details.get("text", "")))
    Watching.responses = None
    return Watching


async def in_chrome(tmp_path):
    from test_site_dock import Chrome, StandIn
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    hub.auth.create_user("Alice", "correct horse", first=True)
    agent, real = StandIn(hub), hub.call_or_follow

    async def call_or_follow(worker, cap, args, why, **actor):     # the house agent, as the dock test has it
        if worker == "hub" and cap in ("agent.site", "agent.chat.send", "agent.chat.read"):
            return {"type": "result", "id": "-", "result": await agent.answer(cap, args)}
        return await real(worker, cap, args, why, **actor)
    hub.call_or_follow = call_or_follow
    try:
        async with browser() as client:
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            async with client.post(base + "/api/plugins", json={"path": str(SHELF)}) as r:
                assert r.status == 200, await r.text()
            cookie = client.cookie_jar.filter_cookies(yarl.URL(base))["rook_session"].value
        async with watching(Chrome)() as page:
            page.responses = []
            await page.cmd("Network.setCookie", name="rook_session", value=cookie, url=base)
            # The shell and the dock on core's pages, the Settings page with a plugin's section, and a
            # plugin's page with its component. Every module the shell imports is a request of its own.
            for path in ("/workers", "/jobs", "/data", "/settings", "/settings?section=plugins",
                         "/settings?section=shelf/shelf", "/plugins/shelf/shelf"):
                await page.cmd("Page.navigate", url=base + path)
                await page.until("document.body.classList.contains('dock-open') && !document.querySelector('#dock').hidden")
                await page.until("!!document.querySelector('main.main h1')")
                await asyncio.sleep(1)
            # A terminal's xterm (its modules and its stylesheet), loaded as a terminal loads it.
            await page.js("import('/site/terminal.js').then(m => Vue.createApp(m.default, {stream: {moniker: 'none', "
                          "open: false, title: 'none'}}).mount(document.body.appendChild(document.createElement('div')))); true")
            await page.until("['xterm.css', 'xterm.mjs', 'addon-fit.mjs'].every(f => "
                             "performance.getEntriesByType('resource').some(e => e.name.includes(f)))")
            await asyncio.sleep(1)
            await page.js("true")       # the page's events are read while a command waits: the last ones too
            assert not page.errors, page.errors
        files = [(u, status, cache) for u, status, cache in page.responses if "/site/" in u or "/plugin-files/" in u]
        unversioned = [u for u, _, _ in files if not re.search(r"\?v=[0-9a-f]{12}\Z", u)]
        assert not unversioned, unversioned
        assert all(cache == FOR_GOOD for _, status, cache in files if status == 200), files
        seen = {u.split(base)[1].split("?")[0] for u, _, _ in files}
        for plain in ("/site/style.css", "/site/shell.js", "/site/vendor/vue.global.prod.js", "/site/assets/rook-logo.png",
                      "/site/assets/favicon.png", "/site/pages/workers.js", "/site/pages/workers-graphs.js",
                      "/site/pages/parts.js", "/site/pages/jobs.js", "/site/pages/data.js", "/site/terminal.js",
                      "/site/vendor/rook-scene.js", "/site/pages/settings.js", "/site/sections/hub.js", "/site/sections/plugins.js",
                      "/site/vendor/xterm/xterm.mjs", "/site/vendor/xterm/addon-fit.mjs", "/site/vendor/xterm/xterm.css",
                      "/plugin-files/agents/shell/dock.js", "/plugin-files/agents/shell/dock.css",
                      "/plugin-files/agents/shell/rookie.js", "/plugin-files/agents/shell/rookie-map.js",
                      "/plugin-files/agents/shell/chat-widgets.js", "/plugin-files/shelf/site/shelf.js",
                      "/plugin-files/shelf/site/book-card.js", "/plugin-files/shelf/site/settings.js"):
            assert plain in seen, (plain, sorted(seen))
    finally:
        await hub.stop_serving()


def test_a_new_hubs_first_run_loads_every_file_at_its_version(tmp_path):
    from test_site_dock import CHROME
    import pytest
    if CHROME is None:
        pytest.skip("no Chrome to load the site with")
    asyncio.run(first_run(tmp_path))


async def first_run(tmp_path):
    """A new hub started as the installer starts it (`--no-password`, open until claimed), and a fresh
    browser on `/`: the wizard's files and all they load come at their versions, as everything else."""
    from test_first_run import Started
    from test_site_dock import Chrome
    data, key = tmp_path / "data", tmp_path / "keys" / "vault.key"
    async with Started(data, key, extra=["--no-password"]) as hub, browser() as stranger:
        base = hub.base
        # Open until claimed, the site signs every visitor in: the page's map has the plugins' files.
        imports = import_map(await text(stranger, base + "/"))
        for file in ("setup.js", "settings-account.js", "dock.js"):
            plain = f"/plugin-files/agents/shell/{file}"
            assert imports[plain] == f"{plain}?v={version(AGENTS / 'shell' / file)}", plain
        async with watching(Chrome)() as page:
            page.responses = []
            await page.cmd("Page.navigate", url=base + "/")
            await page.until("performance.getEntriesByType('resource').some(e => e.name.includes('/plugin-files/agents/shell/setup.js'))")
            await page.until("document.body.classList.contains('setup')")
            await asyncio.sleep(1)
            await page.cmd("Page.navigate", url=base + "/settings?section=agents/your-account")
            await page.until("performance.getEntriesByType('resource').some(e => e.name.includes('settings-account.js'))")
            await asyncio.sleep(1)
            await page.js("true")       # the page's events are read while a command waits: the last ones too
            assert not page.errors, page.errors
    files = [(u, status, cache) for u, status, cache in page.responses if "/site/" in u or "/plugin-files/" in u]
    unversioned = [u for u, _, _ in files if not re.search(r"\?v=[0-9a-f]{12}\Z", u)]
    assert not unversioned, unversioned
    assert all(cache == FOR_GOOD for _, status, cache in files if status == 200), files
    seen = {u.split(base)[1].split("?")[0] for u, _, _ in files}
    for plain in ("/site/shell.js", "/site/style.css", "/site/vendor/vue.global.prod.js", "/site/pages/settings.js",
                  "/plugin-files/agents/shell/setup.js", "/plugin-files/agents/shell/dock.js",
                  "/plugin-files/agents/shell/dock.css", "/plugin-files/agents/shell/settings-account.js"):
        assert plain in seen, (plain, sorted(seen))
