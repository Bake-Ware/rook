"""Every answer says how it may be cached (brief 66), driven through real HTTP on localhost.

The site's files (the index at / and at each page path, /site/*, a plugin's pages and files) are
`Cache-Control: no-cache`: a browser or a proxy may keep them but asks the hub first, and an
unchanged file is answered 304 with no body. API answers (/api/*, /mcp, /knock, /link, /webhook)
are `no-store`, refusals included."""

import asyncio
import pathlib

from helpers import Clock
from rook_hub import Hub
from test_site import browser, login

SHELF = pathlib.Path(__file__).parent / "plugins" / "shelf"


def test_cache_headers(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    agent = hub.auth.mint("agent", user=alice["guid"])
    try:
        async with browser() as client, browser() as stranger:
            # 1. The site's files, to anyone who asks: no-cache, and 304 with no body when unchanged.
            site_files = ["/", "/workers", "/jobs", "/site/shell.js", "/site/pages/workers.js", "/site/style.css",
                          "/site/vendor/vue.global.prod.js", "/site/assets/rook-logo.png"]
            for path in site_files:
                await revalidated(stranger, base + path)

            # 2. A plugin's page and its files (to a session) are the site too.
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            async with client.post(base + "/api/plugins", json={"path": str(SHELF)}) as response:
                assert response.status == 200, await response.text()
                assert response.headers["Cache-Control"] == "no-store"
            await revalidated(client, base + "/plugins/shelf/shelf")
            await revalidated(client, base + "/plugin-files/shelf/site/shelf.js")
            async with stranger.get(base + "/plugin-files/shelf/site/shelf.js") as response:
                assert response.status == 401 and response.headers["Cache-Control"] == "no-cache"

            # 3. API answers are no-store, whether they answer or refuse.
            answers = [(client, "GET", "/api/session", None, 200), (stranger, "GET", "/api/session", None, 401),
                       (client, "GET", "/api/workers", None, 200), (stranger, "POST", "/api/login",
                                                                    {"name": "Alice", "password": "no"}, 401),
                       (stranger, "POST", "/knock", {}, 400), (stranger, "GET", "/link", None, 200),
                       (stranger, "POST", "/link", {}, 400), (stranger, "POST", "/webhook", {}, 401),
                       (stranger, "POST", "/mcp", {}, 400)]
            for who, method, path, body, status in answers:
                async with who.request(method, base + path, json=body) as response:
                    assert response.status == status, (path, response.status)
                    assert response.headers["Cache-Control"] == "no-store", path
            initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}}}
            async with stranger.post(base + "/mcp", json=initialize, headers={
                    "Authorization": f"Bearer {agent}", "Accept": "application/json, text/event-stream"}) as response:
                assert response.status == 200, await response.text()
                assert response.headers["Cache-Control"] == "no-store"
            async with client.post(base + "/api/login", json={"name": "Alice", "password": "correct horse"}) as response:
                assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    finally:
        await hub.stop_serving()


async def revalidated(client, url):
    """The file comes with no-cache and an ETag; asked again with that ETag, the hub answers 304, no body."""
    async with client.get(url) as response:
        assert response.status == 200, url
        assert response.headers["Cache-Control"] == "no-cache", url
        assert await response.read()
        etag = response.headers["ETag"]
    async with client.get(url, headers={"If-None-Match": etag}) as response:
        assert response.status == 304, url
        assert response.headers["Cache-Control"] == "no-cache", url
        assert await response.read() == b""
